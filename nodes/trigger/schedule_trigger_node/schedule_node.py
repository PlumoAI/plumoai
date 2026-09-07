"""
Schedule Trigger Node (node_id: trigger.schedule_trigger_node, category: Trigger Nodes)

Represents a workflow's time-based trigger in the nodes/ registry (node
catalog, config schema). The actual schedule is registered externally by
SchedulerAgentTool (llm_tools/scheduler_agent_tool.py) after the workflow is
saved, and workflow_executor_service.py's generic "trigger" node handling
fires runs from agent_context["workflow_trigger_output"] rather than calling
this node's execute() -- schedules have no live event payload to resolve
per-run the way a plugin-backed trigger (e.g. Gmail) does. execute() exists
so this node behaves sanely if invoked directly (e.g. a manual test run):
it simply surfaces its own configured schedule shape as output.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from services.nodes.base_node import BaseNode

_FREQUENCY_TO_FREQ = {
    "Minutely": "MINUTELY",
    "Hourly": "HOURLY",
    "Daily": "DAILY",
    "Weekly": "WEEKLY",
}

_DAY_TO_BYDAY = {
    "Monday": "MO",
    "Tuesday": "TU",
    "Wednesday": "WE",
    "Thursday": "TH",
    "Friday": "FR",
    "Saturday": "SA",
    "Sunday": "SU",
}


def _build_rrule(config: Dict[str, Any]) -> Optional[str]:
    """Join frequency/interval/byDay into the RFC5545 RRULE string
    scheduler_agent_tool.py's _parse_rrule expects (FREQ + INTERVAL + BYDAY)."""
    freq = _FREQUENCY_TO_FREQ.get(config.get("frequency"))
    if not freq:
        return None

    parts = [f"FREQ={freq}"]

    interval = config.get("interval")
    if isinstance(interval, int) and interval > 1:
        parts.append(f"INTERVAL={interval}")

    if freq == "WEEKLY":
        by_day = [_DAY_TO_BYDAY[d] for d in (config.get("byDay") or []) if d in _DAY_TO_BYDAY]
        if by_day:
            parts.append(f"BYDAY={','.join(by_day)}")

    return ";".join(parts)


class ScheduleTriggerNode(BaseNode):
    NODE_ID = "trigger.schedule_trigger_node"
    NODE_NAME = "Schedule Trigger"
    CATEGORY = "Trigger Nodes"
    DESCRIPTION = (
        "Starts the workflow on a one-time or recurring time-based schedule."
    )

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        schedule_type = config.get("scheduleType")
        if schedule_type not in ("One Time", "Recurring"):
            return {
                "success": False,
                "result": None,
                "error": "Missing or invalid 'scheduleType'. Expected \"One Time\" or \"Recurring\".",
            }

        result = dict(config)
        if schedule_type == "Recurring":
            rrule = _build_rrule(config)
            if not rrule:
                return {
                    "success": False,
                    "result": None,
                    "error": "Missing or invalid 'frequency'. Expected one of: "
                    + ", ".join(_FREQUENCY_TO_FREQ),
                }
            result["rrule"] = rrule

        return {"success": True, "result": result}


__all__ = ["ScheduleTriggerNode"]
