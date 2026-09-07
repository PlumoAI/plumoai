"""
Google Sheets functions class for functions_wrapper plugin.

Each public @tool method maps to one or more Google Sheets REST API v4 actions.
Reference: https://developers.google.com/workspace/sheets/api/guides/concepts
           https://developers.google.com/workspace/sheets/api/reference/rest

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].

Notation notes used throughout this file:
- A1 notation: "SheetName!A1:C10" — a sheet name, a '!', then a cell range using
  column letters (A, B, ... Z, AA, AB, ...) and 1-based row numbers. A bare
  "SheetName" refers to the whole sheet; "A1:C10" without a sheet name refers
  to the first visible sheet.
- GridRange (used by batchUpdate requests): a 0-based, end-exclusive range
  expressed as sheetId/startRowIndex/endRowIndex/startColumnIndex/endColumnIndex.
  This file accepts 1-based, end-inclusive row/column numbers (or column
  letters) from the caller and converts them to GridRange internally via
  _row_start/_row_end/_col_start/_col_end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0

ColumnRef = Union[int, str]


def _missing_params(**kwargs: Any) -> List[str]:
    """Return the names of any required parameters whose value is None, in call order."""
    return [name for name, value in kwargs.items() if value is None]


def _missing_params_error(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """If any of the given required parameters is None, return an error dict naming them; else None."""
    missing = _missing_params(**kwargs)
    if missing:
        return {"success": False, "response": f"Missing required parameter(s): {', '.join(missing)}."}
    return None


def _api_error_detail(data: Optional[Dict]) -> Optional[str]:
    """Extract a human-readable error message from a Sheets API error response, if any."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            status = err.get("status")
            if message and status:
                return f"{message} ({status})"
            return message or status
    return None


def _is_error(data: Optional[Dict]) -> bool:
    """True if data represents a failed Sheets API request (no data, or a Google error body)."""
    return data is None or _api_error_detail(data) is not None


def _error_response(data: Optional[Dict], fallback: str, hint: str = "") -> Dict[str, Any]:
    """Build a {"success": False, "response": ...} error dict that surfaces the real Google API
    error message (if any) alongside a fallback summary and an optional hint for what to try next."""
    detail = _api_error_detail(data)
    msg = f"{fallback} Google API error: {detail}" if detail else fallback
    if hint:
        msg = f"{msg} {hint}"
    return {"success": False, "response": msg}


