from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent

logger = logging.getLogger(__name__)

CALCOM_ME_URL = "https://api.cal.com/v2/me"
CALCOM_BOOKINGS_URL = "https://api.cal.com/v2/bookings"
# Must match calcom_functions.py's BOOKINGS_ACTIONS_VERSION -- both call the
# same GET /v2/bookings/{uid} endpoint.
BOOKINGS_VERSION = "2026-02-25"


def _verify_calcom_signature(secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Cal.com signs webhook deliveries by HMAC-SHA256'ing the raw request body
    with the webhook's secret and sending the resulting hex digest as-is in
    the X-Cal-Signature-256 header (no "sha256=" prefix, unlike Meta's
    convention) -- https://cal.com/docs/platform/webhooks/verify-signature.
    """
    if not signature_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


def _unwrap_calcom_response(data: Any) -> Any:
    # Cal.com v2 wraps most responses as {"status": "success", "data": ...};
    # tolerate an unwrapped body too in case that ever changes.
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _build_callback_identifier(cal_event_trigger: Any, event_type_id: Any, calcom_userid: Any) -> str:
    return f"{cal_event_trigger}_{event_type_id}_{calcom_userid}"


def _parse_calcom_webhook_body(trigger_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cal.com webhook deliveries come in one of two documented shapes (this
    trigger no longer registers a custom payloadTemplate, so it must handle
    whatever Cal.com's own default payload looks like):
    - nested format (BOOKING_CREATED/CANCELLED/RESCHEDULED and most other
      triggers): {triggerEvent, payload: {uid, eventTypeId, organizer: {id, ...}, ...}}
      https://cal.com/help/webhooks#booking-events-nested-format
    - flat format (MEETING_STARTED/MEETING_ENDED): {triggerEvent, uid,
      eventTypeId, userId, ...} -- fields live at the top level, no "payload" wrapper
      https://cal.com/help/webhooks#meeting-started-/-meeting-ended-flat-format
    Returns normalized {trigger_event, uid, event_type_id, calcom_userid}, any
    of which may be None if the body doesn't match either shape.
    """
    trigger_body = trigger_body or {}
    trigger_event = trigger_body.get("triggerEvent")
    payload = trigger_body.get("payload")
    if isinstance(payload, dict):
        organizer = payload.get("organizer") or {}
        return {
            "trigger_event": trigger_event,
            "uid": payload.get("uid"),
            "event_type_id": payload.get("eventTypeId"),
            "calcom_userid": organizer.get("id"),
        }
    return {
        "trigger_event": trigger_event,
        "uid": trigger_body.get("uid"),
        "event_type_id": trigger_body.get("eventTypeId"),
        "calcom_userid": trigger_body.get("userId"),
    }


class CalcomWebhookTrigger(ConnectedServiceTriggerAgent):
    """
    One class backs all four calcom_* trigger_ids declared in plugin.json
    (booking created/cancelled/rescheduled, meeting ended) -- which Cal.com
    event it subscribes to is read from trigger_static_config["cal_event_trigger"],
    not hardcoded here.

    subscribe() does NOT call Cal.com's webhooks API -- it assumes a webhook is
    already configured out-of-band on the connected Cal.com account (e.g. via
    Cal.com's own dashboard) pointed at this platform's shared trigger-callback
    endpoint, sending Cal.com's default payload shape. All subscribe() does is
    register a deterministic callback_unique_identifier
    (cal_event_trigger_eventTypeId_calcomUserId) so that identify_workflow_trigger_node()
    can later match an inbound webhook delivery back to this workflow/node --
    see handle_trigger_callback() and _parse_calcom_webhook_body() above,
    which reconstruct that same identifier from Cal.com's real webhook body
    (nested or flat format, per https://cal.com/help/webhooks).
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
            "Registers a workflow trigger callback for a Cal.com event "
            "(booking created/cancelled/rescheduled, or meeting ended) assuming a "
            "matching webhook is already configured on the Cal.com account."
        )

    async def _fetch_calcom_userid(self) -> Optional[Any]:
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.get(CALCOM_ME_URL, headers={"Authorization": f"Bearer {self.access_token}"})

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            logger.warning("Calcom subscribe: request to /me failed: %s", e)
            return None

        if resp.status_code != 200:
            logger.warning("Calcom subscribe: /me returned %s: %s", resp.status_code, resp.text[:500])
            return None

        me = _unwrap_calcom_response(resp.json())
        return me.get("id") if isinstance(me, dict) else None

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        # cal_event_trigger is static, manifest-level config (ai_agents/calcom/plugin.json's
        # triggers[].config) -- same idea as ToolPlugin.mcp_url for mcp_wrapper tools --
        # not supplied by the API caller.
        cal_event_trigger = (self.static_config.get("cal_event_trigger") or "").strip()
        if not cal_event_trigger:
            return {"success": False, "error": "trigger is missing config.cal_event_trigger in ai_agents/calcom/plugin.json"}

        # event_type_id is required here (unlike the old webhook-API approach)
        # because it's part of the deterministic callback_unique_identifier below --
        # a real Cal.com webhook delivery always carries a concrete eventTypeId, so
        # there's no "any event type" value handle_trigger_callback() could ever match.
        event_type_id = trigger_config.get("event_type_id") or trigger_config.get("eventTypeId")
        if not event_type_id:
            return {"success": False, "error": "trigger_config.event_type_id is required"}

        if not self.access_token:
            return {"success": False, "error": "No access token available for this connected Cal.com account"}

        calcom_userid = await self._fetch_calcom_userid()
        if calcom_userid is None:
            return {"success": False, "error": "Could not resolve the connected Cal.com account's user id via GET /v2/me"}

        # No Cal.com webhooks API call here -- this assumes a webhook is already
        # configured out-of-band on the Cal.com account (e.g. via Cal.com's own
        # dashboard) pointed at this platform's shared trigger-callback endpoint.
        # This identifier is deterministic (not a random token) so
        # handle_trigger_callback() can reconstruct the exact same string
        # from the fields Cal.com's own default webhook payload carries.
        callback_unique_identifier = _build_callback_identifier(cal_event_trigger, event_type_id, calcom_userid)

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=callback_unique_identifier,
            metadata={
                "cal_event_trigger": cal_event_trigger,
                "event_type_id": event_type_id,
                "calcom_userid": calcom_userid,
                "callback_unique_identifier": callback_unique_identifier,
                "last_processed_uid": None,
            },
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"Workflow trigger callback registration failed: {callback_result.get('error')}",
                "callback_unique_identifier": callback_unique_identifier,
            }

        return {
            "success": True,
            "callback_unique_identifier": callback_unique_identifier,
        }

    async def execute_trigger(
        self, trigger_body: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Called by the workflow engine when this trigger node runs. trigger_body
        is Cal.com's own default webhook payload (nested or flat format, see
        _parse_calcom_webhook_body()) -- subscribe() no longer registers a
        custom payloadTemplate, since it no longer calls Cal.com's webhooks API
        at all. Only uid is pulled out of it here; the full booking (title,
        description, all attendees, hosts, location, cancellation/reschedule
        reasons, ...) is fetched via GET /v2/bookings/{uid}, same as Gmail's
        trigger following up its push notification with a messages.get call --
        this also gives a canonical, complete shape regardless of which of the
        two webhook formats delivered the event.
        """
        metadata = metadata or {}
        trigger_body = trigger_body or {}
        parsed = _parse_calcom_webhook_body(trigger_body)
        trigger_event = parsed["trigger_event"] or self.static_config.get("cal_event_trigger")
        booking_uid = parsed["uid"]
        if not booking_uid:
            logger.warning("Calcom execute_trigger: no uid in webhook delivery: %r", trigger_body)
            return [{"success": False, "error": "no booking uid in webhook delivery"}]

        # Duplicate-delivery guard -- Cal.com retries webhook delivery on
        # non-2xx responses, so the same event can arrive more than once.
        # There's no cursor to page through like Gmail's history_id; instead
        # this just tracks the last booking uid processed for this subscription.
        # The cursor is only advanced further below, after the booking fetch
        # actually succeeds -- advancing it here (before knowing whether this
        # delivery is handled successfully) would permanently swallow the event
        # on any transient failure, since a retried delivery would then match
        # last_processed_uid and be skipped without ever having been delivered.
        last_processed_uid = metadata.get("last_processed_uid")
        if last_processed_uid and booking_uid == last_processed_uid:
            logger.info("Calcom execute_trigger: skipping duplicate delivery for booking_uid=%s", booking_uid)
            return []

        if not self.access_token:
            return [{"success": False, "error": "No access token available for this connected Cal.com account"}]

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.get(
                    f"{CALCOM_BOOKINGS_URL}/{booking_uid}",
                    headers={"Authorization": f"Bearer {self.access_token}", "cal-api-version": BOOKINGS_VERSION},
                )

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            logger.warning("Calcom execute_trigger: request to bookings API failed: %s", e)
            return [{"success": False, "error": f"Request to Cal.com bookings API failed: {e}"}]

        if resp.status_code != 200:
            logger.warning(
                "Calcom execute_trigger: bookings API returned %s for uid=%s: %s",
                resp.status_code, booking_uid, resp.text[:1000],
            )
            return [{
                "success": False,
                "error": f"Cal.com bookings API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }]

        booking = _unwrap_calcom_response(resp.json())
        # A recurring booking uid can return an array of recurrences -- take
        # the one this webhook actually fired for (the first entry is fine
        # here since every recurrence shares the same booking-level fields
        # this trigger surfaces; only start/end genuinely differ per entry).
        if isinstance(booking, list):
            booking = booking[0] if booking else {}
        if not isinstance(booking, dict):
            booking = {}

        hosts = booking.get("hosts") or []
        organizer = hosts[0] if hosts else {}
        attendees = [
            {
                "name": a.get("name"),
                "email": a.get("email"),
                "timezone": a.get("timeZone"),
                "phone_number": a.get("phoneNumber"),
                "absent": a.get("absent"),
            }
            for a in (booking.get("attendees") or [])
        ]
        event_type = booking.get("eventType") or {}

        # Booking fetched successfully -- now it's safe to advance the dedup
        # cursor (see the comment above the duplicate-delivery guard for why
        # this happens after, not before, the fetch).
        callback_unique_identifier = metadata.get("callback_unique_identifier")
        if callback_unique_identifier:
            updated_metadata = dict(metadata)
            updated_metadata["last_processed_uid"] = booking_uid
            callback_result = await self.register_workflow_trigger_callback(
                callback_unique_identifier=callback_unique_identifier,
                metadata=updated_metadata,
            )
            if not callback_result.get("success"):
                logger.warning(
                    "Calcom execute_trigger: failed to advance trigger callback last_processed_uid to %s: %s",
                    booking_uid, callback_result.get("error"),
                )
        else:
            logger.warning("Calcom execute_trigger: no callback_unique_identifier in metadata -- cannot advance dedup cursor")

        # Same idea as Gmail's trigger output mirroring GmailFunctions.read_email() --
        # a trigger firing is conceptually "a booking event happened", so its
        # output should look like the data a booking-read tool call would return.
        return [{
            "success": True,
            "response": f"{trigger_event}: {booking.get('title') or booking_uid} ({len(attendees)} attendee(s))",
            "booking_uid": booking.get("uid") or booking_uid,
            "trigger_event": trigger_event,
            "status": booking.get("status"),
            "event_type_id": event_type.get("id"),
            "event_type_slug": event_type.get("slug"),
            "title": booking.get("title"),
            "description": booking.get("description"),
            "start_time": booking.get("start"),
            "end_time": booking.get("end"),
            "duration": booking.get("duration"),
            "location": booking.get("location"),
            "cancellation_reason": booking.get("cancellationReason"),
            "rescheduling_reason": booking.get("reschedulingReason"),
            "rescheduled_from_uid": booking.get("rescheduledFromUid"),
            "rescheduled_to_uid": booking.get("rescheduledToUid"),
            "hosts": hosts,
            "organizer": {
                "name": organizer.get("name"),
                "email": organizer.get("email"),
                "timezone": organizer.get("timeZone"),
            },
            "attendees": attendees,
            "guests": booking.get("guests") or [],
            "raw_booking": booking,
        }]


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
) -> CalcomWebhookTrigger:
    return CalcomWebhookTrigger(
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
    Owns the whole inbound-callback decision for Cal.com's webhook
    deliveries. If any of this app's declared triggers has a webhook_secret
    in its static config (ai_agents/calcom/plugin.json's triggers[].config --
    same idea as Gmail's topic_name / WhatsApp's verification_token), the
    delivery's X-Cal-Signature-256 header is verified against an HMAC-SHA256
    of the raw body using that secret (see _verify_calcom_signature()) before
    anything else runs; a mismatch rejects with 401. No secret configured
    means unverified, not rejected, since requiring one is a deployment
    decision this trigger can't enforce unilaterally.

    callback_unique_identifier is deterministic
    (cal_event_trigger_eventTypeId_calcomUserId), rebuilt from the body the
    same way subscribe() built it (see _build_callback_identifier()) -- one
    call handles all four calcom_* trigger_ids declared in plugin.json, since
    a single delivery's own triggerEvent field already picks out which one
    applies. trigger_defs is consulted only to confirm triggerEvent is one
    this app actually declares (config.cal_event_trigger); anything else
    can't have a matching subscription and is skipped without a platform
    lookup.

    Always acks 200 on a valid/unmatched delivery -- Cal.com retries webhook
    delivery on any non-2xx, so an unrecognized/malformed payload still gets
    acked rather than retried forever. A failed signature check is the one
    exception, returning 401 instead.
    """
    webhook_secret = next(
        ((t.config or {}).get("webhook_secret") for t in trigger_defs if (t.config or {}).get("webhook_secret")),
        None,
    )
    if webhook_secret:
        signature_header = headers.get("x-cal-signature-256") or headers.get("X-Cal-Signature-256")
        if not _verify_calcom_signature(webhook_secret, raw_body, signature_header):
            logger.warning("Calcom webhook signature validation failed")
            return {
                "status_code": 401,
                "response_body": {"error": "invalid X-Cal-Signature-256"},
                "response_content_type": "application/json",
                "callback_unique_identifiers": [],
            }

    try:
        callback_body = json.loads(raw_body) if raw_body else {}
    except ValueError:
        callback_body = {}
    if not isinstance(callback_body, dict):
        callback_body = {}

    logger.info("Calcom trigger callback received: app_code=%s body=%s", app_code, callback_body)
    parsed = _parse_calcom_webhook_body(callback_body)
    trigger_event, event_type_id, calcom_userid = (
        parsed["trigger_event"], parsed["event_type_id"], parsed["calcom_userid"],
    )

    known_cal_event_triggers = {
        (t.config or {}).get("cal_event_trigger")
        for t in trigger_defs
        if (t.config or {}).get("cal_event_trigger")
    }
    identifiers: List[str] = []
    if (
        trigger_event and trigger_event in known_cal_event_triggers
        and event_type_id is not None and calcom_userid is not None
    ):
        identifiers.append(_build_callback_identifier(trigger_event, event_type_id, calcom_userid))
    else:
        logger.warning(
            "Calcom handle_trigger_callback: could not derive identifier (trigger_event=%s event_type_id=%s calcom_userid=%s known=%s) from body=%s",
            trigger_event, event_type_id, calcom_userid, known_cal_event_triggers, callback_body,
        )

    return {
        "status_code": 200,
        "response_body": {},
        "response_content_type": "application/json",
        "callback_unique_identifiers": identifiers,
    }
