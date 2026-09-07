from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"


def _parse_whatsapp_webhook_body(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Flattens Meta's webhook envelope (entry[].changes[] where field=="messages")
    into one {phone_number_id, message, contact} dict per inbound message --
    a single delivery can batch messages for more than one phone number and/or
    more than one message at once.
    https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
    """
    results: List[Dict[str, Any]] = []
    for entry in (body.get("entry") or []):
        for change in (entry.get("changes") or []):
            if change.get("field") not in (None, "messages"):
                continue
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            contacts_by_wa_id = {c.get("wa_id"): c for c in (value.get("contacts") or [])}
            for msg in (value.get("messages") or []):
                results.append({
                    "phone_number_id": phone_number_id,
                    "message": msg,
                    "contact": contacts_by_wa_id.get(msg.get("from")),
                })
    return results


def _verify_signature(app_secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


class WhatsAppMessageReceivedTrigger(ConnectedServiceTriggerAgent):
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

        metadata = self.service_credential.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata) if metadata else {}
            except json.JSONDecodeError:
                metadata = {}
        self._metadata: Dict[str, Any] = metadata if isinstance(metadata, dict) else {}
        self.waba_id: Optional[str] = self._str_or_none(self._metadata.get("waba_id"))
        self.phone_number_id: Optional[str] = self._str_or_none(self._metadata.get("phone_number_id"))
        self.graph_version = self._metadata.get("graph_version") or os.getenv("WHATSAPP_GRAPH_API_VERSION") or DEFAULT_GRAPH_VERSION

    @staticmethod
    def _str_or_none(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return "Subscribes this platform's webhook app to a connected WABA's message events so inbound WhatsApp messages fire this trigger."

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        # phone_number_id scopes which inbound messages this specific workflow
        # node cares about -- required because register_workflow_trigger_callback()
        # needs a deterministic callback_unique_identifier, and Meta's webhook
        # payload always carries phone_number_id (value.metadata.phone_number_id),
        # same role as Gmail's emailAddress / Cal.com's event_type_id.
        phone_number_id = (
            trigger_config.get("phone_number_id")
            or trigger_config.get("phoneNumberId")
            or self.phone_number_id
        )
        if not phone_number_id:
            return {"success": False, "error": "trigger_config.phone_number_id is required (no default phone_number_id configured on this connection either)"}
        if not self.waba_id:
            return {"success": False, "error": "No waba_id configured on this connected WhatsApp Business account"}
        if not self.access_token:
            return {"success": False, "error": "No access token available for this connected WhatsApp Business account"}

        # Subscribes this platform's Meta app to the WABA's webhook events --
        # WABA-wide, not per phone number (Meta has no per-number subscription
        # concept); safe to call repeatedly, Meta treats it as idempotent.
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.post(
                    f"{GRAPH_API_BASE}/{self.graph_version}/{self.waba_id}/subscribed_apps",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request to WhatsApp subscribed_apps API failed: {e}"}

        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"WhatsApp subscribed_apps API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }

        data = resp.json()

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=phone_number_id,
            metadata={
                "phone_number_id": phone_number_id,
                "waba_id": self.waba_id,
                "last_processed_message_id": None,
            },
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"WABA subscribed but workflow trigger callback registration failed: {callback_result.get('error')}",
                "callback_unique_identifier": phone_number_id,
                "raw_response": data,
            }

        return {
            "success": True,
            "callback_unique_identifier": phone_number_id,
            "raw_response": data,
        }

    async def execute_trigger(
        self, trigger_body: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Called by the workflow engine when this trigger node runs. trigger_body
        is Meta's own webhook envelope (see _parse_whatsapp_webhook_body()).
        Only messages for this node's phone_number_id (saved in metadata by
        subscribe()) are relevant -- a single delivery can carry messages for
        other phone numbers on the same WABA too.
        """
        metadata = metadata or {}
        trigger_body = trigger_body or {}
        phone_number_id = metadata.get("phone_number_id") or self.phone_number_id

        parsed_messages = _parse_whatsapp_webhook_body(trigger_body)
        relevant = [m for m in parsed_messages if not phone_number_id or m.get("phone_number_id") == phone_number_id]
        if not relevant:
            logger.info("Whatsapp execute_trigger: no messages for phone_number_id=%s in this delivery", phone_number_id)
            return []

        # Duplicate-delivery guard -- Meta retries webhook delivery on non-2xx
        # responses, so the same message can arrive more than once. Same
        # simple last-id cursor approach as Cal.com's last_processed_uid.
        last_processed_message_id = metadata.get("last_processed_message_id")
        outputs: List[Dict[str, Any]] = []
        newest_message_id = last_processed_message_id
        for item in relevant:
            msg = item["message"]
            message_id = msg.get("id")
            if last_processed_message_id and message_id == last_processed_message_id:
                continue
            contact = item.get("contact") or {}
            msg_type = msg.get("type")
            text_body = (msg.get("text") or {}).get("body") if msg_type == "text" else None
            outputs.append({
                "success": True,
                "response": text_body or f"{msg_type} message from {msg.get('from')}",
                "message_id": message_id,
                "from": msg.get("from"),
                "contact_name": (contact.get("profile") or {}).get("name"),
                "type": msg_type,
                "body": text_body,
                "timestamp": msg.get("timestamp"),
                "raw_message": msg,
            })
            newest_message_id = message_id

        if outputs and newest_message_id != last_processed_message_id and phone_number_id:
            updated_metadata = dict(metadata)
            updated_metadata["last_processed_message_id"] = newest_message_id
            callback_result = await self.register_workflow_trigger_callback(
                callback_unique_identifier=phone_number_id,
                metadata=updated_metadata,
            )
            if not callback_result.get("success"):
                logger.warning(
                    "Whatsapp execute_trigger: failed to advance last_processed_message_id to %s: %s",
                    newest_message_id, callback_result.get("error"),
                )

        return outputs


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
) -> WhatsAppMessageReceivedTrigger:
    return WhatsAppMessageReceivedTrigger(
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
    Owns the whole inbound-callback decision for WhatsApp Business Cloud API
    webhooks (Meta Graph API). Two distinct protocols share this one webhook
    URL:

    1. Verification handshake (GET, run once when the webhook URL is
       configured in the Meta App Dashboard):
       ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=.... Checked
       against trigger_defs[].config.verification_token -- static,
       manifest-level config (ai_agents/whatsapp_business/plugin.json's
       triggers[].config), same idea as Gmail's topic_name or Cal.com's
       cal_event_trigger -- not a per-connected-account credential, since
       this handshake happens on one shared platform URL before any specific
       account is known. Mismatch rejects with 401 (this platform's generic
       auth-failure convention; Meta's own docs suggest 403 for this specific
       case -- switch to that if matching Meta's exact convention matters
       more than this platform's). If it matches (or no token is configured
       to check against), echoes back the raw hub.challenge value as plain
       text -- exactly what Meta requires.
    2. Message delivery (POST, ongoing): optionally signed with
       X-Hub-Signature-256 (HMAC-SHA256 of the raw body using the app
       secret). Verified only if WHATSAPP_APP_SECRET is configured -- unset
       means unverified, not rejected, since requiring it is a deployment
       decision this trigger can't enforce unilaterally.

    callback_unique_identifier is phone_number_id -- the same value
    subscribe() registers, and the one field every inbound message envelope
    guarantees (value.metadata.phone_number_id). A single delivery can batch
    messages for more than one phone number, so more than one identifier can
    come back.
    """
    hub_mode = query_params.get("hub.mode") or query_params.get("hub_mode")
    if hub_mode == "subscribe":
        verify_token = query_params.get("hub.verify_token") or query_params.get("hub_verify_token")
        expected_token = next(
            ((t.config or {}).get("verification_token") for t in trigger_defs if (t.config or {}).get("verification_token")),
            None,
        )
        if expected_token and verify_token != expected_token:
            logger.warning("WhatsApp webhook verification failed: hub.verify_token mismatch")
            return {
                "status_code": 401,
                "response_body": "verification token mismatch",
                "response_content_type": "text/plain",
                "callback_unique_identifiers": [],
            }

        hub_challenge = query_params.get("hub.challenge") or query_params.get("hub_challenge") or ""
        logger.info("WhatsApp webhook verification handshake: hub.mode=subscribe, echoing hub.challenge")
        return {
            "status_code": 200,
            "response_body": hub_challenge,
            "response_content_type": "text/plain",
            "callback_unique_identifiers": [],
        }

    app_secret = os.environ.get("WHATSAPP_APP_SECRET")
    if app_secret:
        signature_header = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256")
        if not _verify_signature(app_secret, raw_body, signature_header):
            logger.warning("WhatsApp webhook signature validation failed")
            return {
                "status_code": 401,
                "response_body": {"error": "invalid X-Hub-Signature-256"},
                "response_content_type": "application/json",
                "callback_unique_identifiers": [],
            }

    try:
        callback_body = json.loads(raw_body) if raw_body else {}
    except ValueError:
        callback_body = {}
    if not isinstance(callback_body, dict):
        callback_body = {}

    logger.info("WhatsApp trigger callback received: app_code=%s body=%s", app_code, callback_body)
    parsed_messages = _parse_whatsapp_webhook_body(callback_body)
    identifiers = sorted({m["phone_number_id"] for m in parsed_messages if m.get("phone_number_id")})

    return {
        "status_code": 200,
        "response_body": {},
        "response_content_type": "application/json",
        "callback_unique_identifiers": identifiers,
    }
