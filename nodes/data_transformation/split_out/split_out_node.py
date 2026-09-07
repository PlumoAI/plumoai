"""
Split Out Node (node_id: data_transformation.split_out, category: Data Transformation)

Deterministic action node that turns array field(s) inside each item into
separate items — one output item per array element.
"""

from __future__ import annotations

import logging
from itertools import zip_longest
from typing import Any, Dict, List

from services.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)

_MISSING = object()


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
            return _MISSING
    return value


def _set_path(obj: Dict[str, Any], path: str, value: Any, disable_dot_notation: bool) -> None:
    parts = [path] if disable_dot_notation else path.split(".")
    cursor = obj
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


class SplitOutNode(BaseNode):
    NODE_ID = "data_transformation.split_out"
    NODE_NAME = "Split Out"
    CATEGORY = "Data Transformation"
    DESCRIPTION = "Turn a list inside each item into separate items, one per array element."

    def _resolve_field_arrays(
        self, item: Dict[str, Any], fields: List[str], disable_dot_notation: bool
    ) -> List[List[Any]]:
        arrays: List[List[Any]] = []
        for field in fields:
            value = _get_path(item, field, disable_dot_notation)
            if value is _MISSING:
                arrays.append([])
            elif isinstance(value, list):
                arrays.append(value)
            else:
                # Non-array value: treat as a single-element array so a scalar
                # field still produces one row instead of erroring.
                arrays.append([value])
        return arrays

    def _build_other_fields(
        self,
        item: Dict[str, Any],
        fields: List[str],
        include: str,
        fields_to_include: List[str],
        disable_dot_notation: bool,
    ) -> Dict[str, Any]:
        if include == "allOtherFields":
            out = dict(item)
            for field in fields:
                if disable_dot_notation:
                    out.pop(field, None)
                else:
                    out.pop(field.split(".")[0], None)
            return out
        if include == "selectedOtherFields":
            out: Dict[str, Any] = {}
            for field in fields_to_include:
                value = _get_path(item, field, disable_dot_notation)
                if value is not _MISSING:
                    _set_path(out, field, value, disable_dot_notation)
            return out
        return {}

    def execute(
        self,
        node_outputs: Dict[str, Any],
        previous_output: Any,
        run_state: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        config = config or {}
        items = _extract_list(previous_output)

        fields = config.get("fieldsToSplitOut")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f.strip() for f in fields):
            return {
                "success": False,
                "result": None,
                "error": "Missing or invalid 'fieldsToSplitOut'. Specify at least one field path to split.",
            }
        fields = [f.strip() for f in fields]

        include = str(config.get("include") or "noOtherFields").strip()
        if include not in ("noOtherFields", "allOtherFields", "selectedOtherFields"):
            include = "noOtherFields"
        fields_to_include = config.get("fieldsToInclude") or []
        if not isinstance(fields_to_include, list):
            fields_to_include = []
        destination_field_name = (config.get("destinationFieldName") or "").strip()
        disable_dot_notation = bool(config.get("disableDotNotation") or False)

        output: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            arrays = self._resolve_field_arrays(item, fields, disable_dot_notation)
            if not any(arrays):
                continue
            other_fields = self._build_other_fields(
                item, fields, include, fields_to_include, disable_dot_notation
            )
            for row in zip_longest(*arrays, fillvalue=None):
                out_item = dict(other_fields)
                if len(fields) == 1 and destination_field_name:
                    out_item[destination_field_name] = row[0]
                else:
                    for field, value in zip(fields, row):
                        _set_path(out_item, field, value, disable_dot_notation)
                output.append(out_item)

        return {
            "success": True,
            "result": output,
            "count": len(output),
            "total_before": len(items),
            "fieldsToSplitOut": fields,
            "include": include,
        }


__all__ = ["SplitOutNode"]
