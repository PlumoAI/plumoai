"""
Google Calendar functions class for functions_wrapper plugin.

Each public @tool method is one calendar action exposed to the LLM.
Private helpers handle HTTP, token refresh, response formatting, etc.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
DEFAULT_MAX_EVENTS = 50
MAX_EVENTS_CAP = 250
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


class GoogleCalendarFunctions(ConnectedServiceToolAgent):
    """
    Google Calendar tool functions.  Each @tool method is a calendar capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Google Calendar: list calendars, list/get/search events, "
        "create/update/delete, respond to invitations, free/busy, colors."
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
        raise NotImplementedError("GoogleCalendarFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GoogleCalendarFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(
            timeout=30.0, headers=self._headers()
        )
        logger.debug("GoogleCalendarFunctions initialized")

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

    async def _calendar_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = (
            f"{CALENDAR_API_BASE}{path}"
            if path.startswith("/")
            else f"{CALENDAR_API_BASE}/{path}"
        )
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._calendar_request(
                method, path, json_body=json_body, params=params, retry_401=False
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "Calendar API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._calendar_request(
                method, path, json_body=json_body, params=params,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("Calendar API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("Calendar API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            return None
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Calendar API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Raw API layer (private)
    # ------------------------------------------------------------------

    async def _list_calendar_list(self) -> List[Dict]:
        data = await self._calendar_request("GET", "/users/me/calendarList")
        return (data or {}).get("items") or []

    async def _list_events(
        self,
        calendar_id: str,
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = DEFAULT_MAX_EVENTS,
        q: Optional[str] = None,
        single_events: bool = True,
        order_by: Optional[str] = "startTime",
        page_token: Optional[str] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        p: Dict[str, Any] = {
            "maxResults": min(max_results, MAX_EVENTS_CAP),
            "singleEvents": single_events,
        }
        if time_min:
            p["timeMin"] = time_min
        if time_max:
            p["timeMax"] = time_max
        if q:
            p["q"] = q
        if order_by and single_events:
            p["orderBy"] = order_by
        if page_token:
            p["pageToken"] = page_token
        data = await self._calendar_request("GET", f"/calendars/{cal_enc}/events", params=p)
        if not data:
            return [], None
        return data.get("items") or [], data.get("nextPageToken")

    async def _create_event(self, calendar_id: str, body: Dict) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        return await self._calendar_request("POST", f"/calendars/{cal_enc}/events", json_body=body)

    async def _update_event(self, calendar_id: str, event_id: str, body: Dict) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        return await self._calendar_request(
            "PATCH", f"/calendars/{cal_enc}/events/{event_enc}", json_body=body
        )

    async def _delete_event(self, calendar_id: str, event_id: str) -> bool:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        res = await self._calendar_request("DELETE", f"/calendars/{cal_enc}/events/{event_enc}")
        return res is not None

    async def _get_event(self, calendar_id: str, event_id: str) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        return await self._calendar_request("GET", f"/calendars/{cal_enc}/events/{event_enc}")

    async def _freebusy_query(self, body: Dict) -> Optional[Dict]:
        return await self._calendar_request("POST", "/freeBusy", json_body=body)

    async def _list_colors_raw(self) -> Optional[Dict]:
        return await self._calendar_request("GET", "/colors")

    async def _respond_to_event_raw(
        self, calendar_id: str, event_id: str, response_status: str, attendee_email: Optional[str] = None
    ) -> Optional[Dict]:
        ev = await self._get_event(calendar_id, event_id)
        if not ev or not ev.get("attendees"):
            return None
        email = attendee_email or await self._primary_calendar_id()
        if not email:
            return None
        status = (response_status or "").lower()
        if status not in ("accepted", "declined", "tentative"):
            return None
        new_attendees = []
        for a in ev.get("attendees") or []:
            att = dict(a) if isinstance(a, dict) else {"email": str(a)}
            if (att.get("email") or "").lower() == email.lower():
                att["responseStatus"] = status
            new_attendees.append(att)
        return await self._update_event(calendar_id, event_id, {"attendees": new_attendees})

    async def _primary_calendar_id(self) -> Optional[str]:
        items = await self._list_calendar_list()
        for cal in items or []:
            if cal.get("primary"):
                return cal.get("id")
        return (items or [{}])[0].get("id") if items else None

    def _build_event_body(self, params: Dict) -> Dict:
        body: Dict[str, Any] = {}
        if params.get("summary") is not None:
            body["summary"] = str(params["summary"])
        if params.get("description") is not None:
            body["description"] = str(params["description"])
        if params.get("location") is not None:
            body["location"] = str(params["location"])
        # Resolve timezone: explicit param > app_config user_timezone > None (UTC)
        tz = (params.get("time_zone") or "").strip() or (self.app_config or {}).get("user_timezone") or None
        start = _normalize_event_datetime(params.get("start"))
        end = _normalize_event_datetime(params.get("end"))
        if start is not None:
            if tz and start.get("dateTime"):
                # Remove the "Z" suffix added by _str_to_event_datetime so Google
                # Calendar interprets the time as local rather than UTC.
                dt_val = start["dateTime"]
                if isinstance(dt_val, str) and dt_val.endswith("Z"):
                    start["dateTime"] = dt_val[:-1]
                start["timeZone"] = tz
            body["start"] = start
        if end is not None:
            if tz and end.get("dateTime"):
                dt_val = end["dateTime"]
                if isinstance(dt_val, str) and dt_val.endswith("Z"):
                    end["dateTime"] = dt_val[:-1]
                end["timeZone"] = tz
            body["end"] = end
        if params.get("attendees"):
            att = params["attendees"]
            raw = att if isinstance(att, list) else [att]
            attendees_out = []
            for e in raw:
                if isinstance(e, dict) and e.get("email"):
                    # Full attendee object — pass through known fields
                    obj: Dict[str, Any] = {"email": e["email"]}
                    if e.get("displayName"):
                        obj["displayName"] = e["displayName"]
                    if e.get("optional") is not None:
                        obj["optional"] = bool(e["optional"])
                    if e.get("comment"):
                        obj["comment"] = e["comment"]
                    if e.get("additionalGuests") is not None:
                        obj["additionalGuests"] = int(e["additionalGuests"])
                    attendees_out.append(obj)
                else:
                    email = _normalize_attendee_email(e)
                    if email:
                        attendees_out.append({"email": email})
                    else:
                        logger.warning("Calendar: skipped invalid attendee %r", e)
            if attendees_out:
                body["attendees"] = attendees_out
        if params.get("recurrence"):
            rec = params["recurrence"]
            body["recurrence"] = rec if isinstance(rec, list) else [str(rec)]
        if params.get("color_id") is not None:
            body["colorId"] = str(params["color_id"])
        if params.get("reminders") is not None:
            rem = params["reminders"]
            if isinstance(rem, dict):
                rem_body: Dict[str, Any] = {}
                if "useDefault" in rem:
                    rem_body["useDefault"] = bool(rem["useDefault"])
                if rem.get("overrides"):
                    overrides = []
                    for o in rem["overrides"]:
                        if isinstance(o, dict) and o.get("method") and o.get("minutes") is not None:
                            overrides.append({"method": o["method"], "minutes": int(o["minutes"])})
                    if overrides:
                        rem_body["overrides"] = overrides
                if rem_body:
                    body["reminders"] = rem_body
        if params.get("visibility") is not None:
            body["visibility"] = str(params["visibility"])
        if params.get("transparency") is not None:
            body["transparency"] = str(params["transparency"])
        return body

    # ------------------------------------------------------------------
    # @tool public methods — one per calendar action
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List all Google Calendars the user has added to their calendar list. "
            "Returns each calendar's ID, display name, whether it is the primary calendar, "
            "and the user's access role (owner, writer, reader). "
            "Use this to discover valid calendar IDs before calling other calendar tools. "
            "The primary calendar ID is usually the user's email address."
        ),
    )
    async def list_calendars(self) -> Dict:
        items = await self._list_calendar_list()
        if not items:
            return {"success": True, "response": "No calendars found.", "calendars": [], "count": 0}
        out = [
            {
                "id": cal.get("id"),
                "summary": cal.get("summary"),
                "primary": cal.get("primary", False),
                "accessRole": cal.get("accessRole"),
            }
            for cal in items
        ]
        return {"success": True, "response": f"Found {len(out)} calendar(s).", "calendars": out, "count": len(out)}

    @tool(
        description=(
            "List events from a specific Google Calendar. "
            "By default returns upcoming events starting from right now. "
            "Use time_min and time_max to define a custom date/time window. "
            "Returns event ID, title, start/end times, location, description, and a link to the event. "
            "Event IDs returned here are required by get_event, update_event, delete_event, and respond_to_event."
        ),
        params={
            "calendar_id": (
                "ID of the calendar to list events from. Use 'primary' for the user's main calendar. "
                "Other valid IDs can be found by calling list_calendars first."
            ),
            "time_min": (
                "Lower bound (inclusive) for event start time, in RFC3339 format with timezone offset. "
                "Example: '2026-06-11T00:00:00Z' for midnight UTC, or '2026-06-11T00:00:00+05:00' for UTC+5. "
                "Defaults to the current date and time (returns upcoming events only)."
            ),
            "time_max": (
                "Upper bound (exclusive) for event start time, in RFC3339 format. "
                "Example: '2026-06-30T23:59:59Z'. "
                "If omitted, there is no upper bound."
            ),
            "max_results": (
                "Maximum number of events to return. Default is 50, maximum allowed is 250."
            ),
            "q": (
                "Free-text search query to filter events. "
                "Matches against event title, description, location, and attendee emails. "
                "Example: 'team standup' or 'john@example.com'."
            ),
        },
    )
    async def list_events(
        self,
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = DEFAULT_MAX_EVENTS,
        q: Optional[str] = None,
    ) -> Dict:
        max_results = min(int(max_results or DEFAULT_MAX_EVENTS), MAX_EVENTS_CAP)
        # Default to now so bare list_events() returns upcoming events, not past ones.
        if not time_min:
            time_min = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        events, _ = await self._list_events(
            calendar_id, time_min=time_min, time_max=time_max, max_results=max_results, q=q
        )
        if not events:
            return {"success": True, "response": "No events found.", "events": [], "count": 0}
        out = []
        lines = []
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        for ev in events:
            start = ev.get("start") or {}
            end = ev.get("end") or {}
            start_str = start.get("dateTime") or start.get("date")
            end_str = end.get("dateTime") or end.get("date")
            out.append({
                "id": ev.get("id"),
                "summary": ev.get("summary"),
                "description": (ev.get("description") or "")[:500],
                "location": ev.get("location"),
                "start": start_str,
                "end": end_str,
                "status": ev.get("status"),
                "htmlLink": ev.get("htmlLink"),
            })
            time_part = (
                f"{_format_datetime_in_user_tz(start_str, user_tz)} to {_format_datetime_in_user_tz(end_str, user_tz)}"
                if (start_str and end_str)
                else "Time not set"
            )
            lines.append(f"Meeting: {ev.get('summary') or 'Untitled'}\nTime: {time_part}")
        response = f"There are {len(out)} meeting(s) scheduled:\n\n" + "\n\n".join(lines)
        return {"success": True, "response": response, "events": out, "count": len(out)}

    @tool(
        description=(
            "Search for Google Calendar events that match a text query. "
            "Searches across event titles, descriptions, locations, and attendee email addresses. "
            "Useful for finding a specific event by name or looking up events involving a particular person. "
            "Optionally restrict the search to a date/time window using time_min and time_max. "
            "Returns event IDs, titles, times, and links."
        ),
        params={
            "q": (
                "Text to search for. Matched against event title, description, location, and attendee emails. "
                "Example: 'Demo Meeting' or 'krishna@example.com'."
            ),
            "calendar_id": (
                "ID of the calendar to search. Use 'primary' for the main calendar. "
                "Get other IDs by calling list_calendars."
            ),
            "time_min": (
                "Search only for events starting on or after this time. RFC3339 format. "
                "Example: '2026-06-01T00:00:00Z'. If omitted, searches all past and future events."
            ),
            "time_max": (
                "Search only for events starting before this time. RFC3339 format. "
                "Example: '2026-12-31T23:59:59Z'. If omitted, no upper bound."
            ),
            "max_results": "Maximum number of events to return. Default is 50, maximum allowed is 250.",
        },
    )
    async def search_events(
        self,
        q: str,
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = DEFAULT_MAX_EVENTS,
    ) -> Dict:
        return await self.list_events(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            q=q,
        )

    @tool(
        description=(
            "Retrieve the full details of a single Google Calendar event by its event ID. "
            "Returns all event fields including title, start/end times with timezone, description, "
            "location, list of attendees with their RSVP status (accepted/declined/tentative/needsAction), "
            "recurrence rules, event status, creation/update timestamps, and a direct link to the event. "
            "Get the event ID from list_events or search_events first."
        ),
        params={
            "event_id": (
                "The unique identifier of the event to retrieve. "
                "Obtain this from the 'id' field in list_events or search_events results."
            ),
            "calendar_id": (
                "ID of the calendar that contains the event. Use 'primary' for the main calendar. "
                "Must match the calendar the event belongs to."
            ),
        },
    )
    async def get_event(self, event_id: str, calendar_id: str = "primary") -> Dict:
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        ev = await self._get_event(calendar_id, event_id)
        if not ev:
            return {"success": False, "response": "Event not found.", "event_id": event_id}
        event_detail = _event_to_detail(ev)
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        start_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        end_str = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        response_text = f"Event: {ev.get('summary') or 'Untitled'}. "
        response_text += " ".join(
            p for p in [
                f"Start: {_format_datetime_in_user_tz(start_str, user_tz)}." if start_str else None,
                f"End: {_format_datetime_in_user_tz(end_str, user_tz)}." if end_str else None,
                f"Location: {ev.get('location')}." if ev.get("location") else None,
                f"Link: {ev.get('htmlLink')}" if ev.get("htmlLink") else None,
            ] if p
        )
        return {"success": True, "response": response_text, "event": event_detail}

    @tool(
        description=(
            "Create a new event on a Google Calendar. "
            "Requires a title (summary), start time, and end time. "
            "Times must be in RFC3339 format (e.g. '2026-06-11T14:00:00Z') for timed events, "
            "or YYYY-MM-DD (e.g. '2026-06-11') for all-day events. "
            "Optionally add attendees by email, a description, location, timezone, color, and recurrence rules. "
            "Returns the created event's ID, title, times, attendees, and a link to view it in Google Calendar."
        ),
        params={
            "summary": (
                "Title of the event. This is the main name shown on the calendar. "
                "Example: 'Team Standup', 'Demo Meeting With Krishna'."
            ),
            "start": (
                "Start date and time of the event. "
                "For timed events use RFC3339 format: 'YYYY-MM-DDTHH:MM:SSZ' for UTC "
                "or 'YYYY-MM-DDTHH:MM:SS+05:30' for a specific timezone offset. "
                "For all-day events use date-only format: 'YYYY-MM-DD'. "
                "Example timed: '2026-06-11T17:00:00Z'. Example all-day: '2026-06-11'."
            ),
            "end": (
                "End date and time of the event. Must be after start. "
                "Same format as start: RFC3339 for timed events (e.g. '2026-06-11T18:00:00Z'), "
                "or YYYY-MM-DD for all-day events (the end date is exclusive, so a one-day event "
                "that starts on 2026-06-11 should end on 2026-06-12)."
            ),
            "calendar_id": (
                "ID of the calendar to create the event in. Use 'primary' for the user's main calendar. "
                "Get other calendar IDs by calling list_calendars."
            ),
            "description": (
                "Optional free-text description or agenda for the event. Supports plain text. "
                "Example: 'Discuss Q3 roadmap and sprint planning.'"
            ),
            "location": (
                "Optional physical or virtual location of the event. "
                "Example: 'Conference Room A', 'https://meet.google.com/abc-defg-hij'."
            ),
            "attendees": (
                "Optional list of people to invite. "
                "Each item can be a plain email string or a nested attendee object. "
                "Plain email: 'alice@example.com'. "
                "Full object: {\"email\": \"alice@example.com\", \"displayName\": \"Alice\", "
                "\"optional\": false, \"comment\": \"Please review the agenda\", \"additionalGuests\": 0}. "
                "Fields in the attendee object: "
                "email (string, required) — the attendee's email address; "
                "displayName (string) — the attendee's name; "
                "optional (boolean) — if true, attendance is optional, default false; "
                "comment (string) — the attendee's response comment; "
                "additionalGuests (integer) — number of additional unnamed guests, default 0. "
                "Example mixed list: ['bob@example.com', {\"email\": \"alice@example.com\", \"optional\": true}]."
            ),
            "time_zone": (
                "Optional IANA timezone name for the event start and end times. "
                "Use when start/end are in local time rather than UTC. "
                "Example: 'Asia/Karachi', 'America/New_York', 'Europe/London'. "
                "If omitted, UTC is assumed."
            ),
            "recurrence": (
                "Optional list of RFC 5545 recurrence rule strings to make this a repeating event. "
                "Each string must be a valid RRULE, EXRULE, RDATE, or EXDATE line. "
                "Example weekly on Mondays: ['RRULE:FREQ=WEEKLY;BYDAY=MO']. "
                "Example daily for 5 occurrences: ['RRULE:FREQ=DAILY;COUNT=5']. "
                "Example every month on the 1st: ['RRULE:FREQ=MONTHLY;BYMONTHDAY=1']. "
                "Example weekdays only: ['RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR']. "
                "If omitted, the event is a one-time event."
            ),
            "color_id": (
                "Optional color ID string to color-code this event on the calendar. "
                "Call list_colors to see all valid IDs and their names. "
                "Example values: '1' (Tomato), '2' (Flamingo), '3' (Tangerine), '4' (Banana), "
                "'5' (Sage), '6' (Basil), '7' (Peacock), '8' (Blueberry), '9' (Lavender), "
                "'10' (Grape), '11' (Graphite)."
            ),
            "reminders": (
                "Optional reminder/notification settings for this event. "
                "A nested object with two fields: "
                "useDefault (boolean) — if true, applies the calendar's default reminders and ignores overrides; "
                "overrides (list of objects) — custom reminders, max 5, each with: "
                "method (string, required) — 'email' to send an email, or 'popup' for an on-screen notification; "
                "minutes (integer, required) — how many minutes before the event to send the reminder (0 to 40320). "
                "Example — popup 10 minutes before and email 60 minutes before: "
                "{\"useDefault\": false, \"overrides\": [{\"method\": \"popup\", \"minutes\": 10}, {\"method\": \"email\", \"minutes\": 60}]}. "
                "Example — use calendar default: {\"useDefault\": true}."
            ),
            "visibility": (
                "Optional visibility setting controlling who can see the event details. "
                "Accepted values: "
                "'default' — uses the calendar's default visibility setting; "
                "'public' — the event is visible to everyone who can see the calendar; "
                "'private' — the event details are hidden from other users (shown as 'Busy'); "
                "'confidential' — treated like private (legacy value)."
            ),
            "transparency": (
                "Optional transparency setting controlling whether the event blocks time in availability queries. "
                "Accepted values: "
                "'opaque' (default) — the event blocks time and shows the user as Busy; "
                "'transparent' — the event does not block time and the user appears as Available."
            ),
        },
    )
    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        calendar_id: str = "primary",
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[Any] = None,
        time_zone: Optional[str] = None,
        recurrence: Optional[Any] = None,
        color_id: Optional[str] = None,
        reminders: Optional[Any] = None,
        visibility: Optional[str] = None,
        transparency: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {
            "calendar_id": calendar_id,
            "summary": summary,
            "start": start,
            "end": end,
        }
        if description is not None:
            params["description"] = description
        if location is not None:
            params["location"] = location
        if attendees is not None:
            params["attendees"] = attendees
        if time_zone is not None:
            params["time_zone"] = time_zone
        if recurrence is not None:
            params["recurrence"] = recurrence
        if color_id is not None:
            params["color_id"] = color_id
        if reminders is not None:
            params["reminders"] = reminders
        if visibility is not None:
            params["visibility"] = visibility
        if transparency is not None:
            params["transparency"] = transparency

        # Merge start/end from prior step results if missing/placeholder
        _merge_start_end_from_provided_data(params, self._step_results)

        body = self._build_event_body(params)
        start_val = body.get("start")
        end_val = body.get("end")
        err_resp = (
            "Start and end date/time are required and must be valid. "
            "Use RFC3339 (e.g. 2026-06-11T14:00:00Z) or YYYY-MM-DD for all-day."
        )
        if not start_val or not end_val:
            return _calendar_date_resolution_response(self._current_query, params.get("start"), params.get("end"), err_resp)
        dt_start = start_val.get("dateTime") or start_val.get("date")
        dt_end = end_val.get("dateTime") or end_val.get("date")
        if not dt_start or not dt_end or "None" in str(dt_start) or "None" in str(dt_end) or str(dt_start).startswith("{"):
            return _calendar_date_resolution_response(
                self._current_query, params.get("start"), params.get("end"),
                "Start and end must be valid RFC3339 datetime strings or YYYY-MM-DD.",
            )
        parsed = _parse_event_time_for_compare(start_val, end_val)
        if parsed:
            start_ob, end_ob, is_all_day = parsed
            if end_ob <= start_ob:
                if not is_all_day and start_val.get("dateTime"):
                    start_dt = datetime.fromisoformat(str(dt_start).replace("Z", "+00:00"))
                    end_dt = start_dt + timedelta(hours=1)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    body["end"] = {"dateTime": end_dt.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
                else:
                    return {
                        "success": False,
                        "response": "End date/time must be after start.",
                        "result": {"success": False},
                    }
        if not body.get("summary"):
            body["summary"] = "Untitled Event"
        if self.app_config.get("calendar_check_conflicts") and dt_start and dt_end:
            freebusy_data = await self._freebusy_query(
                {"timeMin": dt_start, "timeMax": dt_end, "items": [{"id": calendar_id}]}
            )
            if freebusy_data:
                busy_periods = ((freebusy_data.get("calendars") or {}).get(calendar_id) or {}).get("busy") or []
                conflicts = [
                    b for b in busy_periods
                    if _ranges_overlap(dt_start, dt_end, b.get("start") or "", b.get("end") or "")
                ]
                if conflicts:
                    conflict_summary = "; ".join(
                        f"{b.get('start', '')}–{b.get('end', '')}" for b in conflicts[:5]
                    )
                    return {
                        "success": False,
                        "response": f"The requested time conflicts with existing event(s): {conflict_summary}. Please choose another time.",
                        "conflict_detected": True,
                        "conflicts": conflicts[:10],
                        "result": {"success": False},
                    }
        created = await self._create_event(calendar_id, body)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create the event."}
        event_detail = _event_to_detail(created)
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        response_text = _format_event_created_response(created, user_tz)
        return {"success": True, "response": response_text, "event": event_detail}

    @tool(
        description=(
            "Update (patch) an existing Google Calendar event. "
            "Only the fields you provide will be changed — fields you omit remain unchanged. "
            "You must provide the event_id of the event to update. "
            "Get the event_id from list_events, search_events, or get_event. "
            "You can update the title, start/end times, description, location, attendees, "
            "timezone, recurrence rules, and color. "
            "Returns the updated event with all its current fields."
        ),
        params={
            "event_id": (
                "The unique identifier of the event to update. "
                "Obtain this from the 'id' field in list_events or search_events results."
            ),
            "calendar_id": (
                "ID of the calendar that contains the event. Use 'primary' for the main calendar."
            ),
            "summary": (
                "New title for the event. "
                "Example: 'Updated Team Standup'."
            ),
            "start": (
                "New start date/time. RFC3339 for timed events (e.g. '2026-06-12T09:00:00Z') "
                "or YYYY-MM-DD for all-day events (e.g. '2026-06-12')."
            ),
            "end": (
                "New end date/time. RFC3339 for timed events (e.g. '2026-06-12T10:00:00Z') "
                "or YYYY-MM-DD for all-day events. Must be after start."
            ),
            "description": "New description text for the event.",
            "location": (
                "New location for the event. "
                "Example: 'Room 201' or 'https://zoom.us/j/123456'."
            ),
            "attendees": (
                "Updated attendee list. This replaces the entire existing attendee list — "
                "include everyone you want on the event, not just new additions. "
                "Each item can be a plain email string or a nested attendee object. "
                "Plain email: 'alice@example.com'. "
                "Full object: {\"email\": \"alice@example.com\", \"displayName\": \"Alice\", "
                "\"optional\": false, \"comment\": \"Please join\", \"additionalGuests\": 0}. "
                "Fields: email (required), displayName, optional (boolean), comment, additionalGuests (integer)."
            ),
            "time_zone": (
                "IANA timezone name to associate with the updated start/end times. "
                "Example: 'Asia/Karachi', 'America/New_York'. Only needed if changing times to local times."
            ),
            "recurrence": (
                "New recurrence rule(s) as a list of RFC 5545 RRULE strings. "
                "Example: ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR']. "
                "Pass an empty list [] to remove recurrence and make the event one-time."
            ),
            "color_id": (
                "New color ID string for the event. "
                "Example values: '1' (Tomato), '2' (Flamingo), '3' (Tangerine), '4' (Banana), "
                "'5' (Sage), '6' (Basil), '7' (Peacock), '8' (Blueberry), '9' (Lavender), "
                "'10' (Grape), '11' (Graphite). Call list_colors to see all options."
            ),
            "reminders": (
                "Updated reminder/notification settings. "
                "Object with: useDefault (boolean) — use calendar's default reminders; "
                "overrides (list) — custom reminders, max 5, each with: "
                "method ('email' or 'popup') and minutes (integer, 0–40320). "
                "Example: {\"useDefault\": false, \"overrides\": [{\"method\": \"popup\", \"minutes\": 15}]}."
            ),
            "visibility": (
                "Updated visibility. Accepted values: "
                "'default' (calendar default), 'public' (visible to all), "
                "'private' (details hidden, shown as Busy), 'confidential' (same as private)."
            ),
            "transparency": (
                "Updated transparency. "
                "'opaque' (default) — blocks time, user appears Busy; "
                "'transparent' — does not block time, user appears Available."
            ),
        },
    )
    async def update_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
        summary: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[Any] = None,
        time_zone: Optional[str] = None,
        recurrence: Optional[Any] = None,
        color_id: Optional[str] = None,
        reminders: Optional[Any] = None,
        visibility: Optional[str] = None,
        transparency: Optional[str] = None,
    ) -> Dict:
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        params: Dict[str, Any] = {
            "calendar_id": calendar_id,
            "event_id": event_id,
        }
        if summary is not None:
            params["summary"] = summary
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if description is not None:
            params["description"] = description
        if location is not None:
            params["location"] = location
        if attendees is not None:
            params["attendees"] = attendees
        if time_zone is not None:
            params["time_zone"] = time_zone
        if recurrence is not None:
            params["recurrence"] = recurrence
        if color_id is not None:
            params["color_id"] = color_id
        if reminders is not None:
            params["reminders"] = reminders
        if visibility is not None:
            params["visibility"] = visibility
        if transparency is not None:
            params["transparency"] = transparency
        _merge_start_end_from_provided_data(params, self._step_results)

        body = self._build_event_body(params)
        if not body:
            return {
                "success": False,
                "response": "Provide at least one field to update (summary, start, end, description, location, attendees, recurrence, color_id, reminders, visibility, transparency).",
            }
        if body.get("start") and body.get("end"):
            parsed = _parse_event_time_for_compare(body["start"], body["end"])
            if parsed:
                start_ob, end_ob, is_all_day = parsed
                if end_ob <= start_ob:
                    if not is_all_day and body["start"].get("dateTime"):
                        start_dt = datetime.fromisoformat(str(body["start"]["dateTime"]).replace("Z", "+00:00"))
                        end_dt = start_dt + timedelta(hours=1)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        body["end"] = {"dateTime": end_dt.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
                    else:
                        return {"success": False, "response": "End date/time must be after start.", "result": {"success": False}}
        updated = await self._update_event(calendar_id, str(event_id), body)
        if not updated:
            return {"success": False, "response": "Could not update the event."}
        event_detail = _event_to_detail(updated)
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        up_start = (updated.get("start") or {}).get("dateTime") or (updated.get("start") or {}).get("date")
        up_end = (updated.get("end") or {}).get("dateTime") or (updated.get("end") or {}).get("date")
        response_text = f"Event updated: {updated.get('summary') or 'Untitled'}. " + " ".join(
            p for p in [
                f"Start: {_format_datetime_in_user_tz(up_start, user_tz)}." if up_start else None,
                f"End: {_format_datetime_in_user_tz(up_end, user_tz)}." if up_end else None,
                f"Link: {updated.get('htmlLink')}" if updated.get("htmlLink") else None,
            ] if p
        )
        return {"success": True, "response": response_text, "event": event_detail}

    @tool(
        description=(
            "Permanently delete a Google Calendar event. "
            "This action cannot be undone. The event is removed from all attendees' calendars. "
            "You must provide the event_id. Get it from list_events or search_events first. "
            "Returns confirmation that the event was deleted."
        ),
        params={
            "event_id": (
                "The unique identifier of the event to delete. "
                "Obtain this from the 'id' field in list_events or search_events results."
            ),
            "calendar_id": (
                "ID of the calendar that contains the event. Use 'primary' for the main calendar."
            ),
        },
    )
    async def delete_event(self, event_id: str, calendar_id: str = "primary") -> Dict:
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        ok = await self._delete_event(calendar_id, str(event_id))
        if not ok:
            return {"success": False, "response": "Could not delete the event."}
        return {"success": True, "response": "Event deleted.", "event_id": event_id}

    @tool(
        description=(
            "Respond to a Google Calendar event invitation — accept, decline, or mark as tentative. "
            "This updates your RSVP status on the event, which is visible to the event organizer and other attendees. "
            "You must be listed as an attendee on the event to respond. "
            "Returns the updated event showing the new RSVP status."
        ),
        params={
            "event_id": (
                "The unique identifier of the event to respond to. "
                "Obtain this from the 'id' field in list_events or search_events results."
            ),
            "response_status": (
                "Your RSVP response. Must be exactly one of: "
                "'accepted' (you will attend), "
                "'declined' (you will not attend), "
                "'tentative' (you might attend)."
            ),
            "calendar_id": (
                "ID of the calendar that contains the event. Use 'primary' for the main calendar."
            ),
            "attendee_email": (
                "Email address to respond as. Defaults to the primary calendar owner's email. "
                "Only needed if responding on behalf of a different attendee."
            ),
        },
    )
    async def respond_to_event(
        self,
        event_id: str,
        response_status: str,
        calendar_id: str = "primary",
        attendee_email: Optional[str] = None,
    ) -> Dict:
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        rs = (response_status or "").lower()
        if rs not in ("accepted", "declined", "tentative"):
            return {"success": False, "response": "response_status must be accepted, declined, or tentative."}
        updated = await self._respond_to_event_raw(calendar_id, str(event_id), rs, attendee_email)
        if not updated:
            return {
                "success": False,
                "response": "Could not update response (event not found or you are not an attendee).",
            }
        label = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentative"}[rs]
        return {"success": True, "response": f"Invitation {label.lower()}.", "event": _event_to_detail(updated)}

    @tool(
        description=(
            "Query the free/busy schedule for one or more calendars over a given time range. "
            "Returns the time slots when each calendar is busy (has scheduled events), "
            "and calculates the remaining free slots within the requested window. "
            "Use this before scheduling a meeting to check availability or find an open slot. "
            "time_min and time_max are required and must be concrete RFC3339 timestamps."
        ),
        params={
            "time_min": (
                "Start of the time range to check, in RFC3339 format. Required. "
                "Example: '2026-06-11T00:00:00Z' for the start of a day in UTC, "
                "or '2026-06-11T09:00:00+05:00' for 9 AM in UTC+5."
            ),
            "time_max": (
                "End of the time range to check, in RFC3339 format. Required. "
                "Example: '2026-06-11T23:59:59Z' for the end of a day. "
                "Must be after time_min."
            ),
            "calendar_ids": (
                "List of calendar IDs to check availability for. "
                "Defaults to ['primary'] (the user's main calendar). "
                "To check multiple calendars provide a list: ['primary', 'other@example.com']. "
                "Get valid calendar IDs by calling list_calendars."
            ),
        },
    )
    async def get_freebusy(
        self,
        time_min: str,
        time_max: str,
        calendar_ids: Optional[Any] = None,
    ) -> Dict:
        # Resolve calendar_ids
        if not calendar_ids:
            calendar_ids = ["primary"]
        elif not isinstance(calendar_ids, list):
            calendar_ids = [str(calendar_ids)]

        if not time_min or not time_max:
            return {"success": False, "response": "time_min and time_max (RFC3339) are required."}

        # Resolve placeholder values from prior step results
        params_temp: Dict[str, Any] = {"time_min": time_min, "time_max": time_max}
        _merge_freebusy_times_from_provided_data(params_temp, self._step_results)
        time_min = params_temp.get("time_min") or time_min
        time_max = params_temp.get("time_max") or time_max

        time_min_s = str(time_min).strip()
        time_max_s = str(time_max).strip()
        if (
            "{" in time_min_s or "{" in time_max_s
            or _rfc3339_to_naive_dt(time_min_s) is None
            or _rfc3339_to_naive_dt(time_max_s) is None
        ):
            return {
                "success": False,
                "response": (
                    "time_min and time_max must be valid RFC3339 (e.g. 2026-06-11T00:00:00Z). "
                    "Please provide concrete dates in RFC3339 format."
                ),
                "result": {"success": False},
            }

        body = {"timeMin": time_min_s, "timeMax": time_max_s, "items": [{"id": cid} for cid in calendar_ids]}
        data = await self._freebusy_query(body)
        if not data:
            return {"success": False, "response": "Could not fetch free/busy."}

        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        cal_map = data.get("calendars") or {}
        out: Dict[str, Any] = {}
        busy_slots_readable: List[str] = []
        free_slots_readable: List[str] = []

        for cid in calendar_ids:
            busy = (cal_map.get(cid) or {}).get("busy") or []
            slots = [{"start": b.get("start"), "end": b.get("end")} for b in busy if b.get("start") and b.get("end")]
            out[cid] = slots
            for b in slots:
                busy_slots_readable.append(
                    f"{_format_datetime_in_user_tz(b['start'], user_tz)} - {_format_datetime_in_user_tz(b['end'], user_tz)}"
                )
            t_min = _rfc3339_to_naive_dt(time_min_s)
            t_max = _rfc3339_to_naive_dt(time_max_s)
            if t_min is not None and t_max is not None and t_min < t_max:
                sorted_slots = sorted(
                    slots,
                    key=lambda s: (_rfc3339_to_naive_dt(s["start"]) or datetime.min, _rfc3339_to_naive_dt(s["end"]) or datetime.max),
                )
                free_start_dt = t_min
                for b in sorted_slots:
                    bs_dt = _rfc3339_to_naive_dt(b["start"])
                    be_dt = _rfc3339_to_naive_dt(b["end"])
                    if bs_dt is None or be_dt is None:
                        continue
                    if free_start_dt < bs_dt and (bs_dt - free_start_dt).total_seconds() >= 60:
                        start_label = time_min_s if free_start_dt == t_min else (free_start_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z")
                        free_slots_readable.append(
                            f"{_format_datetime_in_user_tz(start_label, user_tz)} - {_format_datetime_in_user_tz(b['start'], user_tz)}"
                        )
                    if be_dt > free_start_dt:
                        free_start_dt = be_dt
                if free_start_dt < t_max and (t_max - free_start_dt).total_seconds() >= 60:
                    free_slots_readable.append(
                        f"{_format_datetime_in_user_tz(free_start_dt.strftime('%Y-%m-%dT%H:%M:%S') + 'Z', user_tz)} - {_format_datetime_in_user_tz(time_max_s, user_tz)}"
                    )

        if not busy_slots_readable and not free_slots_readable:
            response = "No busy or free slots in the requested range."
        else:
            parts = []
            if busy_slots_readable:
                parts.append("Busy time slots:\n" + "\n".join(f"- {x}" for x in busy_slots_readable))
            if free_slots_readable:
                parts.append("Free time slots:\n" + "\n".join(f"- {x}" for x in free_slots_readable))
            response = "Summary of your calendar availability:\n\n" + "\n\n".join(parts)
        return {"success": True, "response": response, "freebusy": out}

    @tool(
        description=(
            "List all available color options that can be applied to Google Calendar events and calendars. "
            "Returns two sets of colors: event colors (used with color_id in create_event/update_event) "
            "and calendar colors (used when configuring a calendar's display color). "
            "Each color has a numeric ID, a display name, a background hex color, and a foreground hex color."
        ),
    )
    async def list_colors(self) -> Dict:
        data = await self._list_colors_raw()
        if not data:
            return {"success": False, "response": "Could not fetch colors."}
        event_colors = (data.get("event") or {}).copy()
        calendar_colors = (data.get("calendar") or {}).copy()
        return {
            "success": True,
            "response": f"Event colors: {len(event_colors)}; calendar colors: {len(calendar_colors)}.",
            "event_colors": event_colors,
            "calendar_colors": calendar_colors,
        }


# ---------------------------------------------------------------------------
# Module-level utility functions (unchanged from google_calendar_agent_tool)
# ---------------------------------------------------------------------------

def _calendar_date_resolution_response(user_query: str, raw_start: Any, raw_end: Any, err_resp: str) -> Dict:
    return {
        "success": False,
        "response": err_resp,
        "result": {"success": False, "response": err_resp},
    }


def _event_to_detail(ev: Dict) -> Dict:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    attendees = ev.get("attendees") or []
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "start_timeZone": start.get("timeZone"),
        "end_timeZone": end.get("timeZone"),
        "attendees": [a.get("email") for a in attendees if a.get("email")],
        "attendees_with_status": [
            {"email": a.get("email"), "responseStatus": a.get("responseStatus")}
            for a in attendees if a.get("email")
        ],
        "htmlLink": ev.get("htmlLink"),
        "status": ev.get("status"),
        "created": ev.get("created"),
        "updated": ev.get("updated"),
    }


def _get_machine_timezone_iana() -> str:
    try:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None and hasattr(local_tz, "key"):
            return getattr(local_tz, "key", "UTC") or "UTC"
    except Exception:
        pass
    return "UTC"


def _format_datetime_in_user_tz(s: Optional[str], user_tz_iana: Optional[str]) -> str:
    if not s or not isinstance(s, str) or "None" in s or "{" in s:
        return str(s) if s else ""
    tz_str = (user_tz_iana or "").strip() or _get_machine_timezone_iana()
    s_clean = s.strip().replace("Z", "+00:00")
    try:
        if "T" in s_clean:
            dt = datetime.fromisoformat(s_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if ZoneInfo:
                try:
                    local_dt = dt.astimezone(ZoneInfo(tz_str))
                    return local_dt.strftime("%b %d, %Y, %I:%M %p")
                except Exception:
                    pass
            return dt.strftime("%b %d, %Y, %I:%M %p")
        if len(s_clean) >= 10 and s_clean.count("-") >= 2:
            return datetime.strptime(s_clean[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except (ValueError, TypeError):
        pass
    return s[:25] if len(s) > 25 else s


def _format_event_created_response(ev: Dict, user_tz_iana: Optional[str] = None) -> str:
    parts = [f"Event created: {ev.get('summary') or 'Untitled'}."]
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    start_dt = start.get("dateTime") or start.get("date")
    end_dt = end.get("dateTime") or end.get("date")
    if start_dt:
        parts.append(f"Start: {_format_datetime_in_user_tz(start_dt, user_tz_iana)}.")
    if end_dt:
        parts.append(f"End: {_format_datetime_in_user_tz(end_dt, user_tz_iana)}.")
    if ev.get("location"):
        parts.append(f"Location: {ev.get('location')}.")
    attendees = ev.get("attendees") or []
    if attendees:
        emails = [a.get("email") for a in attendees if a.get("email")]
        if emails:
            parts.append(f"Attendees: {', '.join(emails)}.")
    if ev.get("htmlLink"):
        parts.append(f"Link: {ev.get('htmlLink')}")
    return " ".join(parts)


def _rfc3339_to_naive_dt(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str) or "None" in s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        if len(s) >= 10 and s.count("-") >= 2:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                pass
    return None


def _ranges_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    a, b = _rfc3339_to_naive_dt(start1), _rfc3339_to_naive_dt(end1)
    c, d = _rfc3339_to_naive_dt(start2), _rfc3339_to_naive_dt(end2)
    if a is None or b is None or c is None or d is None:
        return False
    return a < d and b > c


def _normalize_attendee_email(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("email")
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or "None" in s:
        return None
    if " <" in s and ">" in s:
        start = s.index(" <") + 2
        end = s.index(">", start)
        s = s[start:end].strip()
    if " " in s:
        for part in s.split():
            if "@" in part and " " not in part:
                s = part
                break
        else:
            s = s.replace(" ", "")
    if "@" not in s or " " in s or len(s) < 5:
        return None
    return s.strip()


def _extract_date_or_datetime_from_item(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        if isinstance(item, str) and len(item) >= 10 and item.count("-") >= 2:
            return item.strip()[:25]
        return None
    result = item.get("result")
    if isinstance(result, dict):
        for key in ("datetime_iso", "iso", "date", "start", "end"):
            val = result.get(key)
            if val and isinstance(val, str) and "None" not in val and "{" not in val:
                s = val.strip()
                if len(s) >= 10 and s.count("-") >= 2:
                    return s[:25] if "T" in s else s[:10]
    if result is not None and not isinstance(result, dict):
        s = str(result).strip()
        if len(s) >= 10 and s.count("-") >= 2 and "{" not in s:
            return s[:25] if "T" in s else s[:10]
    for key in ("start", "end", "date"):
        val = item.get(key)
        if val and isinstance(val, str) and "None" not in val and "{" not in val:
            s = val.strip()
            if len(s) >= 10 and s.count("-") >= 2:
                return s[:25] if "T" in s else s[:10]
    return None


def _merge_freebusy_times_from_provided_data(params: Dict, provided_data: Optional[Any]) -> None:
    if not provided_data:
        return
    items = provided_data if isinstance(provided_data, list) else [provided_data]
    resolved_base: Optional[str] = None
    for item in reversed(items):
        resolved_base = _extract_date_or_datetime_from_item(item)
        if resolved_base:
            break
    if not resolved_base:
        return
    base_date = resolved_base.split("T")[0][:10] if "T" in resolved_base else resolved_base[:10]
    time_min = params.get("time_min") or params.get("timeMin")
    time_max = params.get("time_max") or params.get("timeMax")
    has_placeholder = (isinstance(time_min, str) and "{" in time_min) or (isinstance(time_max, str) and "{" in time_max)
    missing = not time_min or not time_max
    if not has_placeholder and not missing:
        return
    placeholders = ("{step1_result}", "{step_1_result}", "{previous_result}", "{date}")
    if has_placeholder:
        if isinstance(time_min, str) and "{" in time_min:
            for ph in placeholders:
                if ph in time_min:
                    params["time_min"] = time_min.replace(ph, base_date)
                    break
            else:
                params["time_min"] = f"{base_date}T00:00:00Z"
        if isinstance(time_max, str) and "{" in time_max:
            for ph in placeholders:
                if ph in time_max:
                    params["time_max"] = time_max.replace(ph, base_date)
                    break
            else:
                params["time_max"] = f"{base_date}T23:59:59Z"
    if missing:
        if not params.get("time_min"):
            params["time_min"] = f"{base_date}T00:00:00Z"
        if not params.get("time_max"):
            params["time_max"] = f"{base_date}T23:59:59Z"


def _merge_start_end_from_provided_data(params: Dict, provided_data: Optional[Any]) -> None:
    if not provided_data or not isinstance(provided_data, list):
        return
    for item in reversed(provided_data):
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        start_val = item.get("start")
        end_val = item.get("end")
        if isinstance(result, dict):
            start_val = start_val or result.get("start") or result.get("datetime_iso") or result.get("iso") or result.get("date")
            end_val = end_val or result.get("end") or result.get("datetime_iso") or result.get("iso") or result.get("date")
        elif result is not None and not isinstance(result, dict):
            s = str(result).strip()
            if s and "None" not in s and not s.startswith("{"):
                start_val = start_val or s
                if end_val is None and start_val:
                    end_val = s
        if start_val is not None and str(start_val).strip() and "None" not in str(start_val):
            params["start"] = start_val if isinstance(start_val, str) else str(start_val)
        if end_val is not None and str(end_val).strip() and "None" not in str(end_val):
            params["end"] = end_val if isinstance(end_val, str) else str(end_val)
        if params.get("start") and params.get("end"):
            return


def _normalize_event_datetime(value: Any) -> Optional[Dict]:
    if value is None:
        return None
    if isinstance(value, dict):
        dt = value.get("dateTime")
        d = value.get("date")
        if dt is not None and str(dt) != "None":
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return {"dateTime": dt.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
            if isinstance(dt, date) and not isinstance(dt, datetime):
                return {"date": dt.isoformat()}
            s = str(dt).strip()
            if s and not s.startswith("{") and "None" not in s:
                return _str_to_event_datetime(s)
            return None
        if d is not None and str(d) != "None":
            s = str(d).strip()[:10]
            if len(s) == 10 and s.count("-") == 2:
                return {"date": s}
            return None
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return {"dateTime": value.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
    if isinstance(value, date) and not isinstance(value, datetime):
        return {"date": value.isoformat()}
    s = str(value).strip()
    if not s or "None" in s or s.startswith("{"):
        return None
    if len(s) <= 12 and s.count("-") >= 2 and "T" not in s:
        return {"date": s[:10]}
    return _str_to_event_datetime(s)


def _str_to_event_datetime(s: str) -> Optional[Dict]:
    if not s or "None" in s or s.startswith("{"):
        return None
    s = s.strip()
    if " " in s and "T" not in s and len(s) > 10:
        s = s.replace(" ", "T", 1)
    if "T" in s or (len(s) > 10 and s.count("-") >= 2):
        if not s.endswith("Z") and "+" not in s and (len(s) < 7 or s[-6] not in "+-"):
            s = s + "Z"
        return {"dateTime": s, "timeZone": "UTC"}
    if len(s) == 10 and s.count("-") == 2:
        return {"date": s}
    return None


def _parse_event_time_for_compare(
    start_val: Dict, end_val: Dict
) -> Optional[Tuple[Any, Any, bool]]:
    dt_s = start_val.get("dateTime") or start_val.get("date")
    dt_e = end_val.get("dateTime") or end_val.get("date")
    if not dt_s or not dt_e:
        return None
    is_all_day = bool(start_val.get("date") and not start_val.get("dateTime"))
    try:
        if is_all_day:
            return (
                datetime.strptime(dt_s[:10], "%Y-%m-%d").date(),
                datetime.strptime(dt_e[:10], "%Y-%m-%d").date(),
                True,
            )
        start_ob = datetime.fromisoformat(str(dt_s).replace("Z", "+00:00"))
        end_ob = datetime.fromisoformat(str(dt_e).replace("Z", "+00:00"))
        return (start_ob, end_ob, False)
    except (ValueError, TypeError):
        return None
