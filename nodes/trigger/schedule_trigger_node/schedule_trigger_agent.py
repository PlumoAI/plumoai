"""
ScheduleTriggerAgent -- subscribe()/execute_trigger() counterpart to
ScheduleTriggerNode (schedule_node.py), structured the same way plugin-backed
triggers (e.g. Gmail, via ai_agents/*/plugin.json + BaseTriggerAgent) are:
same subscribe(trigger_config)/execute_trigger(trigger_body, metadata) shape,
same register_workflow_trigger_callback() bookkeeping call.

It is NOT wired into services/ai_agents/trigger_registry.py or the
POST /api/agent-triggers/subscribe route -- those are built around connected
apps resolved via connectedAiAgentId -> app_code -> OAuth credentials, and a
schedule has none of that. This class exists so a caller (e.g. a future
workflow-save path) can drive scheduling through the identical
subscribe/execute_trigger interface without needing a fake connected app.

subscribe() delegates the actual schedule creation to SchedulerAgentTool's
existing _create_schedule_via_api (llm_tools/scheduler_agent_tool.py) instead
of re-implementing the REST call, loaded the same importlib way
tool_agent_factory.py already loads it (llm_tools/ is intentionally kept off
sys.path so plugin-based tool imports aren't polluted).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from typing import Any, Dict, List, Optional

from services.ai_agents.base_trigger_agent import BaseTriggerAgent

from .schedule_node import _FREQUENCY_TO_FREQ, _build_rrule

logger = logging.getLogger(__name__)


def _build_callback_identifier(workflow_id: Any, trigger_node_id: Any) -> str:
    return f"schedule_{workflow_id}_{trigger_node_id}"

_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_LLM_TOOLS_PATH = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "..", "llm_tools"))


def _load_scheduler_agent_tool_class() -> Optional[type]:
    try:
        from scheduler_agent_tool import SchedulerAgentTool  # type: ignore

        return SchedulerAgentTool
    except ImportError:
        pass
    try:
        module_path = os.path.join(_LLM_TOOLS_PATH, "scheduler_agent_tool.py")
        if not os.path.isfile(module_path):
            return None
        spec = importlib.util.spec_from_file_location("scheduler_agent_tool", module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        return module.SchedulerAgentTool
    except Exception:
        logger.exception("ScheduleTriggerAgent: failed to load SchedulerAgentTool")
        return None


class ScheduleTriggerAgent(BaseTriggerAgent):
    def __init__(
        self,
        *,
        token: Optional[str] = None,
        company_id: Optional[Any] = None,
        agent_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        trigger_node_id: Optional[str] = None,
        default_timezone: str = "UTC",
    ):
        self.token = token
        self.company_id = company_id
        self.agent_id = agent_id or ""
        self.app_code = "schedule"
        self.workflow_id = workflow_id
        self.trigger_node_id = trigger_node_id
        self.default_timezone = default_timezone

    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return "Fires a workflow on a one-time or recurring UTC schedule."

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        trigger_config = trigger_config or {}
        schedule_type_display = trigger_config.get("scheduleType")
        if schedule_type_display not in ("One Time", "Recurring"):
            return {
                "success": False,
                "error": "Missing or invalid 'scheduleType'. Expected \"One Time\" or \"Recurring\".",
            }
        schedule_type = "one_time" if schedule_type_display == "One Time" else "recurring"

        rrule = None
        if schedule_type == "recurring":
            rrule = _build_rrule(trigger_config)
            if not rrule:
                return {
                    "success": False,
                    "error": "Missing or invalid 'frequency'. Expected one of: "
                    + ", ".join(_FREQUENCY_TO_FREQ),
                }

        SchedulerAgentTool = _load_scheduler_agent_tool_class()
        if SchedulerAgentTool is None:
            return {"success": False, "error": "SchedulerAgentTool not available"}

        scheduler = SchedulerAgentTool(
            llm_provider=None,
            agent_id=self.agent_id,
            default_timezone=self.default_timezone,
            token=self.token,
            company_id=self.company_id,
        )

        # Build the payload the same way scheduler_agent_tool.py's own
        # _transform_to_schedule_payload does (reusing its instance methods
        # for exact parity), instead of a flat/simplified shape the schedules
        # API doesn't actually accept -- it expects nested schedule/execution
        # objects with startDate/nextRunAt computed server-side-equivalent.
        schedule = scheduler._default_schedule(schedule_type)
        schedule["endDate"] = trigger_config.get("endDate")
        time_of_day = trigger_config.get("timeOfDay")
        delay_minutes = trigger_config.get("delayMinutes")
        try:
            delay_minutes = int(delay_minutes) if delay_minutes is not None else None
        except (TypeError, ValueError):
            delay_minutes = None

        start_date_iso, next_run_iso = scheduler._compute_dates(
            schedule_type, rrule, schedule["timezone"], time_of_day, delay_minutes=delay_minutes,
        )
        schedule["startDate"] = start_date_iso

        if rrule:
            parsed = scheduler._parse_rrule(rrule)
            freq = (parsed.get("freq") or "WEEKLY").upper()
            interval = parsed.get("interval", 1)
            if freq == "MINUTELY":
                schedule["rrule"] = f"FREQ=MINUTELY;INTERVAL={interval}"
            elif freq == "HOURLY":
                schedule["rrule"] = f"FREQ=HOURLY;INTERVAL={interval}"
            elif freq == "DAILY":
                schedule["rrule"] = f"FREQ=DAILY;INTERVAL={interval}" if interval > 1 else "DAILY"
            else:
                schedule["rrule"] = "WEEKLY"
        else:
            schedule["rrule"] = None

        execution = scheduler._default_execution(schedule_type)
        execution["nextRunAt"] = next_run_iso
        max_runs = trigger_config.get("maxRuns")
        try:
            max_runs = int(max_runs) if max_runs is not None else None
        except (TypeError, ValueError):
            max_runs = None
        if max_runs is not None and max_runs > 0:
            execution["maxRuns"] = max_runs

        payload: Dict[str, Any] = {
            "agentId": self.agent_id,
            "status": "active",
            "action": {
                "type": "run_workflow",
                "workflow_id": self.workflow_id,
                "trigger_node_id": self.trigger_node_id,
            },
            "action_description": "One-time workflow run" if schedule_type == "one_time" else "Recurring workflow run",
            "payload": {},
            "schedule": schedule,
            "execution": execution,
        }

        # This trigger node's schedule is identified by a callback identifier
        # stable across calls (workflow_id/trigger_node_id) -- unlike the
        # resulting scheduleId, which doesn't exist yet before the first
        # create. Look it up first so a re-subscribe (e.g. re-saving the
        # workflow) UPDATEs the existing schedule instead of creating a
        # duplicate one every time. Same pattern as
        # ai_agents/plumoai/trigger_entrypoint.py's automation_id reuse.
        callback_unique_identifier = _build_callback_identifier(self.workflow_id, self.trigger_node_id)
        existing_schedule_id: Optional[Any] = None
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
                    existing_schedule_id = raw_metadata.get("scheduleId")

        ok = False
        message = ""
        data = None
        schedule_id: Optional[Any] = None
        if existing_schedule_id:
            logger.info(
                "ScheduleTriggerAgent.subscribe: updating existing schedule %s: %s",
                existing_schedule_id, payload,
            )
            ok, message, data = await scheduler._update_schedule_via_api(str(existing_schedule_id), payload)
            schedule_id = existing_schedule_id if ok else None
            if not ok and message == "Schedule not found":
                # The saved scheduleId no longer exists server-side (e.g.
                # deleted externally) -- fall back to creating a new one
                # instead of failing the whole subscribe.
                logger.info(
                    "ScheduleTriggerAgent.subscribe: schedule %s not found, creating new one instead",
                    existing_schedule_id,
                )
                existing_schedule_id = None

        if not existing_schedule_id and not ok:
            logger.info("ScheduleTriggerAgent.subscribe: POSTing new schedule payload: %s", payload)
            ok, message, data = await scheduler._create_schedule_via_api(payload)
            schedule_id = (data.get("_id") or data.get("id")) if isinstance(data, dict) else None

        logger.info(
            "ScheduleTriggerAgent.subscribe: schedules API response: ok=%s message=%s data=%s",
            ok, message, data,
        )
        if not ok:
            return {"success": False, "error": message}

        await self.register_workflow_trigger_callback(
            callback_unique_identifier=callback_unique_identifier,
            metadata={"scheduleId": schedule_id, **trigger_config},
        )

        return {"success": True, "scheduleId": schedule_id, "message": message}

    async def execute_trigger(
        self, trigger_body: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        # No live event payload of its own -- workflow_executor_service.py's
        # generic "trigger" node handling already falls back to the
        # pre-supplied workflow_trigger_output for schedule-kind triggers.
        # This just normalizes whatever the caller already has into a list.
        if isinstance(trigger_body, list):
            return trigger_body
        if trigger_body is None:
            return []
        return [trigger_body]


__all__ = ["ScheduleTriggerAgent"]
