"""
JavaScript Node (node_id: code.javascript, category: Code)

Runs user-authored JavaScript against the incoming items, either once for
the whole list ("runOnceForAllItems") or once per item ("runOnceForEachItem").

Reuses the same embedded V8 engine (STPyV8) and JS-global bridging already
built for expression fields in services/workflow_executor/js_expression_resolver.py
(the $(nodeId)/$json/$now globals + the JSObject/JSArray -> plain Python
conversion) instead of reimplementing a second V8 context builder — this
node just adds the $input global on top.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from services.nodes.base_node import BaseNode
from services.workflow_executor.js_expression_resolver import (
    STPyV8,
    V8UnavailableError,
    _build_context,
    _js_to_py,
    run_on_v8,
)

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


def _unwrap_json(value: Any) -> Any:
    """If the returned object is wrapped as {"json": {...}} (the shape items
    are exposed in via $input), unwrap it so the output item is the plain object."""
    if isinstance(value, dict) and set(value.keys()) <= {"json"} and "json" in value:
        return value["json"]
    return value


def _install_input_global(ctxt: Any, all_items: List[Any], current_item: Any) -> None:
    """Register $input.all()/.item/.first()/.last() for this execution."""

    def _dollar_input_all() -> str:
        return json.dumps([{"json": it} for it in all_items], default=str, ensure_ascii=False)

    def _dollar_input_item() -> str:
        return json.dumps({"json": current_item}, default=str, ensure_ascii=False)

    ctxt.locals["__input_all_json"] = _dollar_input_all
    ctxt.locals["__input_item_json"] = _dollar_input_item
    ctxt.eval(
        """
        var $input = (function () {
            var all = JSON.parse(__input_all_json());
            return {
                all: function () { return all; },
                item: JSON.parse(__input_item_json()),
                first: function () { return all.length ? all[0] : undefined; },
                last: function () { return all.length ? all[all.length - 1] : undefined; },
            };
        })();
        """
    )


class JavaScriptNode(BaseNode):
    NODE_ID = "code.javascript"
    NODE_NAME = "JavaScript"
    CATEGORY = "Code"
    DESCRIPTION = "Run custom JavaScript against the incoming items to transform, filter, or generate data."

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        code = config.get("code")
        if not isinstance(code, str) or not code.strip():
            return {
                "success": False,
                "result": None,
                "error": "Missing 'code'. Provide JavaScript source to run.",
            }

        if STPyV8 is None:
            return {
                "success": False,
                "result": None,
                "error": "JavaScript engine (STPyV8) is not available in this environment.",
            }

        mode = str(config.get("mode") or "runOnceForAllItems").strip()
        if mode not in ("runOnceForAllItems", "runOnceForEachItem"):
            mode = "runOnceForAllItems"

        items = _extract_list(previous_output)
        wrapped_code = f"(function() {{\n{code}\n}})()"

        def _run_all_items(ctxt: Any) -> Any:
            _build_context(ctxt, node_outputs, previous_output)
            _install_input_global(ctxt, items, items[0] if items else None)
            return _js_to_py(ctxt.eval(wrapped_code))

        def _run_each_item(ctxt: Any) -> List[Any]:
            results = []
            for item in items:
                _build_context(ctxt, node_outputs, {"output": item})
                _install_input_global(ctxt, items, item)
                results.append(_js_to_py(ctxt.eval(wrapped_code)))
            return results

        try:
            # MUST go through run_on_v8() — never create a JSContext directly here.
            # A second, independent STPyV8 context alongside the one in
            # js_expression_resolver.py is exactly what caused the production
            # SIGSEGV crash-loop; see run_on_v8()'s docstring for the full story.
            if mode == "runOnceForAllItems":
                per_run_results: Any = run_on_v8(_run_all_items)
            else:
                per_run_results = run_on_v8(_run_each_item)
        except V8UnavailableError as e:
            logger.error("[code.javascript] cannot execute: %s", e)
            return {
                "success": False,
                "result": None,
                "error": f"JavaScript engine unavailable: {e}",
            }
        except Exception as e:
            logger.warning("[code.javascript] execution failed: %s", e)
            return {
                "success": False,
                "result": None,
                "error": f"JavaScript execution error: {e}",
            }

        if mode == "runOnceForAllItems":
            if per_run_results is None:
                out_items: List[Any] = []
            elif isinstance(per_run_results, list):
                out_items = [_unwrap_json(v) for v in per_run_results]
            else:
                out_items = [_unwrap_json(per_run_results)]
        else:
            out_items = [
                _unwrap_json(v) for v in per_run_results if v is not None and v is not False
            ]

        return {
            "success": True,
            "result": out_items,
            "count": len(out_items),
            "total_before": len(items),
            "mode": mode,
        }


__all__ = ["JavaScriptNode"]
