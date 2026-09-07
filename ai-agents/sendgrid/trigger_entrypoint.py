from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent

logger = logging.getLogger(__name__)

SENDGRID_API_BASE = "https://api.sendgrid.com/v3"

ALL_EVENT_TYPES = [
    "processed", "dropped", "delivered", "deferred", "bounce",
    "open", "click", "spam_report", "unsubscribe",
    "group_unsubscribe", "group_resubscribe",
]

_DEFAULT_SUBSCRIBER_URL = "https://api.plumoai.com/company/aiagentchat/workflow/trigger-callback/sendgrid"


def _verify_ed25519_signature(public_key_b64: str, timestamp: str, raw_body: bytes, signature_b64: str) -> bool:
    """
    Verifies SendGrid's signed Event Webhook payload (Ed25519 over
    timestamp + raw_body). Requires the optional `cryptography` package --
    if it isn't installed, verification is skipped (treated the same as an
    unconfigured secret elsewhere in this codebase: unverified, not rejected,
    since requiring it is a deployment decision this trigger can't enforce
    unilaterally).
    https://www.twilio.com/docs/sendgrid/for-developers/tracking-events/getting-started-event-webhook-security-features
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        logger.warning("SendGrid webhook signature check skipped: 'cryptography' package not installed")
        return True

    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, timestamp.encode("utf-8") + raw_body)
        return True
    except (InvalidSignature, ValueError, TypeError) as e:
        logger.warning("SendGrid webhook signature validation failed: %s", e)
        return False


def _extract_connected_service_id(event: Dict[str, Any]) -> Optional[str]:
    custom_args = event.get("custom_args") if isinstance(event.get("custom_args"), dict) else {}
    cid = custom_args.get("plumo_connected_service_id") or event.get("plumo_connected_service_id")
    return str(cid) if cid else None


def _parse_multipart_form(headers: Dict[str, str], raw_body: bytes) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """
    Parses a multipart/form-data body (SendGrid's Inbound Parse delivery
    format -- unlike the Event Webhook's JSON array) into text fields and
    file attachments. Uses `multipart` (python-multipart), already a hard
    dependency of this backend's FastAPI stack, not an optional extra.
    """
    from multipart import parse_form

    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    mp_headers: Dict[str, bytes] = {
        "Content-Type": content_type.encode("utf-8") if isinstance(content_type, str) else content_type,
        "Content-Length": str(len(raw_body)).encode("utf-8"),
    }

    fields: Dict[str, str] = {}
    files: List[Dict[str, Any]] = []

    def on_field(f: Any) -> None:
        name = (f.field_name or b"").decode("utf-8", "replace")
        value = (f.value or b"").decode("utf-8", "replace")
        fields[name] = value

    def on_file(f: Any) -> None:
        f.file_object.seek(0)
        content = f.file_object.read()
        files.append({
            "field_name": (f.field_name or b"").decode("utf-8", "replace"),
            "file_name": (f.file_name or b"").decode("utf-8", "replace"),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "size": len(content),
        })

    parse_form(mp_headers, io.BytesIO(raw_body), on_field, on_file)
    return fields, files


def _sniff_multipart_boundary(raw: bytes) -> Optional[str]:
    """
    Recovers the multipart boundary from the body itself (its first line is
    always `--<boundary>`) for callers that only have the raw bytes/string
    and no Content-Type header to read it from -- e.g. execute_trigger, if
    trigger_body ever turns out to carry the still-raw Inbound Parse body
    instead of the empty dict this platform's /execute route usually hands
    it (see SendGridInboundParseTrigger.execute_trigger).
    """
    first_line = raw.split(b"\r\n", 1)[0]
    if first_line.startswith(b"--") and len(first_line) > 2:
        return first_line[2:].decode("utf-8", "replace").strip()
    return None


def _try_parse_multipart_bytes(raw: bytes) -> Optional[Dict[str, Any]]:
    """
    Best-effort: treats `raw` as a raw multipart/form-data body (sniffing its
    own boundary, see _sniff_multipart_boundary), parses it the same way
    _parse_multipart_form/handle_trigger_callback does, and returns the
    resulting fields (with any attachments attached under "attachments") --
    or None if `raw` doesn't look like multipart at all.
    """
    boundary = _sniff_multipart_boundary(raw)
    if not boundary:
        return None
    try:
        fields, files = _parse_multipart_form(
            {"content-type": f"multipart/form-data; boundary={boundary}"}, raw,
        )
    except Exception as e:
        logger.warning("SendGrid Inbound Parse: failed to parse multipart bytes: %s", e)
        return None
    result: Dict[str, Any] = dict(fields)
    if files:
        result["attachments"] = files
    return result


def _extract_inbound_parse_hostname(fields: Dict[str, str]) -> Optional[str]:
    """
    Derives which registered Inbound Parse hostname an inbound delivery
    belongs to -- the identifier subscribe() registered
    (SendGridInboundParseTrigger.subscribe) so this delivery can be routed
    back to the right workflow node(s), the same role connected_service_id
    plays for the Event Webhook. Prefers the `envelope` field (SendGrid's own
    JSON-encoded true recipient list) over the free-text `to` header, since
    `to` can contain a display name and isn't guaranteed to be the actual
    envelope recipient.
    """
    envelope_raw = fields.get("envelope")
    if envelope_raw:
        try:
            envelope = json.loads(envelope_raw)
            to_list = envelope.get("to") if isinstance(envelope, dict) else None
            if isinstance(to_list, list) and to_list and "@" in str(to_list[0]):
                return str(to_list[0]).rsplit("@", 1)[-1].strip().lower()
        except (ValueError, TypeError):
            pass

    to_header = fields.get("to") or ""
    m = re.search(r"[\w.+-]+@([\w.-]+)", to_header)
    return m.group(1).strip().lower() if m else None


class SendGridEmailEventTrigger(ConnectedServiceTriggerAgent):
    """
    Backs the single sendgrid_email_event trigger_id declared in plugin.json.
    Which SendGrid event type(s) fire it is a per-subscription choice, not a
    manifest-fixed one: the caller passes trigger_config["event_types"] (a
    multiselect list, e.g. ["delivered","bounce"]) to subscribe(), and every
    selected type is enabled on this SendGrid account's Event Webhook in one
    call.

    Unlike PlumoAI's own automations/trigger API, SendGrid's Event Webhook is
    account-wide -- one URL/settings object for the whole connected account,
    not something that can be pre-scoped per workflow node the way each
    PlumoAI record_events entry gets its own automation_id. So more than one
    workflow node subscribing this trigger on the SAME connection (each with
    its own event_types selection) shares one callback_unique_identifier (the
    connected_service_id), and subscribe() merges its selected flags into
    whatever's already enabled instead of overwriting the whole settings
    object -- so one node's selection never silently disables events another
    node already turned on.
    """

    def __init__(
        self,
        *,
        app_code: Optional[str] = None,
        workflow_id: Optional[str] = None,
        trigger_node_id: Optional[str] = None,
        trigger_static_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.app_code = app_code
        self.workflow_id = workflow_id
        self.trigger_node_id = trigger_node_id
        self.static_config: Dict[str, Any] = trigger_static_config or {}

    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return (
            "Enables every selected SendGrid event type on this account's signed Event "
            "Webhook in one call (merging with any other event types already enabled by "
            "another subscription on the same connection) so activity on emails sent "
            "through this connection fires this trigger."
        )

    async def _sg_call(self, method: str, path: str, *, json_body: Optional[Dict] = None) -> httpx.Response:
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.request(
                    method,
                    f"{SENDGRID_API_BASE}{path}",
                    headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                    json=json_body,
                )

        resp = await _call()
        if resp.status_code == 401 and await self.refresh_access_token():
            resp = await _call()
        return resp

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        trigger_config = trigger_config or {}
        requested = trigger_config.get("event_types") or trigger_config.get("eventTypes") or []
        if isinstance(requested, str):
            requested = [requested]
        selected = sorted({e for e in requested if e in ALL_EVENT_TYPES})
        if not selected:
            return {
                "success": False,
                "error": f"trigger_config.event_types must include at least one of: {', '.join(ALL_EVENT_TYPES)}",
            }
        if not self.access_token:
            return {"success": False, "error": "No SendGrid API key available for this connection"}

        subscriber_url = self.static_config.get("subscriber_url") or _DEFAULT_SUBSCRIBER_URL

        # The Event Webhook is one account-wide settings object -- read
        # whatever's already enabled (e.g. by another workflow node's
        # subscription on this same connection) so this call only ADDS the
        # selected event_types, never turns another subscription's event
        # back off.
        current_flags: Dict[str, bool] = {}
        try:
            get_resp = await self._sg_call("GET", "/user/webhooks/event/settings")
            if get_resp.status_code < 300:
                current = get_resp.json() or {}
                current_flags = {e: bool(current.get(e)) for e in ALL_EVENT_TYPES}
        except httpx.RequestError as e:
            logger.warning("SendGrid event webhook settings lookup failed (continuing with just the selected events enabled): %s", e)

        event_flags = {e: current_flags.get(e, False) for e in ALL_EVENT_TYPES}
        for e in selected:
            event_flags[e] = True

        settings_body = {"enabled": True, "url": subscriber_url, **event_flags}
        try:
            resp = await self._sg_call("PATCH", "/user/webhooks/event/settings", json_body=settings_body)
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request to SendGrid event webhook settings API failed: {e}"}
        if resp.status_code >= 300:
            return {
                "success": False,
                "error": f"SendGrid event webhook settings API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }

        public_key: Optional[str] = None
        try:
            sign_resp = await self._sg_call("PATCH", "/user/webhooks/event/settings/signed", json_body={"enabled": True})
            if sign_resp.status_code < 300:
                key_resp = await self._sg_call("GET", "/user/webhooks/event/settings/signed")
                if key_resp.status_code < 300:
                    public_key = (key_resp.json() or {}).get("public_key")
        except httpx.RequestError as e:
            logger.warning("SendGrid signed-webhook setup failed (continuing unsigned): %s", e)

        cid = self.connected_service_id
        if not cid:
            return {"success": False, "error": "No connected_service_id available on this SendGrid connection"}
        callback_unique_identifier = str(cid)

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=callback_unique_identifier,
            metadata={"public_key": public_key, "event_types": selected},
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"Event webhook configured but workflow trigger callback registration failed: {callback_result.get('error')}",
                "callback_unique_identifier": callback_unique_identifier,
            }

        return {
            "success": True,
            "callback_unique_identifier": callback_unique_identifier,
            "event_types": selected,
            "signed": bool(public_key),
        }

    async def execute_trigger(
        self, trigger_body: Any, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        trigger_body IS SendGrid's own Event Webhook payload -- a JSON array
        of event objects. The platform's callback dispatch already scopes
        delivery to this connection via callback_unique_identifier
        (connected_service_id), same role PlumoAI's automations/trigger
        pre-scoping plays for PlumoaiRecordTrigger -- so, mirroring
        PlumoaiRecordTrigger.execute_trigger, this node's output is
        trigger_body itself, unmodified. A non-list body (defensive only --
        SendGrid always delivers a JSON array) is wrapped in a list to match
        this method's List[Dict] contract, same as PlumoaiRecordTrigger
        wrapping its single-dict trigger_body in [trigger_body].
        """
        logger.info(
            "SendGridEmailEventTrigger.execute_trigger: trigger_body type=%s value=%r metadata=%r",
            type(trigger_body).__name__, trigger_body, metadata,
        )
        if isinstance(trigger_body, list):
            return trigger_body
        if trigger_body is None:
            return []
        return [trigger_body]


class SendGridInboundParseTrigger(ConnectedServiceTriggerAgent):
    """
    Backs the sendgrid_inbound_email trigger_id declared in plugin.json --
    SendGrid's Inbound Parse feature (receiving mail), a completely separate
    product from the Event Webhook (tracking sent mail) SendGridEmailEventTrigger
    covers, with its own settings API and its own delivery format
    (multipart/form-data, not a JSON array of events).
    https://www.twilio.com/docs/sendgrid/api-reference/settings-inbound-parse/create-a-parse-setting

    subscribe() registers/updates ONE SendGrid-side Parse setting for a
    caller-chosen hostname (trigger_config["hostname"]) -- the hostname's own
    MX record must already point at mx.sendgrid.net in the user's DNS; that
    part is entirely outside this platform's or SendGrid's API surface, so
    subscribe() can't do it and doesn't attempt to.

    callback_unique_identifier is the hostname itself (lowercased), not
    connected_service_id -- unlike the Event Webhook, an inbound email
    carries no custom_args this platform controls, but Inbound Parse
    hostnames are already globally unique per DNS record, so the hostname
    embedded in the delivery's own `to`/`envelope` field is a suffient,
    self-describing routing key (see _extract_inbound_parse_hostname()) --
    same idea as WhatsApp's phone_number_id or PlumoAI's project_id/table_id.
    """

    def __init__(
        self,
        *,
        app_code: Optional[str] = None,
        workflow_id: Optional[str] = None,
        trigger_node_id: Optional[str] = None,
        trigger_static_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.app_code = app_code
        self.workflow_id = workflow_id
        self.trigger_node_id = trigger_node_id
        self.static_config: Dict[str, Any] = trigger_static_config or {}

    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return (
            "Registers or updates a SendGrid Inbound Parse setting for the given hostname, "
            "pointed at this platform's shared SendGrid callback endpoint, so inbound email "
            "arriving at that hostname (once its MX record points to mx.sendgrid.net) fires "
            "this trigger."
        )

    async def _sg_call(self, method: str, path: str, *, json_body: Optional[Dict] = None) -> httpx.Response:
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.request(
                    method,
                    f"{SENDGRID_API_BASE}{path}",
                    headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                    json=json_body,
                )

        resp = await _call()
        if resp.status_code == 401 and await self.refresh_access_token():
            resp = await _call()
        return resp

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        trigger_config = trigger_config or {}
        hostname = str(trigger_config.get("hostname") or "").strip().lower()
        if not hostname:
            return {"success": False, "error": "trigger_config.hostname is required, e.g. 'parse.yourdomain.com'"}
        if not self.access_token:
            return {"success": False, "error": "No SendGrid API key available for this connection"}

        subscriber_url = self.static_config.get("subscriber_url") or _DEFAULT_SUBSCRIBER_URL
        spam_check = bool(trigger_config.get("spam_check", False))
        send_raw = bool(trigger_config.get("send_raw", False))
        settings_body = {"hostname": hostname, "url": subscriber_url, "spam_check": spam_check, "send_raw": send_raw}

        try:
            existing = await self._sg_call("GET", f"/user/webhooks/parse/settings/{hostname}")
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request to SendGrid Inbound Parse settings API failed: {e}"}

        try:
            if existing.status_code < 300:
                # hostname is part of the URL, not the PATCH body, for updates.
                resp = await self._sg_call(
                    "PATCH", f"/user/webhooks/parse/settings/{hostname}",
                    json_body={"url": subscriber_url, "spam_check": spam_check, "send_raw": send_raw},
                )
            else:
                resp = await self._sg_call("POST", "/user/webhooks/parse/settings", json_body=settings_body)
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request to SendGrid Inbound Parse settings API failed: {e}"}

        if resp.status_code >= 300:
            return {
                "success": False,
                "error": f"SendGrid Inbound Parse settings API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=hostname,
            metadata={"hostname": hostname, "send_raw": send_raw},
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"Inbound Parse setting configured but workflow trigger callback registration failed: {callback_result.get('error')}",
                "callback_unique_identifier": hostname,
            }

        return {"success": True, "callback_unique_identifier": hostname, "hostname": hostname}

    async def execute_trigger(
        self, trigger_body: Any, metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Mirrors how handle_trigger_callback reads an Inbound Parse delivery
        (see _parse_multipart_form / _try_parse_multipart_bytes): if
        trigger_body turns out to actually carry the raw multipart body --
        as bytes/str, at the top level or nested under a common wrapper key
        -- this parses it the same way and returns the real fields object
        (to/from/subject/text/html/envelope/... plus "attachments") instead
        of an opaque/empty value.

        Known limitation, confirmed in production logs: as of the last
        deployed check, the platform's own /execute route
        (backend/routes/agent_triggers_routes.py) received triggerBody as an
        EMPTY dict for a real Inbound Parse delivery -- the external
        workflow/trigger service that populates it (not part of this repo)
        appears to build it by decoding the same raw webhook body it already
        forwarded to /identify-workflow-trigger, which works for the Event
        Webhook's JSON array but fails for Inbound Parse's multipart body.
        The parsing added here recovers the real data IF that body ever
        arrives in some raw/wrapped form; it cannot recover anything from an
        already-empty dict, since at that point the actual field data is
        gone -- handle_trigger_callback parsed it correctly earlier, but
        that result is never threaded through to this call. A genuinely
        empty dict remains a signal that the upstream service needs fixing,
        not something this method can work around.
        """
        logger.info(
            "SendGridInboundParseTrigger.execute_trigger: trigger_body type=%s value=%r metadata=%r",
            type(trigger_body).__name__, trigger_body, metadata,
        )

        if isinstance(trigger_body, (bytes, bytearray)):
            parsed = _try_parse_multipart_bytes(bytes(trigger_body))
            if parsed is not None:
                return [parsed]
            try:
                decoded = json.loads(trigger_body)
                return decoded if isinstance(decoded, list) else [decoded]
            except (ValueError, TypeError):
                return [{"raw": trigger_body.decode("utf-8", "replace")}]

        if isinstance(trigger_body, str):
            parsed = _try_parse_multipart_bytes(trigger_body.encode("utf-8", "replace"))
            if parsed is not None:
                return [parsed]
            try:
                decoded = json.loads(trigger_body)
                return decoded if isinstance(decoded, list) else [decoded]
            except (ValueError, TypeError):
                return [{"raw": trigger_body}]

        if isinstance(trigger_body, dict):
            # A forwarder that can't natively carry a multipart body as JSON
            # might still stash the raw bytes/string under a wrapper key
            # instead of dropping it -- check the common ones before giving up.
            for key in ("raw_body", "raw", "body", "multipart"):
                value = trigger_body.get(key)
                if isinstance(value, (bytes, bytearray)):
                    parsed = _try_parse_multipart_bytes(bytes(value))
                    if parsed is not None:
                        return [parsed]
                elif isinstance(value, str):
                    parsed = _try_parse_multipart_bytes(value.encode("utf-8", "replace"))
                    if parsed is not None:
                        return [parsed]
            return [trigger_body]

        if isinstance(trigger_body, list):
            return trigger_body
        if trigger_body is None:
            return []
        return [{"raw": trigger_body}]


async def create_trigger_agent(
    *,
    app_code: str,
    trigger_id: str,
    app_config: Dict[str, Any],
    trigger_static_config: Optional[Dict[str, Any]] = None,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    trigger_node_id: Optional[str] = None,
) -> ConnectedServiceTriggerAgent:
    cls = SendGridInboundParseTrigger if trigger_id == "sendgrid_inbound_email" else SendGridEmailEventTrigger
    return cls(
        token=token,
        company_id=company_id,
        user_id=user_id,
        app_config=app_config or {},
        trigger_static_config=trigger_static_config or {},
        app_code=app_code,
        workflow_id=workflow_id,
        trigger_node_id=trigger_node_id,
    )


def handle_trigger_callback(
    *,
    app_code: str,
    trigger_defs: List[Any],
    method: str,
    headers: Dict[str, str],
    query_params: Dict[str, str],
    raw_body: bytes,
) -> Dict[str, Any]:
    """
    Owns the whole inbound-callback decision for BOTH SendGrid trigger types
    declared in plugin.json (sendgrid_email_event and sendgrid_inbound_email)
    -- this platform's trigger dispatch only ever loads ONE handle_trigger_callback
    per app_code (trigger_registry.identify_workflow_trigger_node() always
    loads trigger_defs[0]'s module, regardless of which trigger_id a given
    delivery is actually for), so a single shared SendGrid callback URL and a
    single function here must tell the two providers' completely different
    payload formats apart:
    - Event Webhook: JSON array of event objects (Content-Type: application/json).
    - Inbound Parse: multipart/form-data with to/from/subject/text/html/
      envelope/attachment* fields (Content-Type: multipart/form-data).
    Dispatched purely on the Content-Type header -- same "no verification
    handshake, just POSTs a body" shape either way.

    Signature verification is NOT performed here even though subscribe()
    enables SendGrid's signed Event Webhook and records each connection's
    Ed25519 public_key: unlike WhatsApp's WHATSAPP_APP_SECRET (one shared
    secret for this platform's single Meta app, valid before any specific
    connected account is known), SendGrid generates a distinct keypair PER
    connected account, and trigger_defs here only carries this plugin's
    static manifest config (plugin.json's triggers[].config -- just
    subscriber_url), not any per-connection subscribe() metadata. There is no
    way to know which connection's public_key to check against before the
    body is parsed and its custom_args.plumo_connected_service_id is read --
    so, same "unverified, not rejected" precedent used elsewhere in this
    codebase when a check can't be enforced unilaterally, requests are
    accepted unverified. _verify_ed25519_signature() is kept available for a
    future revision of this callback layer that threads per-connection
    metadata through (or for out-of-band verification once
    callback_unique_identifiers below have resolved a specific connection).
    Inbound Parse deliveries have no signing mechanism at all to verify.

    callback_unique_identifiers:
    - Event Webhook: each event's custom_args.plumo_connected_service_id --
      the tag send_email/send_template_email attach to every outbound
      message specifically so the corresponding inbound event can be routed
      back to the right connection, the same role phone_number_id plays for
      WhatsApp and event_type_id plays for Cal.com.
    - Inbound Parse: the receiving hostname, read from the delivery's own
      envelope/to field (see _extract_inbound_parse_hostname) -- the same
      value SendGridInboundParseTrigger.subscribe() registered.
    """
    content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        try:
            fields, _files = _parse_multipart_form(headers, raw_body)
        except Exception as e:
            logger.warning("SendGrid Inbound Parse callback: failed to parse multipart body: %s", e)
            return {
                "status_code": 200,  # ack anyway -- SendGrid retries on non-2xx and a malformed delivery won't improve on retry
                "response_body": {},
                "response_content_type": "application/json",
                "callback_unique_identifiers": [],
            }
        hostname = _extract_inbound_parse_hostname(fields)
        logger.info(
            "SendGrid Inbound Parse callback received: app_code=%s hostname=%s fields=%s",
            app_code, hostname, sorted(fields.keys()),
        )
        return {
            "status_code": 200,
            "response_body": {},
            "response_content_type": "application/json",
            "callback_unique_identifiers": [hostname] if hostname else [],
        }

    try:
        callback_body = json.loads(raw_body) if raw_body else []
    except ValueError:
        callback_body = []
    if not isinstance(callback_body, list):
        callback_body = []

    logger.info("SendGrid Event Webhook callback received: app_code=%s events=%d", app_code, len(callback_body))
    identifiers = sorted({
        cid for e in callback_body if isinstance(e, dict) for cid in [_extract_connected_service_id(e)] if cid
    })

    return {
        "status_code": 200,
        "response_body": {},
        "response_content_type": "application/json",
        "callback_unique_identifiers": identifiers,
    }
