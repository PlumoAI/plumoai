"""
Split In Batches Node (node_id: flow.split_in_batches, category: Flow)

A genuine loop controller, not a one-shot data transform: it is re-scheduled
through a real back edge in the workflow graph once per pass. Wire its
"loop" output through the loop body and back into this same node; wire its
"done" output to whatever should run once the loop finishes.

WIRING REQUIREMENT: this node has two incoming edges once wired into a loop
(the initial entry edge, and the loop-back edge from the end of the body).
workflow_executor_service.py's `edges_in` map keeps only the FIRST edge it
sees for a given target node (first-edge-wins) as `previous_output` for
template resolution — it does not distinguish "first pass" from "loop-back
pass". For that reason this node does NOT use `previous_output` to detect
which pass it's on, or to read what a loop-body pass produced; it uses
`run_state` (persisted across visits by workflow_executor_service.py,
keyed by this node's id) as the sole source of truth instead. Only the very
first pass reads `previous_output` at all, to seed the full item list.

Each pass:
- First pass (run_state is empty, or config.reset is true): read the full
  item list from `previous_output`, store it, and emit the first batch on
  "loop" (or go straight to "done" with an empty result if there are no
  items at all).
- Later passes: pop the next batch from the stored item list and emit it on
  "loop", or -- once every item has been emitted -- emit on "done" instead,
  ending the loop.
"""

from __future__ import annotations

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


class SplitInBatchesNode(BaseNode):
    NODE_ID = "flow.split_in_batches"
    NODE_NAME = "Split In Batches"
    CATEGORY = "Flow"
    DESCRIPTION = "Loop over the incoming items in fixed-size batches, one pass per batch."

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}

        try:
            batch_size = max(1, int(config.get("batchSize", 1)))
        except (TypeError, ValueError):
            return {
                "success": False,
                "result": None,
                "error": "Invalid 'batchSize'. Specify a positive integer.",
            }

        is_first_pass = bool(config.get("reset")) or "items" not in run_state
        if is_first_pass:
            items = _extract_list(previous_output)
            run_state.clear()
            run_state["items"] = items
            run_state["index"] = 0

        items = run_state["items"]
        index = run_state["index"]
        total = len(items)

        if index >= total:
            return {
                "success": True,
                "result": [],
                "route": "done",
                "totalItems": total,
            }

        batch = items[index:index + batch_size]
        run_state["index"] = index + batch_size

        return {
            "success": True,
            "result": batch,
            "route": "loop",
            "count": len(batch),
            "index": index,
            "totalItems": total,
            "isFirstPass": is_first_pass,
        }


__all__ = ["SplitInBatchesNode"]
