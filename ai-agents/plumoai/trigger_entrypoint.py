from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent

logger = logging.getLogger(__name__)

_AUTOMATIONS_TRIGGER_URL = "http://plumoai-api:3010/api/v1/automations/trigger"

# Internal record_events values ("created"/"updated") -> the literal event
# strings the automations/trigger API expects.
_EVENT_LABELS = {
    "created": "on record created",
    "updated": "on record updated",
}

# When a trigger's config.record_events covers both created and updated (the
# plumoai_record_created_or_updated trigger), the platform treats that as one
# combined event type rather than two separate ones -- so subscribe() makes a
# single automations/trigger call for it instead of one call per event.
_COMBINED_EVENT_KEY = "created_or_updated"
_COMBINED_EVENT_LABEL = "on record added or updated"


async def _call_automations_trigger_api(
    *,
    token: Optional[str],
    company_id: Optional[Any],
    workflow_id: Optional[str],
    trigger_node_id: Optional[str],
    ai_employee_id: Any,
    table_id: Any,
    event: str,
    automation_id: Optional[str] = None,
    label: str = "PlumoAI Trigger",
) -> Dict[str, Any]:
    """
    POST {AUTH_URL}/api/v1/automations/trigger -- the platform API that
    actually registers (automation_id omitted -- Create) or updates
    (automation_id given -- Update) a record-events automation, scoped to
    ai_employee_id/table_id and bound to this exact workflow_id/node_id. This is
    what makes the platform start watching for record events on that
    AI Employee/table and, eventually, deliver them to this trigger's callback
    endpoint -- same role as Gmail's users.watch call or WhatsApp's
    subscribed_apps call, just for PlumoAI's own record events.
    """
    if not token:
        return {"success": False, "error": "No platform token available to call automations/trigger"}

    body: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "node_id": trigger_node_id,
        # automations/trigger's project scoping is keyed on "project_id", not
        # "ai_employee_id" -- an AI Employee IS a project on the platform side
        # (see entrypoint.py's ai_employee_list mapping: ai_employee_id ==
        # project_id), just presented under a different name to this trigger's
        # own input_schema/param_options.
        "project_id": ai_employee_id,
        "ai_employee_id": ai_employee_id,
        "table_id": table_id,
        "event": event,
        "label": label,
    }
    if automation_id:
        body["automation_id"] = automation_id

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "companyId": json.dumps([str(company_id)]) if company_id is not None else "[]",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_AUTOMATIONS_TRIGGER_URL, headers=headers, json=body)
    except httpx.RequestError as e:
        logger.warning("Plumoai automations/trigger request failed (event=%s): %s", event, e)
        return {"success": False, "error": f"Request to automations/trigger failed: {e}"}

    try:
        parsed = resp.json() if resp.content else None
    except ValueError:
        parsed = None

    logger.info(
        "Plumoai automations/trigger response: status=%s event=%s automation_id=%s workflow_id=%s node_id=%s body=%s",
        resp.status_code, event, automation_id, workflow_id, trigger_node_id,
        parsed if parsed is not None else resp.text[:1000],
    )

    if resp.status_code not in (200, 201):
        return {
            "success": False,
            "error": f"automations/trigger returned {resp.status_code}: {resp.text[:500]}",
            "status_code": resp.status_code,
            "raw_response": parsed,
        }

    data = parsed if isinstance(parsed, dict) else {}
    inner = data.get("data")
    if isinstance(inner, dict):
        data = inner
    returned_automation_id = data.get("automation_id") or data.get("automationId") or data.get("id")

    return {"success": True, "automation_id": returned_automation_id, "data": data, "raw_response": parsed}


