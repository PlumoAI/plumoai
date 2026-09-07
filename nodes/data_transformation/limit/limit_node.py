"""
Limit Node (node_id: limit, category: Data Transformation)

Deterministic action node that trims a list of items down to a maximum
count, taking either the first N or the last N entries.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from services.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)


def _extract_list(previous_output: Any) -> List[Any]:
    """Extract the list to limit from previous_output. No hardcoded schema beyond
    common container keys — "output"/"items" are workflow_executor_service's own
    convention (state.outputs[nid] == {"output": ..., "items": [...]}); the rest
    cover ad-hoc dict shapes when this node is invoked outside that engine."""
    if previous_output is None:
        return []
    if isinstance(previous_output, list):
        return previous_output
    if isinstance(previous_output, dict):
        for key in ("output", "items", "result", "data", "rows", "records"):
            v = previous_output.get(key)
            if isinstance(v, list):
                return v
        return [previous_output]
    return [previous_output]


class LimitNode(BaseNode):
    NODE_ID = "limit"
    NODE_NAME = "Limit"
    CATEGORY = "Data Transformation"
    DESCRIPTION = "Keep only the first or last N items of the incoming list."

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        items = _extract_list(previous_output)

        try:
            count = int(config.get("count"))
        except (TypeError, ValueError):
            return {
                "success": False,
                "result": None,
                "error": "Missing or invalid 'count' parameter. Specify how many items to keep.",
            }
        if count < 0:
            return {
                "success": False,
                "result": None,
                "error": "'count' must be zero or a positive integer.",
            }

        position = str(config.get("position") or "first").strip().lower()
        if position not in ("first", "last"):
            position = "first"

        limited = items[:count] if position == "first" else (items[-count:] if count > 0 else [])

        return {
            "success": True,
            "result": limited,
            "count": len(limited),
            "total_before": len(items),
            "position": position,
        }


__all__ = ["LimitNode"]