class GoogleSheetsFunctions(ConnectedServiceToolAgent):
    """
    Google Sheets tool functions. Each @tool method is a Sheets API v4 capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Google Sheets: create spreadsheets; read/write/append/clear cell values "
        "(single range or batch, A1 notation); manage sheets/tabs, rows/columns, "
        "cell formatting, merges, sorting, find & replace, filters, conditional "
        "formatting, protected ranges, named ranges, and developer metadata."
    )

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        user_id: Optional[int] = None,
        company_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_config: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> None:
        super().__init__(
            token=token,
            company_id=company_id,
            user_id=user_id,
            app_config=app_config,
        )
        self.agent_id = agent_id or ""
        self._httpx_client: Optional[httpx.AsyncClient] = None
        # Set by FunctionsWrapperAgentTool before each tool call
        self._current_query: str = ""
        self._step_results: List[Dict] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("GoogleSheetsFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GoogleSheetsFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GoogleSheetsFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def _sheets_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{SHEETS_API_BASE}{path}" if path.startswith("/") else f"{SHEETS_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._sheets_request(
                method, path, json_body=json_body, params=params, retry_401=False,
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "Sheets API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._sheets_request(
                method, path, json_body=json_body, params=params,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("Sheets API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("Sheets API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            try:
                error_data = r.json()
            except Exception:
                error_data = None
            if isinstance(error_data, dict) and isinstance(error_data.get("error"), dict):
                return error_data
            return {"error": {"code": r.status_code, "message": (r.text or "")[:500] or f"HTTP {r.status_code}", "status": "UNKNOWN"}}
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Sheets API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    async def _batch_update(self, spreadsheet_id: str, requests: List[Dict]) -> Optional[Dict]:
        return await self._sheets_request(
            "POST", f"/{spreadsheet_id}:batchUpdate", json_body={"requests": requests},
        )

    # ------------------------------------------------------------------
    # @tool public methods — Spreadsheets
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Extract the spreadsheet ID from a Google Sheets URL. Every other tool needs the "
            "spreadsheet_id (not the full URL) — use this first when the user gives you a link "
            "such as 'https://docs.google.com/spreadsheets/d/1A2B3C.../edit#gid=123456789'. "
            "Also extracts the sheet/tab gid from the URL fragment if present."
        ),
        params={
            "url": (
                "A Google Sheets URL, e.g. "
                "'https://docs.google.com/spreadsheets/d/1A2B3C4D5E6F7G8H9I0J/edit#gid=123456789'. "
                "Also accepts a bare spreadsheet ID, which is returned as-is. Required."
            ),
        },
    )
    async def get_spreadsheet_id_from_url(self, url: str) -> Dict:
        if err := _missing_params_error(url=url):
            return err
        spreadsheet_id = _extract_spreadsheet_id(url)
        if not spreadsheet_id:
            return {"success": False, "response": "Could not find a spreadsheet ID in that URL."}
        gid = _extract_gid(url)
        response = f"spreadsheet_id = {spreadsheet_id}"
        if gid is not None:
            response += f", gid (sheet tab id) = {gid}"
        response += (
            ". Next: call get_spreadsheet(spreadsheet_id=...) to see its sheets/tabs, or "
            "get_values/update_values(spreadsheet_id=..., range='SheetName!A1:D10') to read or write cells."
        )
        return {
            "success": True,
            "response": response,
            "spreadsheet_id": spreadsheet_id,
            "gid": gid,
        }

    @tool(
        description=(
            "Create a new Google Sheets spreadsheet, optionally with one or more named sheets/tabs. "
            "Returns the new spreadsheet's ID (needed for all other tools), title, and URL."
        ),
        params={
            "title": "Title for the new spreadsheet. Example: 'Q3 Budget'. Required.",
            "sheet_titles": (
                "Optional list of tab/sheet names to create, e.g. ['Summary', 'Raw Data']. "
                "If omitted, the spreadsheet is created with a single default sheet named 'Sheet1'."
            ),
            "locale": "Optional locale for the spreadsheet, e.g. 'en_US'.",
            "time_zone": "Optional IANA time zone for the spreadsheet, e.g. 'America/New_York'.",
        },
    )
    async def create_spreadsheet(
        self,
        title: str,
        sheet_titles: Optional[List[str]] = None,
        locale: Optional[str] = None,
        time_zone: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(title=title):
            return err
        properties: Dict[str, Any] = {"title": title}
        if locale:
            properties["locale"] = locale
        if time_zone:
            properties["timeZone"] = time_zone
        body: Dict[str, Any] = {"properties": properties}
        if sheet_titles:
            body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles if t]
        data = await self._sheets_request("POST", "", json_body=body)
        if _is_error(data) or not data.get("spreadsheetId"):
            return _error_response(data, "Could not create the spreadsheet.")
        return {
            "success": True,
            "response": (
                f"Created spreadsheet '{title}' (id={data.get('spreadsheetId')}). URL: {data.get('spreadsheetUrl')}. "
                "Next: use this spreadsheet_id with update_values/append_values to write data, or get_spreadsheet "
                "to confirm the sheet/tab names and IDs."
            ),
            "spreadsheet_id": data.get("spreadsheetId"),
            "spreadsheet_url": data.get("spreadsheetUrl"),
            "sheets": [_sheet_summary(s) for s in data.get("sheets") or []],
        }

    @tool(
        description=(
            "Get metadata for a Google Sheets spreadsheet: its title, list of sheets/tabs "
            "(with sheetId, title, index, row/column counts, frozen rows/columns), named "
            "ranges, and (optionally) cell data for given ranges."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet (from its URL: .../spreadsheets/d/{spreadsheet_id}/edit). Required.",
            "ranges": (
                "Optional list of A1-notation ranges to limit the response to, e.g. "
                "['Sheet1!A1:D10', 'Summary']. If omitted, returns metadata for the whole spreadsheet."
            ),
            "include_grid_data": (
                "If true, includes the actual cell data (values, formatting, formulas) for the "
                "requested ranges. This can be very large — only set true when cell-level detail "
                "is needed and ranges is also set. Default false."
            ),
            "fields": (
                "Optional partial response field mask (Google API fields syntax) to reduce response "
                "size, e.g. 'spreadsheetId,properties.title,sheets.properties'."
            ),
        },
    )
    async def get_spreadsheet(
        self,
        spreadsheet_id: str,
        ranges: Optional[List[str]] = None,
        include_grid_data: Optional[bool] = None,
        fields: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id):
            return err
        params: Dict[str, Any] = {}
        if ranges:
            params["ranges"] = ranges
        if include_grid_data is not None:
            params["includeGridData"] = bool(include_grid_data)
        if fields:
            params["fields"] = fields
        data = await self._sheets_request("GET", f"/{spreadsheet_id}", params=params or None)
        if _is_error(data) or not data:
            return _error_response(data, "Could not get spreadsheet metadata.", "Check that spreadsheet_id is correct (use get_spreadsheet_id_from_url if you only have a URL) and that this account has access to it.")
        sheets = [_sheet_summary(s) for s in data.get("sheets") or []]
        title = (data.get("properties") or {}).get("title")
        return {
            "success": True,
            "response": (
                f"Spreadsheet '{title}' has {len(sheets)} sheet(s): "
                + ", ".join(f"{s['title']} (id={s['sheetId']})" for s in sheets)
                + ". Next: use one of these sheet titles in A1 notation, e.g. "
                + (f"get_values(spreadsheet_id=..., range='{sheets[0]['title']}!A1:D10')" if sheets else "get_values(spreadsheet_id=..., range='SheetName!A1:D10')")
                + " to read cell values, or update_values to write them. Use each sheet's numeric sheetId "
                "(shown above) for structural tools like add_sheet, merge_cells, sort_range, etc."
            ),
            "spreadsheet": data,
            "sheets": sheets,
        }

    @tool(
        description=(
            "Apply one or more raw batchUpdate request objects to a spreadsheet in a single atomic "
            "operation. This is the low-level, full-power escape hatch for any structural change "
            "not covered by a dedicated tool (e.g. adding charts, banded ranges, pivot tables, "
            "data validation rules, text-to-columns, or any combination of changes that must be "
            "applied together). Each item in requests must be a single-key object matching the "
            "Sheets API Request schema, e.g. {\"addSheet\": {\"properties\": {\"title\": \"New Tab\"}}}. "
            "Prefer the dedicated tools (add_sheet, merge_cells, sort_range, etc.) when they cover "
            "your need — use this only for advanced/uncommon operations."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet to update. Required.",
            "requests": (
                "List of raw Sheets API request objects, applied in order, all-or-nothing. "
                "Example: [{\"addSheet\": {\"properties\": {\"title\": \"New Tab\"}}}, "
                "{\"updateSheetProperties\": {\"properties\": {\"sheetId\": 0, \"title\": \"Renamed\"}, "
                "\"fields\": \"title\"}}]. Required."
            ),
        },
    )
    async def batch_update(self, spreadsheet_id: str, requests: List[Dict]) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, requests=requests):
            return err
        data = await self._batch_update(spreadsheet_id, requests)
        if _is_error(data):
            return _error_response(data, "batchUpdate failed.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Applied {len(requests)} request(s).", "result": data}

    @tool(
        description=(
            "Update spreadsheet-level properties: title, locale, time zone, default cell "
            "formatting, or auto-recalculation settings."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet to update. Required.",
            "title": "New title for the spreadsheet.",
            "locale": "New locale, e.g. 'en_US'.",
            "time_zone": "New IANA time zone, e.g. 'America/New_York'.",
            "auto_recalc": (
                "Recalculation frequency for volatile functions. One of: "
                "'ON_CHANGE', 'MINUTE', 'HOUR'."
            ),
        },
    )
    async def update_spreadsheet_properties(
        self,
        spreadsheet_id: str,
        title: Optional[str] = None,
        locale: Optional[str] = None,
        time_zone: Optional[str] = None,
        auto_recalc: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id):
            return err
        props: Dict[str, Any] = {}
        fields: List[str] = []
        if title is not None:
            props["title"] = title
            fields.append("title")
        if locale is not None:
            props["locale"] = locale
            fields.append("locale")
        if time_zone is not None:
            props["timeZone"] = time_zone
            fields.append("timeZone")
        if auto_recalc is not None:
            props["autoRecalc"] = auto_recalc
            fields.append("autoRecalc")
        if not props:
            return {"success": False, "response": "Provide at least one property to update."}
        data = await self._batch_update(spreadsheet_id, [{
            "updateSpreadsheetProperties": {"properties": props, "fields": ",".join(fields)},
        }])
        if _is_error(data):
            return _error_response(data, "Could not update spreadsheet properties.", "Check that spreadsheet_id is correct (use get_spreadsheet_id_from_url if you only have a URL) and that this account has access to it.")
        return {"success": True, "response": "Spreadsheet properties updated.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Values
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Read cell values from a single range of a spreadsheet, using A1 notation "
            "(e.g. 'Sheet1!A1:D10', 'Sheet1!A:A' for a whole column, or 'Sheet1' for all data)."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "range": "A1-notation range to read, e.g. 'Sheet1!A1:D10'. Required.",
            "value_render_option": (
                "How values should be represented in the output. One of: "
                "'FORMATTED_VALUE' (default — values as displayed, e.g. '$1.00'), "
                "'UNFORMATTED_VALUE' (raw values without formatting, e.g. 1), "
                "'FORMULA' (formula text, e.g. '=A1+B1', instead of the computed value)."
            ),
            "date_time_render_option": (
                "How dates/times should be represented. One of: "
                "'SERIAL_NUMBER' (default — days since the spreadsheet epoch as a number), "
                "'FORMATTED_STRING' (formatted as displayed). Only applies when "
                "value_render_option is 'UNFORMATTED_VALUE'."
            ),
            "major_dimension": (
                "Whether the returned 'values' array is organized by 'ROWS' (default — each "
                "inner array is one row) or 'COLUMNS' (each inner array is one column)."
            ),
        },
    )
    async def get_values(
        self,
        spreadsheet_id: str,
        range: str,
        value_render_option: Optional[str] = None,
        date_time_render_option: Optional[str] = None,
        major_dimension: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, range=range):
            return err
        params: Dict[str, Any] = {}
        if value_render_option:
            params["valueRenderOption"] = value_render_option
        if date_time_render_option:
            params["dateTimeRenderOption"] = date_time_render_option
        if major_dimension:
            params["majorDimension"] = major_dimension
        data = await self._sheets_request("GET", f"/{spreadsheet_id}/values/{quote(range, safe='')}", params=params or None)
        if _is_error(data):
            return _error_response(data, "Could not read values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range.")
        values = data.get("values") or []
        response = f"Read {len(values)} row(s) from {data.get('range', range)}."
        if not values:
            response += (
                " The range is empty. If this is unexpected, call get_spreadsheet(spreadsheet_id=...) to "
                "confirm the sheet/tab name and that data exists at this range."
            )
        else:
            response += " To write back, call update_values with the same or another range."
        return {
            "success": True,
            "response": response,
            "range": data.get("range", range),
            "major_dimension": data.get("majorDimension", "ROWS"),
            "values": values,
        }

    @tool(
        description=(
            "Read cell values from multiple ranges of a spreadsheet in one call. "
            "More efficient than calling get_values repeatedly."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "ranges": "List of A1-notation ranges to read, e.g. ['Sheet1!A1:B5', 'Summary!A1:A1']. Required.",
            "value_render_option": "Same as get_values: 'FORMATTED_VALUE' (default), 'UNFORMATTED_VALUE', or 'FORMULA'.",
            "date_time_render_option": "Same as get_values: 'SERIAL_NUMBER' (default) or 'FORMATTED_STRING'.",
            "major_dimension": "Same as get_values: 'ROWS' (default) or 'COLUMNS'.",
        },
    )
    async def batch_get_values(
        self,
        spreadsheet_id: str,
        ranges: List[str],
        value_render_option: Optional[str] = None,
        date_time_render_option: Optional[str] = None,
        major_dimension: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, ranges=ranges):
            return err
        params: Dict[str, Any] = {"ranges": ranges}
        if value_render_option:
            params["valueRenderOption"] = value_render_option
        if date_time_render_option:
            params["dateTimeRenderOption"] = date_time_render_option
        if major_dimension:
            params["majorDimension"] = major_dimension
        data = await self._sheets_request("GET", f"/{spreadsheet_id}/values:batchGet", params=params)
        if _is_error(data):
            return _error_response(data, "Could not read values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range.")
        value_ranges = data.get("valueRanges") or []
        return {
            "success": True,
            "response": f"Read {len(value_ranges)} range(s). To write back, call batch_update_values or update_values.",
            "value_ranges": value_ranges,
        }

    @tool(
        description=(
            "Write cell values into a range of a spreadsheet, overwriting whatever is currently "
            "there. The values array is a list of rows, where each row is a list of cell values "
            "(strings, numbers, booleans, or formulas like '=SUM(A1:A10)'). "
            "The write starts at the top-left cell of range; if values is larger than range, "
            "it expands beyond range; if smaller, only the provided cells are overwritten."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "range": "A1-notation range identifying the top-left starting cell, e.g. 'Sheet1!A1'. Required.",
            "values": (
                "2D list of cell values, e.g. [['Name', 'Score'], ['Alice', 90], ['Bob', 85]]. "
                "To write a formula, use a string starting with '=', e.g. '=SUM(B2:B3)'. Required."
            ),
            "value_input_option": (
                "How input data should be interpreted. One of: "
                "'USER_ENTERED' (default — values are parsed as if typed by a user, so "
                "'1/2/2026' becomes a date and '=A1+1' becomes a formula), "
                "'RAW' (values are stored exactly as-is, with no parsing — numbers and formulas "
                "are stored as plain text strings)."
            ),
            "major_dimension": "Whether values is organized by 'ROWS' (default, each inner list is one row) or 'COLUMNS'.",
        },
    )
    async def update_values(
        self,
        spreadsheet_id: str,
        range: str,
        values: List[List[Any]],
        value_input_option: Optional[str] = None,
        major_dimension: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, range=range, values=values):
            return err
        body: Dict[str, Any] = {"range": range, "values": values}
        if major_dimension:
            body["majorDimension"] = major_dimension
        params = {"valueInputOption": value_input_option or "USER_ENTERED"}
        data = await self._sheets_request("PUT", f"/{spreadsheet_id}/values/{quote(range, safe='')}", json_body=body, params=params)
        if _is_error(data):
            return _error_response(data, "Could not update values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range. After a successful write, call get_values on the same range to verify the new values.")
        return {
            "success": True,
            "response": (
                f"Updated {data.get('updatedCells', 0)} cell(s) in {data.get('updatedRange', range)}. "
                "Next: call get_values with the same range to verify the written values."
            ),
            "result": data,
        }

    @tool(
        description=(
            "Write multiple separate ranges of cell values in one atomic call. "
            "More efficient than calling update_values repeatedly when updating several "
            "non-contiguous ranges."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "data": (
                "List of {range, values} objects, e.g. "
                "[{\"range\": \"Sheet1!A1\", \"values\": [[\"Name\"], [\"Alice\"]]}, "
                "{\"range\": \"Sheet1!C1\", \"values\": [[\"Score\"], [90]]}]. Required."
            ),
            "value_input_option": "Same as update_values: 'USER_ENTERED' (default) or 'RAW'.",
        },
    )
    async def batch_update_values(
        self,
        spreadsheet_id: str,
        data: List[Dict[str, Any]],
        value_input_option: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, data=data):
            return err
        body = {"valueInputOption": value_input_option or "USER_ENTERED", "data": data}
        result = await self._sheets_request("POST", f"/{spreadsheet_id}/values:batchUpdate", json_body=body)
        if _is_error(result):
            return _error_response(result, "Could not update values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with corrected A1 ranges. After a successful write, call batch_get_values or get_values to verify the new values.")
        return {
            "success": True,
            "response": (
                f"Updated {result.get('totalUpdatedCells', 0)} cell(s) across {result.get('totalUpdatedSheets', 0)} sheet(s). "
                "Next: call batch_get_values or get_values on the same ranges to verify the written values."
            ),
            "result": result,
        }

    @tool(
        description=(
            "Append a new row (or rows) of values after the last row of data found within a range. "
            "Google Sheets automatically finds the first empty row below the existing table and "
            "writes the new data there — ideal for adding records to a growing list/log without "
            "needing to know the current row count."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "range": (
                "A1-notation range identifying the table to append to, e.g. 'Sheet1!A1:D1' or "
                "just 'Sheet1'. The API searches for the existing table within this range and "
                "appends after its last row. Required."
            ),
            "values": "2D list of cell values to append, e.g. [['Alice', 90, 'Pass']] for one new row. Required.",
            "value_input_option": "Same as update_values: 'USER_ENTERED' (default, parses dates/formulas) or 'RAW'.",
            "insert_data_option": (
                "How the new data should be inserted. One of: "
                "'OVERWRITE' (default — overwrite existing data in the rows after the table), "
                "'INSERT_ROWS' (insert new rows for the data, shifting existing data down)."
            ),
            "major_dimension": "Whether values is organized by 'ROWS' (default) or 'COLUMNS'.",
        },
    )
    async def append_values(
        self,
        spreadsheet_id: str,
        range: str,
        values: List[List[Any]],
        value_input_option: Optional[str] = None,
        insert_data_option: Optional[str] = None,
        major_dimension: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, range=range, values=values):
            return err
        body: Dict[str, Any] = {"range": range, "values": values}
        if major_dimension:
            body["majorDimension"] = major_dimension
        params: Dict[str, Any] = {"valueInputOption": value_input_option or "USER_ENTERED"}
        if insert_data_option:
            params["insertDataOption"] = insert_data_option
        data = await self._sheets_request("POST", f"/{spreadsheet_id}/values/{quote(range, safe='')}:append", json_body=body, params=params)
        if _is_error(data):
            return _error_response(data, "Could not append values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range.")
        updates = data.get("updates") or {}
        return {
            "success": True,
            "response": (
                f"Appended {updates.get('updatedCells', 0)} cell(s) at {updates.get('updatedRange', range)}. "
                "Next: call get_values on the returned updatedRange to verify the appended row(s)."
            ),
            "result": data,
        }

    @tool(
        description=(
            "Clear all values from a single range, leaving the cells empty but preserving "
            "formatting, formulas references that point elsewhere, and merges."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "range": "A1-notation range to clear, e.g. 'Sheet1!A1:D10'. Required.",
        },
    )
    async def clear_values(self, spreadsheet_id: str, range: str) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, range=range):
            return err
        data = await self._sheets_request("POST", f"/{spreadsheet_id}/values/{quote(range, safe='')}:clear", json_body={})
        if _is_error(data):
            return _error_response(data, "Could not clear values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range.")
        return {"success": True, "response": f"Cleared {data.get('clearedRange', range)}.", "result": data}

    @tool(
        description="Clear values from multiple ranges in one atomic call.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "ranges": "List of A1-notation ranges to clear, e.g. ['Sheet1!A1:D10', 'Summary!A1:A1']. Required.",
        },
    )
    async def batch_clear_values(self, spreadsheet_id: str, ranges: List[str]) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, ranges=ranges):
            return err
        data = await self._sheets_request("POST", f"/{spreadsheet_id}/values:batchClear", json_body={"ranges": ranges})
        if _is_error(data):
            return _error_response(data, "Could not clear values.", "If this is an 'Unable to parse range' or similar error, call get_spreadsheet(spreadsheet_id=...) to see the real sheet/tab names and dimensions, then retry with a corrected A1 range.")
        return {"success": True, "response": f"Cleared {len(data.get('clearedRanges') or [])} range(s).", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Sheets (tabs)
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Add a new sheet (tab) to a spreadsheet. Returns the new sheet's sheetId "
            "(needed for most other structural tools), title, and index."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "title": "Title for the new sheet/tab, e.g. 'April'. Required.",
            "index": "Zero-based position for the new sheet among the spreadsheet's tabs. If omitted, it is added at the end.",
            "row_count": "Number of rows to give the new sheet's grid. Default 1000.",
            "column_count": "Number of columns to give the new sheet's grid. Default 26.",
            "sheet_type": "Type of sheet to create: 'GRID' (default, normal data sheet) or 'OBJECT' (chart sheet).",
            "frozen_rows": "Number of rows to freeze at the top of the new sheet.",
            "frozen_columns": "Number of columns to freeze at the left of the new sheet.",
            "tab_color": "Tab color as a hex string, e.g. '#FF0000'.",
            "right_to_left": "If true, the sheet's layout is right-to-left. Default false.",
        },
    )
    async def add_sheet(
        self,
        spreadsheet_id: str,
        title: str,
        index: Optional[int] = None,
        row_count: Optional[int] = None,
        column_count: Optional[int] = None,
        sheet_type: Optional[str] = None,
        frozen_rows: Optional[int] = None,
        frozen_columns: Optional[int] = None,
        tab_color: Optional[str] = None,
        right_to_left: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, title=title):
            return err
        props: Dict[str, Any] = {"title": title}
        if index is not None:
            props["index"] = int(index)
        if sheet_type:
            props["sheetType"] = sheet_type
        if right_to_left is not None:
            props["rightToLeft"] = bool(right_to_left)
        if tab_color:
            props["tabColor"] = _hex_to_color(tab_color)
        grid_props: Dict[str, Any] = {}
        if row_count is not None:
            grid_props["rowCount"] = int(row_count)
        if column_count is not None:
            grid_props["columnCount"] = int(column_count)
        if frozen_rows is not None:
            grid_props["frozenRowCount"] = int(frozen_rows)
        if frozen_columns is not None:
            grid_props["frozenColumnCount"] = int(frozen_columns)
        if grid_props:
            props["gridProperties"] = grid_props
        data = await self._batch_update(spreadsheet_id, [{"addSheet": {"properties": props}}])
        if _is_error(data) or not data.get("replies"):
            return _error_response(data, "Could not add the sheet.", "Check that spreadsheet_id is correct (use get_spreadsheet_id_from_url if you only have a URL) and that this account has access to it. Also ensure title is not already used by another sheet.")
        new_sheet = (data["replies"][0].get("addSheet") or {}).get("properties") or {}
        return {
            "success": True,
            "response": (
                f"Added sheet '{new_sheet.get('title')}' (sheetId={new_sheet.get('sheetId')}). "
                f"Next: use range='{new_sheet.get('title')}!A1' with update_values/append_values to write data, "
                f"or sheet_id={new_sheet.get('sheetId')} with structural tools (merge_cells, sort_range, etc.)."
            ),
            "sheet": _sheet_props_summary(new_sheet),
        }

    @tool(
        description="Delete a sheet/tab from a spreadsheet. This action cannot be undone.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to delete (from get_spreadsheet or add_sheet). Required.",
        },
    )
    async def delete_sheet(self, spreadsheet_id: str, sheet_id: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id):
            return err
        data = await self._batch_update(spreadsheet_id, [{"deleteSheet": {"sheetId": int(sheet_id)}}])
        if _is_error(data):
            return _error_response(data, "Could not delete the sheet.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Sheet {sheet_id} deleted.", "sheet_id": sheet_id}

    @tool(
        description=(
            "Duplicate an existing sheet/tab within the same spreadsheet, including its data "
            "and formatting. Returns the new sheet's sheetId, title, and index."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "source_sheet_id": "The numeric sheetId of the sheet to duplicate. Required.",
            "new_sheet_name": "Title for the duplicated sheet. If omitted, Sheets generates a name like 'Copy of Sheet1'.",
            "insert_index": "Zero-based position for the new sheet. If omitted, it is placed immediately after the source sheet.",
        },
    )
    async def duplicate_sheet(
        self,
        spreadsheet_id: str,
        source_sheet_id: int,
        new_sheet_name: Optional[str] = None,
        insert_index: Optional[int] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, source_sheet_id=source_sheet_id):
            return err
        req: Dict[str, Any] = {"sourceSheetId": int(source_sheet_id)}
        if new_sheet_name:
            req["newSheetName"] = new_sheet_name
        if insert_index is not None:
            req["insertSheetIndex"] = int(insert_index)
        data = await self._batch_update(spreadsheet_id, [{"duplicateSheet": req}])
        if _is_error(data) or not data.get("replies"):
            return _error_response(data, "Could not duplicate the sheet.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        new_sheet = (data["replies"][0].get("duplicateSheet") or {}).get("properties") or {}
        return {
            "success": True,
            "response": f"Duplicated sheet {source_sheet_id} as '{new_sheet.get('title')}' (sheetId={new_sheet.get('sheetId')}).",
            "sheet": _sheet_props_summary(new_sheet),
        }

    @tool(
        description=(
            "Update properties of an existing sheet/tab: rename it, move it, resize its grid, "
            "freeze rows/columns, hide it, change its tab color, or change its right-to-left layout."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to update. Required.",
            "title": "New title for the sheet/tab.",
            "index": "New zero-based position among the spreadsheet's tabs.",
            "row_count": "New number of rows in the sheet's grid. Reducing this deletes data in removed rows.",
            "column_count": "New number of columns in the sheet's grid. Reducing this deletes data in removed columns.",
            "frozen_rows": "New number of frozen rows at the top.",
            "frozen_columns": "New number of frozen columns at the left.",
            "hidden": "If true, hides the sheet/tab. If false, makes it visible.",
            "tab_color": "New tab color as a hex string, e.g. '#00FF00'.",
            "right_to_left": "If true, sets the sheet's layout to right-to-left.",
        },
    )
    async def update_sheet_properties(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        title: Optional[str] = None,
        index: Optional[int] = None,
        row_count: Optional[int] = None,
        column_count: Optional[int] = None,
        frozen_rows: Optional[int] = None,
        frozen_columns: Optional[int] = None,
        hidden: Optional[bool] = None,
        tab_color: Optional[str] = None,
        right_to_left: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id):
            return err
        props: Dict[str, Any] = {"sheetId": int(sheet_id)}
        fields: List[str] = []
        if title is not None:
            props["title"] = title
            fields.append("title")
        if index is not None:
            props["index"] = int(index)
            fields.append("index")
        if hidden is not None:
            props["hidden"] = bool(hidden)
            fields.append("hidden")
        if tab_color is not None:
            props["tabColor"] = _hex_to_color(tab_color)
            fields.append("tabColor")
        if right_to_left is not None:
            props["rightToLeft"] = bool(right_to_left)
            fields.append("rightToLeft")
        grid_props: Dict[str, Any] = {}
        if row_count is not None:
            grid_props["rowCount"] = int(row_count)
            fields.append("gridProperties.rowCount")
        if column_count is not None:
            grid_props["columnCount"] = int(column_count)
            fields.append("gridProperties.columnCount")
        if frozen_rows is not None:
            grid_props["frozenRowCount"] = int(frozen_rows)
            fields.append("gridProperties.frozenRowCount")
        if frozen_columns is not None:
            grid_props["frozenColumnCount"] = int(frozen_columns)
            fields.append("gridProperties.frozenColumnCount")
        if grid_props:
            props["gridProperties"] = grid_props
        if not fields:
            return {"success": False, "response": "Provide at least one property to update."}
        data = await self._batch_update(spreadsheet_id, [{
            "updateSheetProperties": {"properties": props, "fields": ",".join(fields)},
        }])
        if _is_error(data):
            return _error_response(data, "Could not update the sheet properties.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Sheet {sheet_id} updated.", "result": data}

    @tool(
        description=(
            "Copy a sheet/tab from this spreadsheet into a different destination spreadsheet. "
            "The copy is added as a new sheet in the destination."
        ),
        params={
            "spreadsheet_id": "The ID of the source spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to copy. Required.",
            "destination_spreadsheet_id": "The ID of the spreadsheet to copy the sheet into. Required.",
        },
    )
    async def copy_sheet_to(self, spreadsheet_id: str, sheet_id: int, destination_spreadsheet_id: str) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, destination_spreadsheet_id=destination_spreadsheet_id):
            return err
        data = await self._sheets_request(
            "POST", f"/{spreadsheet_id}/sheets/{int(sheet_id)}:copyTo",
            json_body={"destinationSpreadsheetId": destination_spreadsheet_id},
        )
        if _is_error(data) or not data:
            return _error_response(data, "Could not copy the sheet.", "Check that spreadsheet_id, sheet_id, and destination_spreadsheet_id are all correct and accessible \u2014 use get_spreadsheet on each spreadsheet to confirm.")
        return {
            "success": True,
            "response": f"Copied sheet {sheet_id} to spreadsheet {destination_spreadsheet_id} as '{data.get('title')}' (sheetId={data.get('sheetId')}).",
            "sheet": data,
        }

    # ------------------------------------------------------------------
    # @tool public methods — Rows / Columns (dimensions)
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Insert new, empty rows or columns into a sheet at a given position, shifting "
            "existing rows/columns down or right."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to modify. Required.",
            "dimension": "Either 'ROWS' or 'COLUMNS'. Required.",
            "start_index": "1-based position before which to insert. E.g. start_index=2 inserts before row/column 2 (so it becomes the new row/column 2). Required.",
            "count": "Number of rows/columns to insert. Default 1.",
            "inherit_from_before": (
                "If true, new rows/columns inherit formatting from the row/column before the "
                "insertion point; if false (default), they inherit from the row/column after. "
                "Cannot be true if start_index is 1 (the first row/column)."
            ),
        },
    )
    async def insert_dimensions(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        dimension: str,
        start_index: int,
        count: Optional[int] = None,
        inherit_from_before: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, dimension=dimension, start_index=start_index):
            return err
        n = int(count) if count else 1
        req: Dict[str, Any] = {
            "range": {
                "sheetId": int(sheet_id),
                "dimension": dimension.upper(),
                "startIndex": int(start_index) - 1,
                "endIndex": int(start_index) - 1 + n,
            },
        }
        if inherit_from_before is not None:
            req["inheritFromBefore"] = bool(inherit_from_before)
        data = await self._batch_update(spreadsheet_id, [{"insertDimension": req}])
        if _is_error(data):
            return _error_response(data, "Could not insert dimensions.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Inserted {n} {dimension.lower()} before position {start_index}.", "result": data}

    @tool(
        description="Delete a range of rows or columns from a sheet entirely, shifting remaining rows/columns up or left.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to modify. Required.",
            "dimension": "Either 'ROWS' or 'COLUMNS'. Required.",
            "start_index": "1-based first row/column number to delete (inclusive). Required.",
            "end_index": "1-based last row/column number to delete (inclusive). Required.",
        },
    )
    async def delete_dimensions(self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, end_index: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, dimension=dimension, start_index=start_index, end_index=end_index):
            return err
        req = {
            "range": {
                "sheetId": int(sheet_id),
                "dimension": dimension.upper(),
                "startIndex": int(start_index) - 1,
                "endIndex": int(end_index),
            },
        }
        data = await self._batch_update(spreadsheet_id, [{"deleteDimension": req}])
        if _is_error(data):
            return _error_response(data, "Could not delete dimensions.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Deleted {dimension.lower()} {start_index}-{end_index}.", "result": data}

    @tool(
        description="Resize rows or columns to a fixed pixel size, or hide/show them.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to modify. Required.",
            "dimension": "Either 'ROWS' or 'COLUMNS'. Required.",
            "start_index": "1-based first row/column number to update (inclusive). Required.",
            "end_index": "1-based last row/column number to update (inclusive). Required.",
            "pixel_size": "New size in pixels for each row/column in the range.",
            "hidden": "If true, hides the rows/columns; if false, shows them.",
        },
    )
    async def update_dimension_properties(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        dimension: str,
        start_index: int,
        end_index: int,
        pixel_size: Optional[int] = None,
        hidden: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, dimension=dimension, start_index=start_index, end_index=end_index):
            return err
        props: Dict[str, Any] = {}
        fields: List[str] = []
        if pixel_size is not None:
            props["pixelSize"] = int(pixel_size)
            fields.append("pixelSize")
        if hidden is not None:
            props["hiddenByUser"] = bool(hidden)
            fields.append("hiddenByUser")
        if not fields:
            return {"success": False, "response": "Provide pixel_size and/or hidden."}
        req = {
            "range": {
                "sheetId": int(sheet_id),
                "dimension": dimension.upper(),
                "startIndex": int(start_index) - 1,
                "endIndex": int(end_index),
            },
            "properties": props,
            "fields": ",".join(fields),
        }
        data = await self._batch_update(spreadsheet_id, [{"updateDimensionProperties": req}])
        if _is_error(data):
            return _error_response(data, "Could not update dimension properties.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Updated {dimension.lower()} {start_index}-{end_index}.", "result": data}

    @tool(
        description="Automatically resize rows or columns to fit their content.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to modify. Required.",
            "dimension": "Either 'ROWS' or 'COLUMNS'. Required.",
            "start_index": "1-based first row/column number to auto-resize (inclusive). Required.",
            "end_index": "1-based last row/column number to auto-resize (inclusive). Required.",
        },
    )
    async def auto_resize_dimensions(self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, end_index: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, dimension=dimension, start_index=start_index, end_index=end_index):
            return err
        req = {
            "dimensions": {
                "sheetId": int(sheet_id),
                "dimension": dimension.upper(),
                "startIndex": int(start_index) - 1,
                "endIndex": int(end_index),
            },
        }
        data = await self._batch_update(spreadsheet_id, [{"autoResizeDimensions": req}])
        if _is_error(data):
            return _error_response(data, "Could not auto-resize dimensions.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Auto-resized {dimension.lower()} {start_index}-{end_index}.", "result": data}

    @tool(
        description="Move a contiguous block of rows or columns to a different position within the same sheet.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to modify. Required.",
            "dimension": "Either 'ROWS' or 'COLUMNS'. Required.",
            "start_index": "1-based first row/column number of the block to move (inclusive). Required.",
            "end_index": "1-based last row/column number of the block to move (inclusive). Required.",
            "destination_index": (
                "1-based row/column number the block should be moved to (the position it will "
                "occupy before the move's shifting is applied). Required."
            ),
        },
    )
    async def move_dimension(self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, end_index: int, destination_index: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, dimension=dimension, start_index=start_index, end_index=end_index, destination_index=destination_index):
            return err
        req = {
            "source": {
                "sheetId": int(sheet_id),
                "dimension": dimension.upper(),
                "startIndex": int(start_index) - 1,
                "endIndex": int(end_index),
            },
            "destinationIndex": int(destination_index) - 1,
        }
        data = await self._batch_update(spreadsheet_id, [{"moveDimension": req}])
        if _is_error(data):
            return _error_response(data, "Could not move dimensions.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": f"Moved {dimension.lower()} {start_index}-{end_index} to position {destination_index}.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Cells: formatting, merge, sort, find/replace
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Apply cell formatting (background color, text color/bold/italic/font size, number "
            "format, horizontal alignment, text wrapping) to a rectangular range of cells. "
            "Only the formatting fields you provide are changed; cell values are untouched."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet containing the range. Required.",
            "start_row": "1-based first row of the range (inclusive). Required.",
            "end_row": "1-based last row of the range (inclusive). Required.",
            "start_column": "1-based first column of the range (inclusive). Accepts a number (1=A) or a column letter ('A'). Required.",
            "end_column": "1-based last column of the range (inclusive). Accepts a number or a column letter. Required.",
            "background_color": "Cell background color as a hex string, e.g. '#FFFF00' for yellow.",
            "text_color": "Text color as a hex string, e.g. '#FF0000' for red.",
            "bold": "If true, makes the text bold; if false, removes bold.",
            "italic": "If true, makes the text italic; if false, removes italic.",
            "font_size": "Font size in points, e.g. 12.",
            "number_format_type": (
                "Number format category to apply. One of: 'TEXT', 'NUMBER', 'PERCENT', "
                "'CURRENCY', 'DATE', 'TIME', 'DATE_TIME', 'SCIENTIFIC'."
            ),
            "number_format_pattern": (
                "Number format pattern string, e.g. '#,##0.00' for two-decimal numbers, "
                "'$#,##0.00' for currency, 'yyyy-mm-dd' for dates, '0.00%' for percentages. "
                "Used together with number_format_type."
            ),
            "horizontal_alignment": "Horizontal text alignment: 'LEFT', 'CENTER', or 'RIGHT'.",
            "wrap_strategy": "How text wraps within cells: 'OVERFLOW_CELL', 'WRAP', or 'CLIP'.",
        },
    )
    async def update_cell_format(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        start_column: ColumnRef,
        end_column: ColumnRef,
        background_color: Optional[str] = None,
        text_color: Optional[str] = None,
        bold: Optional[bool] = None,
        italic: Optional[bool] = None,
        font_size: Optional[int] = None,
        number_format_type: Optional[str] = None,
        number_format_pattern: Optional[str] = None,
        horizontal_alignment: Optional[str] = None,
        wrap_strategy: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column):
            return err
        cell_format: Dict[str, Any] = {}
        fields: List[str] = []
        if background_color:
            cell_format["backgroundColor"] = _hex_to_color(background_color)
            fields.append("userEnteredFormat.backgroundColor")
        text_format: Dict[str, Any] = {}
        if text_color:
            text_format["foregroundColor"] = _hex_to_color(text_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if bold is not None:
            text_format["bold"] = bool(bold)
            fields.append("userEnteredFormat.textFormat.bold")
        if italic is not None:
            text_format["italic"] = bool(italic)
            fields.append("userEnteredFormat.textFormat.italic")
        if font_size is not None:
            text_format["fontSize"] = int(font_size)
            fields.append("userEnteredFormat.textFormat.fontSize")
        if text_format:
            cell_format["textFormat"] = text_format
        if number_format_type:
            nf: Dict[str, Any] = {"type": number_format_type}
            if number_format_pattern:
                nf["pattern"] = number_format_pattern
            cell_format["numberFormat"] = nf
            fields.append("userEnteredFormat.numberFormat")
        if horizontal_alignment:
            cell_format["horizontalAlignment"] = horizontal_alignment
            fields.append("userEnteredFormat.horizontalAlignment")
        if wrap_strategy:
            cell_format["wrapStrategy"] = wrap_strategy
            fields.append("userEnteredFormat.wrapStrategy")
        if not fields:
            return {"success": False, "response": "Provide at least one formatting field to apply."}
        req = {
            "range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column),
            "cell": {"userEnteredFormat": cell_format},
            "fields": ",".join(fields),
        }
        data = await self._batch_update(spreadsheet_id, [{"repeatCell": req}])
        if _is_error(data):
            return _error_response(data, "Could not update cell formatting.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Cell formatting applied.", "result": data}

    @tool(
        description=(
            "Merge a rectangular range of cells into a single cell. "
            "Only the top-left cell's value/formatting is kept."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet containing the range. Required.",
            "start_row": "1-based first row of the range (inclusive). Required.",
            "end_row": "1-based last row of the range (inclusive). Required.",
            "start_column": "1-based first column of the range (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the range (inclusive); number or letter. Required.",
            "merge_type": (
                "How to merge. One of: 'MERGE_ALL' (default — one merged cell for the whole range), "
                "'MERGE_COLUMNS' (one merged cell per column, spanning all the given rows), "
                "'MERGE_ROWS' (one merged cell per row, spanning all the given columns)."
            ),
        },
    )
    async def merge_cells(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        start_column: ColumnRef,
        end_column: ColumnRef,
        merge_type: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column):
            return err
        req = {
            "range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column),
            "mergeType": merge_type or "MERGE_ALL",
        }
        data = await self._batch_update(spreadsheet_id, [{"mergeCells": req}])
        if _is_error(data):
            return _error_response(data, "Could not merge cells.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Cells merged.", "result": data}

    @tool(
        description="Unmerge a previously merged rectangular range of cells back into individual cells.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet containing the range. Required.",
            "start_row": "1-based first row of the range (inclusive). Required.",
            "end_row": "1-based last row of the range (inclusive). Required.",
            "start_column": "1-based first column of the range (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the range (inclusive); number or letter. Required.",
        },
    )
    async def unmerge_cells(self, spreadsheet_id: str, sheet_id: int, start_row: int, end_row: int, start_column: ColumnRef, end_column: ColumnRef) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column):
            return err
        req = {"range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column)}
        data = await self._batch_update(spreadsheet_id, [{"unmergeCells": req}])
        if _is_error(data):
            return _error_response(data, "Could not unmerge cells.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Cells unmerged.", "result": data}

    @tool(
        description=(
            "Find and replace text within a sheet or across the whole spreadsheet, optionally "
            "restricted to formulas or with regex matching."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "find": "The text (or regex pattern, if search_by_regex is true) to search for. Required.",
            "replacement": "The text to replace matches with. Required (use an empty string to delete matches).",
            "sheet_id": "Restrict the search to this numeric sheetId. Ignored if all_sheets is true. If both are omitted, searches the first sheet.",
            "all_sheets": "If true, searches across every sheet in the spreadsheet. Default false.",
            "match_case": "If true, the search is case-sensitive. Default false.",
            "match_entire_cell": "If true, only replaces cells whose entire content matches find. Default false.",
            "search_by_regex": "If true, find is treated as a regular expression. Default false.",
            "include_formulas": "If true, also searches within formula text (not just displayed values). Default false.",
        },
    )
    async def find_replace(
        self,
        spreadsheet_id: str,
        find: str,
        replacement: str,
        sheet_id: Optional[int] = None,
        all_sheets: Optional[bool] = None,
        match_case: Optional[bool] = None,
        match_entire_cell: Optional[bool] = None,
        search_by_regex: Optional[bool] = None,
        include_formulas: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, find=find, replacement=replacement):
            return err
        req: Dict[str, Any] = {"find": find, "replacement": replacement}
        if all_sheets:
            req["allSheets"] = True
        elif sheet_id is not None:
            req["sheetId"] = int(sheet_id)
        if match_case is not None:
            req["matchCase"] = bool(match_case)
        if match_entire_cell is not None:
            req["matchEntireCell"] = bool(match_entire_cell)
        if search_by_regex is not None:
            req["searchByRegex"] = bool(search_by_regex)
        if include_formulas is not None:
            req["includeFormulas"] = bool(include_formulas)
        data = await self._batch_update(spreadsheet_id, [{"findReplace": req}])
        if _is_error(data) or not data.get("replies"):
            return _error_response(data, "Could not run find/replace.", "Check that sheet_id (if provided) is correct \u2014 call get_spreadsheet(spreadsheet_id=...) to confirm.")
        result = (data["replies"][0].get("findReplace") or {})
        return {
            "success": True,
            "response": f"Replaced {result.get('occurrencesChanged', 0)} occurrence(s) in {result.get('valuesChanged', 0)} cell(s).",
            "result": result,
        }

    @tool(
        description=(
            "Sort a rectangular range of cells by one or more columns. "
            "Sorting rearranges the rows in-place within the given range."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet containing the range. Required.",
            "start_row": "1-based first row of the range to sort (inclusive). Typically the first data row, after any header row. Required.",
            "end_row": "1-based last row of the range to sort (inclusive). Required.",
            "start_column": "1-based first column of the range to sort (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the range to sort (inclusive); number or letter. Required.",
            "sort_specs": (
                "List of sort keys applied in order, e.g. "
                "[{\"column\": \"B\", \"ascending\": false}, {\"column\": 1, \"ascending\": true}]. "
                "Each item has 'column' (1-based number or letter, must be within the range) and "
                "'ascending' (bool, default true). Required."
            ),
        },
    )
    async def sort_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        start_column: ColumnRef,
        end_column: ColumnRef,
        sort_specs: List[Dict[str, Any]],
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column, sort_specs=sort_specs):
            return err
        if not sort_specs:
            return {"success": False, "response": "sort_specs must contain at least one entry."}
        specs = []
        for spec in sort_specs:
            col = spec.get("column")
            if col is None:
                continue
            specs.append({
                "dimensionIndex": _col_start(col),
                "sortOrder": "ASCENDING" if spec.get("ascending", True) else "DESCENDING",
            })
        if not specs:
            return {"success": False, "response": "sort_specs must contain at least one entry with a 'column'."}
        req = {
            "range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column),
            "sortSpecs": specs,
        }
        data = await self._batch_update(spreadsheet_id, [{"sortRange": req}])
        if _is_error(data):
            return _error_response(data, "Could not sort the range.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Range sorted.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Filters
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Apply a basic filter to a range, enabling the filter dropdown UI on its header row "
            "for sorting/filtering. Only one basic filter can exist per sheet; setting a new one "
            "replaces any existing basic filter on that sheet."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to filter. Required.",
            "start_row": "1-based first row of the filter range (inclusive), usually the header row. Required.",
            "end_row": "1-based last row of the filter range (inclusive). Required.",
            "start_column": "1-based first column of the filter range (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the filter range (inclusive); number or letter. Required.",
        },
    )
    async def set_basic_filter(self, spreadsheet_id: str, sheet_id: int, start_row: int, end_row: int, start_column: ColumnRef, end_column: ColumnRef) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column):
            return err
        req = {"filter": {"range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column)}}
        data = await self._batch_update(spreadsheet_id, [{"setBasicFilter": req}])
        if _is_error(data):
            return _error_response(data, "Could not set the basic filter.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Basic filter applied.", "result": data}

    @tool(
        description="Remove the basic filter from a sheet, if one exists.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to clear the filter from. Required.",
        },
    )
    async def clear_basic_filter(self, spreadsheet_id: str, sheet_id: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id):
            return err
        data = await self._batch_update(spreadsheet_id, [{"clearBasicFilter": {"sheetId": int(sheet_id)}}])
        if _is_error(data):
            return _error_response(data, "Could not clear the basic filter.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Basic filter cleared.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Conditional formatting
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Add a conditional formatting rule to a range: cells matching the condition get the "
            "given formatting applied automatically."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet containing the range. Required.",
            "start_row": "1-based first row of the range (inclusive). Required.",
            "end_row": "1-based last row of the range (inclusive). Required.",
            "start_column": "1-based first column of the range (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the range (inclusive); number or letter. Required.",
            "condition_type": (
                "The condition that triggers the formatting. Common values: "
                "'NUMBER_GREATER', 'NUMBER_GREATER_THAN_EQ', 'NUMBER_LESS', 'NUMBER_LESS_THAN_EQ', "
                "'NUMBER_EQ', 'NUMBER_NOT_EQ', 'NUMBER_BETWEEN', 'NUMBER_NOT_BETWEEN', "
                "'TEXT_CONTAINS', 'TEXT_NOT_CONTAINS', 'TEXT_STARTS_WITH', 'TEXT_ENDS_WITH', "
                "'TEXT_EQ', 'TEXT_IS_EMAIL', 'TEXT_IS_URL', 'DATE_EQ', 'DATE_BEFORE', 'DATE_AFTER', "
                "'DATE_ON_OR_BEFORE', 'DATE_ON_OR_AFTER', 'DATE_BETWEEN', 'DATE_NOT_BETWEEN', "
                "'ONE_OF_LIST', 'BLANK', 'NOT_BLANK', 'CUSTOM_FORMULA' (use condition_values=['=A1>B1']). "
                "Required."
            ),
            "condition_values": (
                "List of values the condition compares against, as strings, e.g. ['100'] for "
                "NUMBER_GREATER, ['10','20'] for NUMBER_BETWEEN, or ['=$C1=\"Done\"'] for "
                "CUSTOM_FORMULA. Leave empty for BLANK/NOT_BLANK."
            ),
            "background_color": "Background color to apply when the condition is met, as a hex string, e.g. '#00FF00'.",
            "text_color": "Text color to apply when the condition is met, as a hex string.",
            "bold": "If true, makes matching cells' text bold.",
            "index": "Zero-based position for this rule among the sheet's conditional format rules. If omitted, it is added last (lowest priority).",
        },
    )
    async def add_conditional_format_rule(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        start_column: ColumnRef,
        end_column: ColumnRef,
        condition_type: str,
        condition_values: Optional[List[str]] = None,
        background_color: Optional[str] = None,
        text_color: Optional[str] = None,
        bold: Optional[bool] = None,
        index: Optional[int] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column, condition_type=condition_type):
            return err
        cell_format: Dict[str, Any] = {}
        if background_color:
            cell_format["backgroundColor"] = _hex_to_color(background_color)
        text_format: Dict[str, Any] = {}
        if text_color:
            text_format["foregroundColor"] = _hex_to_color(text_color)
        if bold is not None:
            text_format["bold"] = bool(bold)
        if text_format:
            cell_format["textFormat"] = text_format
        if not cell_format:
            return {"success": False, "response": "Provide at least one of background_color, text_color, or bold."}
        condition: Dict[str, Any] = {"type": condition_type}
        if condition_values:
            condition["values"] = [{"userEnteredValue": str(v)} for v in condition_values]
        rule = {
            "ranges": [_build_grid_range(sheet_id, start_row, end_row, start_column, end_column)],
            "booleanRule": {"condition": condition, "format": cell_format},
        }
        req: Dict[str, Any] = {"rule": rule}
        if index is not None:
            req["index"] = int(index)
        data = await self._batch_update(spreadsheet_id, [{"addConditionalFormatRule": req}])
        if _is_error(data):
            return _error_response(data, "Could not add the conditional format rule.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        return {"success": True, "response": "Conditional format rule added.", "result": data}

    @tool(
        description="Delete a conditional formatting rule from a sheet by its index.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet. Required.",
            "index": "Zero-based index of the rule to delete, as listed in the sheet's conditionalFormats (from get_spreadsheet). Required.",
        },
    )
    async def delete_conditional_format_rule(self, spreadsheet_id: str, sheet_id: int, index: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id, index=index):
            return err
        data = await self._batch_update(spreadsheet_id, [{"deleteConditionalFormatRule": {"sheetId": int(sheet_id), "index": int(index)}}])
        if _is_error(data):
            return _error_response(data, "Could not delete the conditional format rule.", "Call get_spreadsheet(spreadsheet_id=...) to see the sheet\u2019s conditionalFormats list and find the correct index.")
        return {"success": True, "response": f"Conditional format rule {index} deleted.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Protected ranges
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Protect a range of cells (or an entire sheet) so that only specified editors can "
            "modify it. Returns the new protectedRangeId, needed by delete_protected_range."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "sheet_id": "The numeric sheetId of the sheet to protect. Required.",
            "start_row": "1-based first row of the range to protect (inclusive). Omit (with end_row, start_column, end_column) to protect the entire sheet.",
            "end_row": "1-based last row of the range to protect (inclusive).",
            "start_column": "1-based first column of the range to protect (inclusive); number or letter.",
            "end_column": "1-based last column of the range to protect (inclusive); number or letter.",
            "description": "Optional human-readable description of why the range is protected.",
            "warning_only": (
                "If true, editing shows a warning but is not actually blocked (anyone can dismiss "
                "it and edit). If false (default), only the listed editors can edit."
            ),
            "editor_emails": (
                "List of email addresses allowed to edit the protected range when warning_only is "
                "false. The current user is always included automatically."
            ),
        },
    )
    async def add_protected_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: Optional[int] = None,
        end_row: Optional[int] = None,
        start_column: Optional[ColumnRef] = None,
        end_column: Optional[ColumnRef] = None,
        description: Optional[str] = None,
        warning_only: Optional[bool] = None,
        editor_emails: Optional[List[str]] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, sheet_id=sheet_id):
            return err
        protected: Dict[str, Any] = {}
        if start_row is not None or end_row is not None or start_column is not None or end_column is not None:
            protected["range"] = _build_grid_range(sheet_id, start_row, end_row, start_column, end_column)
        else:
            protected["range"] = {"sheetId": int(sheet_id)}
        if description:
            protected["description"] = description
        if warning_only is not None:
            protected["warningOnly"] = bool(warning_only)
        if editor_emails:
            protected["editors"] = {"users": editor_emails}
        data = await self._batch_update(spreadsheet_id, [{"addProtectedRange": {"protectedRange": protected}}])
        if _is_error(data) or not data.get("replies"):
            return _error_response(data, "Could not add the protected range.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying.")
        result = (data["replies"][0].get("addProtectedRange") or {}).get("protectedRange") or {}
        return {
            "success": True,
            "response": f"Protected range created (protectedRangeId={result.get('protectedRangeId')}).",
            "protected_range": result,
        }

    @tool(
        description="Remove a protected range, restoring normal edit access.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "protected_range_id": "The protectedRangeId to remove (from add_protected_range or get_spreadsheet). Required.",
        },
    )
    async def delete_protected_range(self, spreadsheet_id: str, protected_range_id: int) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, protected_range_id=protected_range_id):
            return err
        data = await self._batch_update(spreadsheet_id, [{"deleteProtectedRange": {"protectedRangeId": int(protected_range_id)}}])
        if _is_error(data):
            return _error_response(data, "Could not delete the protected range.", "Call get_spreadsheet(spreadsheet_id=...) to find the correct protectedRangeId.")
        return {"success": True, "response": f"Protected range {protected_range_id} deleted.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Named ranges
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Create a named range — a friendly name that refers to a range of cells, usable in "
            "formulas (e.g. '=SUM(SalesData)'). Returns the new namedRangeId."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "name": "Name for the range, e.g. 'SalesData'. Must start with a letter or underscore and contain only letters, numbers, and underscores. Required.",
            "sheet_id": "The numeric sheetId containing the range. Required.",
            "start_row": "1-based first row of the range (inclusive). Required.",
            "end_row": "1-based last row of the range (inclusive). Required.",
            "start_column": "1-based first column of the range (inclusive); number or letter. Required.",
            "end_column": "1-based last column of the range (inclusive); number or letter. Required.",
        },
    )
    async def add_named_range(
        self,
        spreadsheet_id: str,
        name: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        start_column: ColumnRef,
        end_column: ColumnRef,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, name=name, sheet_id=sheet_id, start_row=start_row, end_row=end_row, start_column=start_column, end_column=end_column):
            return err
        named_range = {
            "name": name,
            "range": _build_grid_range(sheet_id, start_row, end_row, start_column, end_column),
        }
        data = await self._batch_update(spreadsheet_id, [{"addNamedRange": {"namedRange": named_range}}])
        if _is_error(data) or not data.get("replies"):
            return _error_response(data, "Could not add the named range.", "Call get_spreadsheet(spreadsheet_id=...) to look up the correct sheetId, row/column counts, or other IDs (namedRangeId, protectedRangeId, etc.) before retrying. Also check that name is a valid identifier and not already used.")
        result = (data["replies"][0].get("addNamedRange") or {}).get("namedRange") or {}
        return {"success": True, "response": f"Named range '{name}' created (namedRangeId={result.get('namedRangeId')}).", "named_range": result}

    @tool(
        description="Update the name and/or the cell range that an existing named range points to.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "named_range_id": "The namedRangeId to update (from add_named_range or get_spreadsheet). Required.",
            "name": "New name for the range.",
            "sheet_id": "The numeric sheetId containing the new range. Required if updating the range.",
            "start_row": "1-based first row of the new range (inclusive). Required if updating the range.",
            "end_row": "1-based last row of the new range (inclusive). Required if updating the range.",
            "start_column": "1-based first column of the new range (inclusive); number or letter. Required if updating the range.",
            "end_column": "1-based last column of the new range (inclusive); number or letter. Required if updating the range.",
        },
    )
    async def update_named_range(
        self,
        spreadsheet_id: str,
        named_range_id: str,
        name: Optional[str] = None,
        sheet_id: Optional[int] = None,
        start_row: Optional[int] = None,
        end_row: Optional[int] = None,
        start_column: Optional[ColumnRef] = None,
        end_column: Optional[ColumnRef] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, named_range_id=named_range_id):
            return err
        named_range: Dict[str, Any] = {"namedRangeId": named_range_id}
        fields: List[str] = []
        if name is not None:
            named_range["name"] = name
            fields.append("name")
        if sheet_id is not None and start_row is not None and end_row is not None and start_column is not None and end_column is not None:
            named_range["range"] = _build_grid_range(sheet_id, start_row, end_row, start_column, end_column)
            fields.append("range")
        if not fields:
            return {"success": False, "response": "Provide name and/or a full range (sheet_id, start_row, end_row, start_column, end_column) to update."}
        data = await self._batch_update(spreadsheet_id, [{"updateNamedRange": {"namedRange": named_range, "fields": ",".join(fields)}}])
        if _is_error(data):
            return _error_response(data, "Could not update the named range.", "Call get_spreadsheet(spreadsheet_id=...) to find the correct namedRangeId.")
        return {"success": True, "response": f"Named range {named_range_id} updated.", "result": data}

    @tool(
        description="Delete a named range by its ID.",
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "named_range_id": "The namedRangeId to delete (from add_named_range or get_spreadsheet). Required.",
        },
    )
    async def delete_named_range(self, spreadsheet_id: str, named_range_id: str) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id, named_range_id=named_range_id):
            return err
        data = await self._batch_update(spreadsheet_id, [{"deleteNamedRange": {"namedRangeId": named_range_id}}])
        if _is_error(data):
            return _error_response(data, "Could not delete the named range.", "Call get_spreadsheet(spreadsheet_id=...) to find the correct namedRangeId.")
        return {"success": True, "response": f"Named range {named_range_id} deleted.", "result": data}

    # ------------------------------------------------------------------
    # @tool public methods — Developer metadata
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Search for developer metadata (custom key/value pairs an app can attach to a "
            "spreadsheet, sheet, row, or column) matching the given key and/or value."
        ),
        params={
            "spreadsheet_id": "The ID of the spreadsheet. Required.",
            "metadata_key": "Match metadata entries with this key.",
            "metadata_value": "Match metadata entries with this value. Used together with metadata_key.",
            "location_type": (
                "Restrict results to metadata at this location type: "
                "'ROW', 'COLUMN', 'SHEET', or 'SPREADSHEET'."
            ),
        },
    )
    async def search_developer_metadata(
        self,
        spreadsheet_id: str,
        metadata_key: Optional[str] = None,
        metadata_value: Optional[str] = None,
        location_type: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(spreadsheet_id=spreadsheet_id):
            return err
        lookup: Dict[str, Any] = {}
        if metadata_key:
            if metadata_value is not None:
                lookup["metadataKey"] = metadata_key
                lookup["metadataValue"] = metadata_value
            else:
                lookup["metadataKey"] = metadata_key
        if location_type:
            lookup["locationMatchingStrategy"] = "INTERSECTING_LOCATIONS"
            lookup["locationType"] = location_type
        if not lookup:
            return {"success": False, "response": "Provide metadata_key and/or location_type to search by."}
        data = await self._sheets_request(
            "POST", f"/{spreadsheet_id}/developerMetadata:search",
            json_body={"dataFilters": [{"developerMetadataLookup": lookup}]},
        )
        if not data:
            return {"success": True, "response": "No matching developer metadata found.", "matches": [], "count": 0}
        matches = data.get("matchedDeveloperMetadata") or []
        return {"success": True, "response": f"Found {len(matches)} matching metadata entry(ies).", "matches": matches, "count": len(matches)}


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

_SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,}$")
_GID_RE = re.compile(r"[#&]gid=(\d+)")


def _extract_spreadsheet_id(url: str) -> Optional[str]:
    """Extract the spreadsheet ID from a Google Sheets URL, or pass through a bare ID."""
    url = url.strip()
    match = _SPREADSHEET_ID_RE.search(url)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(url):
        return url
    return None


def _extract_gid(url: str) -> Optional[int]:
    """Extract the sheet/tab gid from a Google Sheets URL fragment or query string, if present."""
    match = _GID_RE.search(url)
    if match:
        return int(match.group(1))
    return None


def _col_letter_to_index(letters: str) -> int:
    """Convert a column letter string ('A', 'B', ..., 'Z', 'AA', ...) to a 0-based index."""
    result = 0
    for ch in letters.strip().upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Invalid column letter: {letters!r}")
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def _col_start(col: Optional[ColumnRef]) -> Optional[int]:
    """Convert a 1-based column number or letter (inclusive start) to a 0-based GridRange startColumnIndex."""
    if col is None:
        return None
    if isinstance(col, str):
        return _col_letter_to_index(col)
    return int(col) - 1


def _col_end(col: Optional[ColumnRef]) -> Optional[int]:
    """Convert a 1-based column number or letter (inclusive end) to a 0-based, exclusive GridRange endColumnIndex."""
    if col is None:
        return None
    if isinstance(col, str):
        return _col_letter_to_index(col) + 1
    return int(col)


def _build_grid_range(
    sheet_id: int,
    start_row: Optional[int] = None,
    end_row: Optional[int] = None,
    start_column: Optional[ColumnRef] = None,
    end_column: Optional[ColumnRef] = None,
) -> Dict[str, Any]:
    """
    Build a Sheets API GridRange from 1-based, inclusive row/column bounds.
    Columns may be given as 1-based numbers (1=A) or column letters ('A', 'AB', ...).
    """
    grid: Dict[str, Any] = {"sheetId": int(sheet_id)}
    if start_row is not None:
        grid["startRowIndex"] = int(start_row) - 1
    if end_row is not None:
        grid["endRowIndex"] = int(end_row)
    sc = _col_start(start_column)
    if sc is not None:
        grid["startColumnIndex"] = sc
    ec = _col_end(end_column)
    if ec is not None:
        grid["endColumnIndex"] = ec
    return grid


def _hex_to_color(hex_str: str) -> Dict[str, float]:
    """Convert a '#RRGGBB' (or 'RRGGBB') hex string to a Sheets API Color object (0-1 floats)."""
    s = hex_str.strip().lstrip("#")
    if len(s) != 6:
        return {"red": 0, "green": 0, "blue": 0}
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return {"red": r, "green": g, "blue": b}


def _sheet_props_summary(props: Dict) -> Dict:
    grid = props.get("gridProperties") or {}
    return {
        "sheetId": props.get("sheetId"),
        "title": props.get("title"),
        "index": props.get("index"),
        "sheetType": props.get("sheetType"),
        "rowCount": grid.get("rowCount"),
        "columnCount": grid.get("columnCount"),
        "frozenRowCount": grid.get("frozenRowCount"),
        "frozenColumnCount": grid.get("frozenColumnCount"),
        "hidden": props.get("hidden"),
    }


def _sheet_summary(sheet: Dict) -> Dict:
    return _sheet_props_summary(sheet.get("properties") or {})
