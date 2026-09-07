"""
Filter Node (node_id: data_transformation.filter, category: Data Transformation)

Deterministic action node that keeps only the items of a list that match
a structured condition ("if_else_node_conditions" — the same shape/evaluator
used by this workflow engine's if_else_node and switch_node).

Unlike if_else_node (which evaluates its condition once, using $json = the
previous node's first item, then branches the whole node true/false), Filter
must test EACH item individually. Its node.json declares
config.requiresRawInput = true, so workflow_executor_service.py skips its
usual once-per-node input resolution and hands this node the RAW,
unresolved conditions block (any "={{ ... }}" JS expressions still intact).
This node then re-resolves those expressions once per item — with $json set
to that specific item — before evaluating.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from services.nodes.base_node import BaseNode
from services.workflow_executor.if_else_evaluator import evaluate_conditions
from services.workflow_executor.js_expression_resolver import resolve_js_expressions

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


class FilterNode(BaseNode):
    NODE_ID = "data_transformation.filter"
    NODE_NAME = "Filter"
    CATEGORY = "Data Transformation"
    DESCRIPTION = "Keep only the items of the incoming list that match one or more field conditions."

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        items = _extract_list(previous_output)

        conditions_block = config.get("conditions")
        if not isinstance(conditions_block, dict):
            return {
                "success": False,
                "result": None,
                "error": "Missing or invalid 'conditions'. Expected a conditions object "
                "({combinator, conditions: [...]}).",
            }

        filtered: List[Any] = []
        for item in items:
            # Re-resolve any "={{ ... }}" JS expressions in this condition block
            # with $json bound to this specific item, so each item is tested
            # against its own field values rather than a single shared one.
            resolved_block = resolve_js_expressions(
                conditions_block, node_outputs, {"output": item}
            )
            matched, err = evaluate_conditions(resolved_block)
            if err:
                return {"success": False, "result": None, "error": err}
            if matched:
                filtered.append(item)

        return {
            "success": True,
            "result": filtered,
            "count": len(filtered),
            "total_before": len(items),
        }


__all__ = ["FilterNode"]