def _first(body: Dict[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        v = body.get(k)
        if v not in (None, ""):
            return v
    return None


def _build_callback_identifier(ai_employee_id: Any, table_id: Any) -> str:
    return f"plumoai_record_{ai_employee_id}_{table_id}"


def _parse_record_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes one inbound record-event payload item, tolerating either
    snake_case or camelCase field names -- the exact casing the platform's
    record-events webhook uses isn't fixed by anything in this repo, same
    situation as Cal.com's nested/flat payload shapes.
    """
    event = _first(item, "event", "record_event", "eventType", "event_type")
    event = str(event).strip().lower() if event else None
    # Tolerate a few natural spellings for the same two events.
    if event in ("record_created", "record.created", "created"):
        event = "created"
    elif event in ("record_updated", "record.updated", "updated"):
        event = "updated"
    return {
        "event": event,
        "ai_employee_id": _first(item, "ai_employee_id", "aiEmployeeId"),
        "table_id": _first(item, "table_id", "tableId"),
        "record_id": _first(item, "record_id", "recordId"),
    }


def _parse_record_webhook_body(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    A single delivery may batch more than one record event -- tolerate both a
    single-event body and a {"events": [...]} batch envelope.
    """
    body = body or {}
    raw_events = body.get("events")
    if isinstance(raw_events, list):
        return [_parse_record_event(e) for e in raw_events if isinstance(e, dict)]
    return [_parse_record_event(body)]


def _verify_signature(secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header:
        return False
    header = signature_header.strip()
    if header.startswith("sha256="):
        header = header.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class PlumoaiRecordTrigger(ConnectedServiceTriggerAgent):
    """
    One class backs all three plumoai_record_* trigger_ids declared in
    plugin.json (created / updated / created_or_updated) -- which record
    event(s) it cares about comes from trigger_config["record_events"] (the
    event-selection field a user picks on the trigger node) when supplied,
    else falls back to trigger_static_config["record_events"] (this
    trigger_id's own default in plugin.json, e.g. plumoai_record_created
    defaults to just "created"). Neither is hardcoded here.

    subscribe() calls the platform's own /api/v1/automations/trigger API
    once per event_spec derived from record_events -- "on record created" or
    "on record updated" for the single-event triggers, or one combined "on
    record added or updated" call for the created_or_updated trigger (the
    platform treats that as one event type, not two) -- each bound to this
    exact workflow_id/trigger_node_id. This is what makes the platform start
    watching record events for ai_employee_id/table_id and later deliver them
    straight to execute_trigger(), same role as Gmail's users.watch call or
    WhatsApp's subscribed_apps call.

    Because each event already gets its own automation_id bound to this
    node at subscribe() time, delivery is pre-scoped by the platform itself
    -- execute_trigger() doesn't need to inspect an "event" field (the real
    payload doesn't carry one). The delivered trigger_body only carries
    record_id/ai_employee_id/table_id though, so execute_trigger() fetches
    the full record via the detailed_record MCP tool before returning it.
    callback_unique_identifier (scoped to ai_employee_id/table_id only) is
    still registered separately via register_workflow_trigger_callback(),
    purely so subscribe() can find and update an existing automation instead
    of creating a duplicate one on a repeat save -- it plays no role in
    routing the automations/trigger delivery itself, and execute_trigger()
    does not use it for dedup (see execute_trigger()'s own note on why a
    dedup guard isn't possible with this payload shape).
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
            "Registers a workflow trigger callback for PlumoAI's own record created/updated "
            "events on a selected AI Employee/table, assuming the platform's record-events webhook "
            "is already configured to call this trigger's callback endpoint."
        )

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        trigger_config = trigger_config or {}

        # record_events is user-selectable per trigger node (input_schema's
        # "record_events" enum array in plugin.json) -- when the user picks
        # event(s) on the trigger node, that selection wins; otherwise fall
        # back to this trigger_id's own default from plugin.json's static
        # config (e.g. plumoai_record_created defaults to just "created").
        selected_events = trigger_config.get("record_events") or trigger_config.get("recordEvents")
        if isinstance(selected_events, str):
            selected_events = [selected_events]
        if isinstance(selected_events, list):
            record_events = [e for e in selected_events if e in ("created", "updated")]
        else:
            record_events = []
        if not record_events:
            record_events = self.static_config.get("record_events") or []
        if not record_events:
            return {"success": False, "error": "trigger is missing config.record_events in ai_agents/plumoai/plugin.json"}

        ai_employee_id = _first(trigger_config, "ai_employee_id", "aiEmployeeId")
        table_id = _first(trigger_config, "table_id", "tableId")
        if not ai_employee_id or not table_id:
            return {"success": False, "error": "trigger_config.ai_employee_id and trigger_config.table_id are required"}

        callback_unique_identifier = _build_callback_identifier(ai_employee_id, table_id)

        # automation_id(s) are platform-assigned bookkeeping, not something a
        # subscribe() caller would ever know or pass in -- so the only source
        # of truth for "does this AI Employee/table already have a platform
        # automation for this exact workflow_id/trigger_node_id" is whatever
        # was saved in that callback's own metadata on a prior subscribe()
        # call. Look it up by callback_unique_identifier (workflow_id/
        # trigger_node_id are auto-filled from self by
        # fetch_workflow_trigger_callbacks, scoping the match to this exact
        # node) and reuse its automation_id(s) so this call UPDATEs the
        # existing platform automation instead of creating a duplicate one.
        existing_automation_id: Optional[Any] = None
        existing_automation_ids: Dict[str, Any] = {}
        fetch_result = await self.fetch_workflow_trigger_callbacks(
            callback_unique_identifier=callback_unique_identifier,
        )
        if fetch_result.get("success"):
            raw = fetch_result.get("raw_response")
            match = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else None)
            if match:
                raw_metadata = match.get("metadata")
                if isinstance(raw_metadata, str):
                    try:
                        raw_metadata = json.loads(raw_metadata) if raw_metadata.strip() else {}
                    except ValueError:
                        raw_metadata = {}
                if isinstance(raw_metadata, dict):
                    existing_automation_ids = raw_metadata.get("automation_ids") or {}
                    existing_automation_id = raw_metadata.get("automation_id")
        logger.info(
            "Plumoai subscribe: looked up existing callback callback_unique_identifier=%s workflow_id=%s trigger_node_id=%s -> "
            "automation_id=%s automation_ids=%s",
            callback_unique_identifier, self.workflow_id, self.trigger_node_id,
            existing_automation_id, existing_automation_ids,
        )

        automation_ids: Dict[str, Any] = {}
        automation_responses: Dict[str, Any] = {}
        if set(record_events) == {"created", "updated"}:
            event_specs = [(_COMBINED_EVENT_KEY, _COMBINED_EVENT_LABEL)]
        else:
            event_specs = [(e, _EVENT_LABELS.get(e, f"on record {e}")) for e in record_events]

        # record_events is now user-selectable per subscribe() call (the
        # trigger node's own event-selection field), so the key a prior
        # subscribe() stored an automation_id under (e.g.
        # "created_or_updated") won't match this call's event_specs keys
        # when the user changes their selection (e.g. down to just
        # "created"). This node only ever has ONE desired automation at a
        # time though (record_events collapses to a single event_spec: one
        # event, or the combined pair) -- so reuse whichever automation_id
        # was already registered for this node regardless of its old key,
        # UPDATEing it to the newly selected event instead of leaving it
        # registered under the stale event and creating an orphaned second
        # automation that keeps firing on the old selection.
        reusable_automation_ids = list(existing_automation_ids.values())
        if existing_automation_id and existing_automation_id not in reusable_automation_ids:
            reusable_automation_ids.append(existing_automation_id)
        reusable_automation_ids = [a for a in reusable_automation_ids if a]

        for idx, (event, event_label) in enumerate(event_specs):
            automation_id = existing_automation_ids.get(event)
            if not automation_id and idx < len(reusable_automation_ids):
                automation_id = reusable_automation_ids[idx]
            api_result = await _call_automations_trigger_api(
                token=self.token,
                company_id=self.company_id,
                workflow_id=self.workflow_id,
                trigger_node_id=self.trigger_node_id,
                ai_employee_id=ai_employee_id,
                table_id=table_id,
                event=event_label,
                automation_id=automation_id,
            )
            if not api_result.get("success"):
                return {
                    "success": False,
                    "error": f"automations/trigger failed for event '{event_label}': {api_result.get('error')}",
                }
            automation_ids[event] = api_result.get("automation_id")
            # response.data for this event (e.g. {"automation_id": ...,
            # "workflow_id": ..., "node_id": ...}) -- kept in the callback's
            # metadata, not just the automation_id, so it's inspectable
            # later without re-calling the API.
            automation_responses[event] = api_result.get("data")

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=callback_unique_identifier,
            metadata={
                "ai_employee_id": ai_employee_id,
                "table_id": table_id,
                "record_events": record_events,
                "automation_ids": automation_ids,
                "automation_responses": automation_responses,
                "callback_unique_identifier": callback_unique_identifier,
                "last_processed_key": None,
            },
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"Platform automation registered but workflow trigger callback registration failed: {callback_result.get('error')}",
                "callback_unique_identifier": callback_unique_identifier,
                "automation_ids": automation_ids,
                "automation_responses": automation_responses,
            }

        return {
            "success": True,
            "callback_unique_identifier": callback_unique_identifier,
            "automation_ids": automation_ids,
            "automation_responses": automation_responses,
        }

    async def _fetch_detailed_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """
        Calls the plumoai MCP tool detailed_record(record_id=...) directly
        (bypassing the LLM decision loop), same pattern as entrypoint.py's
        build_param_options -- the callback delivery only carries
        record_id/ai_employee_id/table_id, not the record's actual field
        values, so execute_trigger needs this to hand downstream nodes the
        real record content.
        """
        from backend.services.tool_agent_factory import ToolAgentFactory

        agent = await ToolAgentFactory._create_mcp_agent(
            app_code="plumoai",
            app_config=self.app_config or {},
            llm_provider=None,
            token=self.token,
            user_id=self.user_id,
            company_id=self.company_id,
            agent_id=None,
        )
        if agent is None:
            logger.warning("Plumoai execute_trigger: could not create MCP agent to fetch detailed_record")
            return None
        try:
            raw = await agent._call_tool("detailed_record", {"record_id": record_id})
        except Exception as e:
            logger.warning("Plumoai execute_trigger: detailed_record call failed: %s", e)
            return None
        finally:
            cleanup = getattr(agent, "cleanup", None)
            if callable(cleanup):
                try:
                    await cleanup()
                except Exception as e:
                    logger.warning("Plumoai execute_trigger: agent cleanup failed: %s", e)

        if not raw.get("success"):
            logger.warning("Plumoai execute_trigger: detailed_record returned error: %s", raw.get("error"))
            return None

        for block in raw.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            data = parsed.get("data") if isinstance(parsed, dict) else None
            if isinstance(data, dict):
                return data
        return None

    async def execute_trigger(
        self, trigger_body: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Called by the workflow engine when this trigger node runs. The
        callback delivery only carries record_id/ai_employee_id/table_id, not
        the record's field values, so this fetches the full record via the
        detailed_record MCP tool (record_id) and returns that as the node's
        output -- falling back to trigger_body as-is if the fetch fails, so a
        transient API error doesn't stall the workflow entirely.
        """
        metadata = metadata or {}
        trigger_body = trigger_body or {}
        logger.info(
            "Plumoai execute_trigger: start trigger_node_id=%s workflow_id=%s trigger_body=%s metadata=%s",
            self.trigger_node_id, self.workflow_id, trigger_body, metadata,
        )

        ai_employee_id = metadata.get("ai_employee_id")
        table_id = metadata.get("table_id")
        body_ai_employee_id = _first(trigger_body, "ai_employee_id", "aiEmployeeId")
        body_table_id = _first(trigger_body, "table_id", "tableId")
        if (ai_employee_id and body_ai_employee_id and body_ai_employee_id != ai_employee_id) or (
            table_id and body_table_id and body_table_id != table_id
        ):
            logger.info(
                "Plumoai execute_trigger: trigger_body ai_employee_id/table_id (%s/%s) does not match this node's scope (%s/%s) -- ignoring",
                body_ai_employee_id, body_table_id, ai_employee_id, table_id,
            )
            return []

        # NOTE: no duplicate-delivery guard here. The delivered trigger_body
        # only ever carries record_id/ai_employee_id/table_id -- all static
        # per record -- with no per-event field (no timestamp, no delivery
        # id) to tell two genuinely different events on the same record
        # apart. A hash-of-body dedup key would therefore match on the first
        # delivery and then incorrectly swallow every subsequent real event
        # for that record forever. Without a real per-event identifier in the
        # payload, correct dedup isn't possible here, so this trades
        # (rare) duplicate re-deliveries for never silently dropping real ones.

        record_id = _first(trigger_body, "record_id", "recordId")
        if not record_id:
            logger.warning("Plumoai execute_trigger: no record_id in trigger_body -- returning trigger_body as-is")
            return [trigger_body]

        detailed = await self._fetch_detailed_record(record_id)
        if detailed is None:
            logger.warning("Plumoai execute_trigger: detailed_record fetch failed for record_id=%s -- falling back to trigger_body", record_id)
            return [trigger_body]

        logger.info("Plumoai execute_trigger: returning detailed_record for record_id=%s", record_id)
        return [detailed]


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
) -> PlumoaiRecordTrigger:
    return PlumoaiRecordTrigger(
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
    Owns the whole inbound-callback decision for the PlumoAI platform's own
    record-events webhook. If any of this app's declared triggers has a
    webhook_secret in its static config (ai_agents/plumoai/plugin.json's
    triggers[].config -- same idea as Cal.com's webhook_secret), the delivery's
    X-Plumoai-Signature-256 header is verified against an HMAC-SHA256 of the
    raw body using that secret before anything else runs; a mismatch rejects
    with 401. No secret configured means unverified, not rejected, since
    requiring one is a deployment decision this trigger can't enforce
    unilaterally (config.webhook_secret ships empty until the platform side
    assigns a real one).

    callback_unique_identifier is scoped to (ai_employee_id, table_id) only, not
    the event -- see PlumoaiRecordTrigger's class docstring for why. A single
    delivery can batch events for more than one AI Employee/table.
    """
    webhook_secret = next(
        ((t.config or {}).get("webhook_secret") for t in trigger_defs if (t.config or {}).get("webhook_secret")),
        None,
    )
    if webhook_secret:
        signature_header = headers.get("x-plumoai-signature-256") or headers.get("X-Plumoai-Signature-256")
        if not _verify_signature(webhook_secret, raw_body, signature_header):
            logger.warning("Plumoai record-events webhook signature validation failed")
            return {
                "status_code": 401,
                "response_body": {"error": "invalid X-Plumoai-Signature-256"},
                "response_content_type": "application/json",
                "callback_unique_identifiers": [],
            }

    try:
        callback_body = json.loads(raw_body) if raw_body else {}
    except ValueError:
        callback_body = {}
    if not isinstance(callback_body, dict):
        callback_body = {}

    logger.info("Plumoai trigger callback received: app_code=%s body=%s", app_code, callback_body)
    parsed_events = _parse_record_webhook_body(callback_body)
    identifiers = sorted({
        _build_callback_identifier(e["ai_employee_id"], e["table_id"])
        for e in parsed_events
        if e.get("ai_employee_id") and e.get("table_id")
    })
    if not identifiers:
        logger.warning("Plumoai handle_trigger_callback: could not derive any identifier from body=%s", callback_body)

    return {
        "status_code": 200,
        "response_body": {},
        "response_content_type": "application/json",
        "callback_unique_identifiers": identifiers,
    }
