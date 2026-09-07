"""
Remove Duplicates Node (node_id: data_transformation.remove_duplicates,
category: Data Transformation)

Deterministic action node that removes items from a list that duplicate an
earlier item, comparing either every field, every field except a chosen
subset, or only a chosen subset of fields. The first occurrence of each
distinct value is kept; later duplicates are dropped.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from services.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)


def _extract_list(previous_output: Any) -> List[Any]:
    """"output"/"items" are workflow_executor_service's own convention
    (state.outputs[nid] == {"output": ..., "items": [...]}); the rest cover
    ad-hoc dict shapes when this node is invoked outside that engine."""
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


def _get_path(obj: Any, path: str, disable_dot_notation: bool) -> Any:
    parts = [path] if disable_dot_notation else path.split(".")
    value = obj
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _signature(value: Any) -> str:
    """Deterministic, order-independent string signature for equality comparison."""
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except TypeError:
        return str(value)


class RemoveDuplicatesNode(BaseNode):
    NODE_ID = "data_transformation.remove_duplicates"
    NODE_NAME = "Remove Duplicates"
    CATEGORY = "Data Transformation"
    DESCRIPTION = "Remove items from the incoming list that duplicate an earlier item, comparing all fields or a chosen subset."

    def _comparison_key(
        self,
        item: Any,
        compare: str,
        fields: List[str],
        disable_dot_notation: bool,
    ) -> str:
        if not isinstance(item, dict) or compare == "allFields":
            return _signature(item)
        if compare == "allFieldsExceptSelected":
            excluded = {f if disable_dot_notation else f.split(".")[0] for f in fields}
            subset = {k: v for k, v in item.items() if k not in excluded}
            return _signature(subset)
        if compare == "selectedFields":
            subset = {f: _get_path(item, f, disable_dot_notation) for f in fields}
            return _signature(subset)
        return _signature(item)

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        items = _extract_list(previous_output)

        compare = str(config.get("compare") or "allFields").strip()
        if compare not in ("allFields", "allFieldsExceptSelected", "selectedFields"):
            compare = "allFields"

        fields = config.get("fieldsToCompare") or []
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            fields = []
        fields = [f.strip() for f in fields if f and f.strip()]

        if compare in ("allFieldsExceptSelected", "selectedFields") and not fields:
            return {
                "success": False,
                "result": None,
                "error": f"compare='{compare}' requires at least one field in 'fieldsToCompare'.",
            }

        disable_dot_notation = bool(config.get("disableDotNotation") or False)

        seen: set = set()
        deduped: List[Any] = []
        for item in items:
            key = self._comparison_key(item, compare, fields, disable_dot_notation)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return {
            "success": True,
            "result": deduped,
            "count": len(deduped),
            "total_before": len(items),
            "removed": len(items) - len(deduped),
            "compare": compare,
        }


__all__ = ["RemoveDuplicatesNode"]
