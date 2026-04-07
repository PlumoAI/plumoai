"""
Google Calendar Agent Tool.

List calendars, list/get/search events, create/update/delete events, respond to invitations,
free/busy query, list colors. Credentials from connected service.
Uses Google Calendar API v3 REST: https://developers.google.com/calendar/api/v3/reference

Reference (feature parity): https://github.com/nspady/google-calendar-mcp
- list_calendars, list_events, get_event, search_events
- create_event, update_event, delete_event
- respond_to_event (accept, decline, tentative)
- get_freebusy, get_current_time (use Calculator & DateTime for time), list_colors
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# Retry config for Calendar API (rate limit / transient errors)
_CALENDAR_REQUEST_MAX_RETRIES = 3
_CALENDAR_REQUEST_BASE_DELAY = 1.0
_CALENDAR_REQUEST_MAX_DELAY = 30.0

logger = logging.getLogger(__name__)

from backend.services.app_agents.base_tool_agent import BaseToolAgent

# Google Calendar API v3 base
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
DEFAULT_MAX_EVENTS = 50
MAX_EVENTS_CAP = 250


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content,
    }


class GoogleCalendarAgentTool(BaseToolAgent):
    """App agent for Google Calendar (app_code: google_calendar)."""

    TOOL_DESCRIPTION = """Google Calendar: list calendars, list/get/search events, create/update/delete, respond to invitations, free/busy, colors.
Supports LLM-driven intent (complex/vague queries), multi-step plans (e.g. get_freebusy then create_event), and optional natural-language summaries.

ACTIONS:
- list_calendars, list_events, get_event, search_events, create_event, update_event, delete_event, respond_to_event, get_freebusy, list_colors.
- create_event: calendar_id, summary, start, end; optional description, location, attendees (emails).
- update_event: calendar_id, event_id; optional summary, start, end, description, location.
- get_freebusy: timeMin, timeMax (RFC3339), calendar_ids. Use to check availability before creating events.

EVENT TIMES: start/end as RFC3339 or YYYY-MM-DD. timeMin/timeMax for list_events, search_events, get_freebusy.

App config (optional): calendar_check_conflicts=true to detect conflicts before create and suggest alternatives; calendar_multi_step_continue_on_failure=true to run all plan steps and aggregate results on partial failure; calendar_llm_summary=true for natural-language summaries; calendar_llm_summary_recommendations=true to include timing suggestions in summaries.
"""

    ACTION_DESCRIPTIONS = (
        "list_calendars=list available calendars; "
        "list_events=list events (calendar_id, timeMin?, timeMax?, maxResults?, q?); "
        "get_event=get event by id (calendar_id, event_id); "
        "search_events=search by query (calendar_id, q, timeMin?, timeMax?); "
        "create_event=create event (calendar_id, summary, start, end); "
        "update_event=update event (calendar_id, event_id, summary?/start?/end?); "
        "delete_event=delete event (calendar_id, event_id); "
        "respond_to_event=accept/decline/tentative (calendar_id, event_id, response_status); "
        "get_freebusy=availability (timeMin, timeMax, calendar_ids); "
        "list_colors=list event/calendar colors"
    )

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def __init__(
        self,
        llm_provider: Any,
        agent_id: Optional[str] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id or ""
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config or {}
        self._credentials_data = self.app_config.get("_google_calendar_credentials_data") or {}
        self._credentials = self._credentials_data.get("credentials") or {}
        self._access_token: Optional[str] = self._credentials.get("access_token")
        self._connected_service_id = self.app_config.get("connected_service_id")
        self._httpx_client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        if not self._connected_service_id or not self.token:
            return False
        url = f"{_AUTH_URL}/servicesCredentials/refresh/{self._connected_service_id}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self.company_id:
            headers["companyids"] = json.dumps([str(self.company_id)])
        if self.user_id is not None and str(self.user_id).strip():
            headers["userId"] = str(self.user_id).strip()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, headers=headers, json={})
                if r.status_code == 200:
                    data = r.json()
                    if data.get("type") == "success" and isinstance(data.get("data"), dict):
                        self._access_token = data["data"].get("access_token")
                        if self._access_token and self._httpx_client:
                            self._httpx_client.headers["Authorization"] = f"Bearer {self._access_token}"
                        return bool(self._access_token)
                if r.status_code >= 400 and self._credentials.get("refresh_token"):
                    return await self._refresh_via_google_oauth2()
        except Exception as e:
            logger.warning("Google Calendar token refresh failed: %s", e)
            if self._credentials.get("refresh_token"):
                return await self._refresh_via_google_oauth2()
        return False

    async def _refresh_via_google_oauth2(self) -> bool:
        refresh_token = self._credentials.get("refresh_token")
        client_id = self._credentials.get("client_id") or self._credentials.get("clientId")
        client_secret = self._credentials.get("client_secret") or self._credentials.get("clientSecret")
        if not refresh_token or not client_id or not client_secret:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    "https://oauth2.googleapis.com/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                if r.status_code != 200:
                    return False
                data = r.json()
                self._access_token = data.get("access_token")
                if self._access_token and self._httpx_client:
                    self._httpx_client.headers["Authorization"] = f"Bearer {self._access_token}"
                return bool(self._access_token)
        except Exception as e:
            logger.warning("Google Calendar OAuth2 refresh failed: %s", e)
        return False

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
        """path is relative to CALENDAR_API_BASE. Retries on 401 (after refresh), 429 (rate limit), and 5xx with exponential backoff."""
        url = f"{CALENDAR_API_BASE}{path}" if path.startswith("/") else f"{CALENDAR_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self._access_token}"
            return await self._calendar_request(method, path, json_body=json_body, params=params, retry_401=False)
        # Retry with exponential backoff on rate limit or server error
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _CALENDAR_REQUEST_MAX_RETRIES:
            delay = min(
                _CALENDAR_REQUEST_BASE_DELAY * (2 **_retry_count),
                _CALENDAR_REQUEST_MAX_DELAY,
            )
            logger.warning("Calendar API %s %s -> %s; retrying in %.1fs (%d/%d)", method, path, r.status_code, delay, _retry_count + 1, _CALENDAR_REQUEST_MAX_RETRIES)
            await asyncio.sleep(delay)
            return await self._calendar_request(method, path, json_body=json_body, params=params, retry_401=False, _retry_count=_retry_count + 1)
        if r.status_code >= 400:
            logger.warning("Calendar API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    body_str = json.dumps(json_body, default=str)[:2000]
                    logger.warning("Calendar API request body: %s", body_str)
                except Exception:
                    logger.warning("Calendar API request body: %s", json_body)
            return None
        if r.status_code == 204 or not r.content:
            logger.info("Calendar API %s %s -> %s (no body)", method, path, r.status_code)
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Calendar API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                logger.info("Calendar API %s %s -> %s (response size %s bytes)", method, path, r.status_code, len(r.content))
            return data
        except Exception:
            return None

    # ----- API layer -----
    async def _list_calendar_list(self) -> List[Dict]:
        data = await self._calendar_request("GET", "/users/me/calendarList")
        if not data or "items" not in data:
            return []
        return data.get("items", [])

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
    ) -> tuple:
        """Returns (events list, next_page_token)."""
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        path = f"/calendars/{cal_enc}/events"
        params = {"maxResults": min(max_results, MAX_EVENTS_CAP), "singleEvents": single_events}
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        if q:
            params["q"] = q
        if order_by and single_events:
            params["orderBy"] = order_by
        if page_token:
            params["pageToken"] = page_token
        data = await self._calendar_request("GET", path, params=params)
        if not data:
            return [], None
        return data.get("items", []), data.get("nextPageToken")

    async def _create_event(self, calendar_id: str, body: Dict) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        return await self._calendar_request("POST", f"/calendars/{cal_enc}/events", json_body=body)

    async def _update_event(self, calendar_id: str, event_id: str, body: Dict) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        return await self._calendar_request("PATCH", f"/calendars/{cal_enc}/events/{event_enc}", json_body=body)

    async def _delete_event(self, calendar_id: str, event_id: str) -> bool:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        res = await self._calendar_request("DELETE", f"/calendars/{cal_enc}/events/{event_enc}")
        return res is not None  # 204 returns {}

    async def _get_event(self, calendar_id: str, event_id: str) -> Optional[Dict]:
        import urllib.parse
        cal_enc = urllib.parse.quote(calendar_id, safe="")
        event_enc = urllib.parse.quote(event_id, safe="")
        return await self._calendar_request("GET", f"/calendars/{cal_enc}/events/{event_enc}")

    async def _freebusy_query(self, body: Dict) -> Optional[Dict]:
        """POST freeBusy with body: timeMin, timeMax, items: [{ id: calendarId }, ...]."""
        return await self._calendar_request("POST", "/freeBusy", json_body=body)

    async def _list_colors(self) -> Optional[Dict]:
        return await self._calendar_request("GET", "/colors")

    async def _respond_to_event(
        self, calendar_id: str, event_id: str, response_status: str, attendee_email: Optional[str] = None
    ) -> Optional[Dict]:
        """Set attendee response (accepted, declined, tentative). If attendee_email omitted, use primary calendar id."""
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
        for a in ev.get("attendees", []):
            att = dict(a) if isinstance(a, dict) else {"email": str(a)}
            if (att.get("email") or "").lower() == email.lower():
                att["responseStatus"] = status
            new_attendees.append(att)
        return await self._update_event(calendar_id, event_id, {"attendees": new_attendees})

    async def _primary_calendar_id(self) -> Optional[str]:
        """Primary calendar id (often the user's email in Google)."""
        items = await self._list_calendar_list()
        for cal in items or []:
            if cal.get("primary"):
                return cal.get("id")
        return (items or [{}])[0].get("id") if items else None

    async def _llm_generate(self, prompt: str, max_tokens: int = 600) -> Optional[str]:
        """Call LLM for intent/summary. Returns response text or None."""
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return None
        try:
            gen = self.llm_provider.generate(prompt, max_tokens=max_tokens)
            if gen is None:
                return None
            if hasattr(gen, "__aiter__"):
                out = ""
                async for chunk in gen:
                    if isinstance(chunk, dict) and "text" in chunk:
                        out += chunk.get("text", "")
                    elif isinstance(chunk, str):
                        out += chunk
                return out.strip() or None
            if isinstance(gen, str):
                return gen.strip() or None
        except Exception as e:
            logger.debug("Google Calendar LLM call failed: %s", e)
        return None

    async def _interpret_intent_with_llm(
        self, user_query: str, tool_args: Dict, provided_data: Optional[Any]
    ) -> Optional[Dict]:
        """
        Use LLM to interpret user intent into a single action or a multi-step plan.
        Returns {"action": str, "params": dict} or {"plan": [{"action": str, "params": dict}, ...]} or None.
        Uses richer context (more prior results, longer snippets) and encourages proactive plans
        (e.g. get_freebusy before create when availability/conflict is mentioned).
        """
        context_parts = [f"User query: {str(user_query or '').strip()[:500]}"]
        context_parts.append(f"Tool args (from planner): {json.dumps({k: v for k, v in tool_args.items() if k != 'step_action'}, default=str)[:600]}")
        if provided_data:
            items = provided_data if isinstance(provided_data, list) else [provided_data]
            for i, item in enumerate(items[:6]):  # up to 6 prior steps
                if not isinstance(item, dict):
                    continue
                action = item.get("operation") or item.get("action") or "unknown"
                success = item.get("success", True)
                res = item.get("result")
                if isinstance(res, dict):
                    keys = list(res.keys())[:12]
                    snippet = json.dumps({k: res.get(k) for k in keys if res.get(k) is not None}, default=str)[:400]
                    context_parts.append(f"Prior step {i + 1} (action={action}, success={success}): result keys {keys}; snippet: {snippet}")
                elif res is not None:
                    context_parts.append(f"Prior step {i + 1} (action={action}, success={success}): {str(res)[:350]}")
        prompt = """You are a Google Calendar assistant. Given the user query and context, output a single JSON object.

Actions: list_calendars, list_events, get_event, search_events, create_event, update_event, delete_event, respond_to_event, get_freebusy, list_colors.

PROACTIVE PLANNING:
- When the user mentions availability, conflicts, "free", "busy", or scheduling with others: prefer a multi-step plan that starts with get_freebusy (time_min, time_max, calendar_ids) then create_event or update_event using that context.
- When rescheduling or moving events: consider get_event then update_event, or list_events then update_event.
- For "find a slot" or "suggest a time": use get_freebusy first, then create_event with a slot that fits.

Output (single action):
{"action": "<action>", "params": {"calendar_id": "primary", "summary": "...", "start": "RFC3339", "end": "RFC3339", "attendees": ["email"], "event_id": "...", "time_min": "...", "time_max": "...", "calendar_ids": ["primary"], "q": "...", ...}}

Or (multi-step plan):
{"plan": [{"action": "get_freebusy", "params": {"time_min": "...", "time_max": "...", "calendar_ids": ["primary"]}}, {"action": "create_event", "params": {"summary": "...", "start": "...", "end": "...", ...}}]}

Rules: calendar_id/calendar_ids "primary" when unspecified. start/end RFC3339 or YYYY-MM-DD. Attendees as email strings. Output ONLY valid JSON, no markdown.

Context:
"""
        prompt += "\n".join(context_parts)
        prompt += "\n\nJSON:"
        out = await self._llm_generate(prompt, max_tokens=900)
        if not out:
            return None
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix):].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            obj = json.loads(out)
            if isinstance(obj, dict) and (obj.get("action") or obj.get("plan")):
                return obj
        except json.JSONDecodeError:
            logger.debug("Calendar intent LLM returned invalid JSON: %s", out[:200])
        return None

    # ----- Action decision -----
    async def _decide_action(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        tool_args = tool_args or {}
        step_action = (tool_args.get("step_action") or "").lower()
        q_lower = (user_query or "").lower()
        # list_colors
        if "color" in step_action or "color" in q_lower or tool_args.get("action") == "list_colors":
            return {"action": "list_colors", "params": {}}
        # get_freebusy: timeMin, timeMax, calendar_ids (resolve placeholders like {step1_result} from provided_data)
        if tool_args.get("timeMin") is not None and tool_args.get("timeMax") is not None:
            cids = tool_args.get("calendar_ids")
            if not cids and tool_args.get("calendar_id"):
                cids = [str(tool_args["calendar_id"])]
            if not cids:
                cids = ["primary"]
            params = {
                "time_min": tool_args.get("timeMin") or tool_args.get("time_min"),
                "time_max": tool_args.get("timeMax") or tool_args.get("time_max"),
                "calendar_ids": cids if isinstance(cids, list) else [cids],
            }
            _merge_freebusy_times_from_provided_data(params, provided_data)
            if params.get("time_min") and params.get("time_max") and "{" not in str(params.get("time_min")) and "{" not in str(params.get("time_max")):
                return {"action": "get_freebusy", "params": params}
        # Explicit from plan
        if tool_args.get("calendar_id") and (tool_args.get("event_id") or tool_args.get("eventId")):
            if "delete" in step_action or tool_args.get("action") == "delete_event":
                return {
                    "action": "delete_event",
                    "params": {
                        "calendar_id": str(tool_args["calendar_id"]),
                        "event_id": str(tool_args.get("event_id") or tool_args.get("eventId")),
                    },
                }
            # respond_to_event: accept, decline, tentative
            rs = (tool_args.get("response_status") or "").lower()
            if rs in ("accepted", "declined", "tentative") or "accept" in step_action or "decline" in step_action or "tentative" in step_action:
                return {
                    "action": "respond_to_event",
                    "params": {
                        "calendar_id": str(tool_args["calendar_id"]),
                        "event_id": str(tool_args.get("event_id") or tool_args.get("eventId")),
                        "response_status": rs or ("accepted" if "accept" in step_action else "declined" if "decline" in step_action else "tentative"),
                        "attendee_email": tool_args.get("attendee_email"),
                    },
                }
            # get_event: fetch single event by id
            if tool_args.get("action") == "get_event" or "get event" in step_action or "fetch event" in step_action or "event details" in step_action:
                return {
                    "action": "get_event",
                    "params": {
                        "calendar_id": str(tool_args["calendar_id"]),
                        "event_id": str(tool_args.get("event_id") or tool_args.get("eventId")),
                    },
                }
            if "update" in step_action or "edit" in step_action or tool_args.get("action") == "update_event":
                params = {
                    "calendar_id": str(tool_args["calendar_id"]),
                    "event_id": str(tool_args.get("event_id") or tool_args.get("eventId")),
                }
                for k in ("summary", "start", "end", "description", "location"):
                    if tool_args.get(k) is not None:
                        params[k] = tool_args[k]
                _merge_start_end_from_provided_data(params, provided_data)
                return {"action": "update_event", "params": params}
        if tool_args.get("calendar_id") and (tool_args.get("summary") or tool_args.get("start") or tool_args.get("end")):
            if "create" in step_action or "add" in step_action or not tool_args.get("event_id"):
                params = {"calendar_id": str(tool_args["calendar_id"])}
                for k in ("summary", "start", "end", "description", "location", "attendees"):
                    if tool_args.get(k) is not None:
                        params[k] = tool_args[k]
                # Merge start/end from previous steps (e.g. Calculator & DateTime) when present
                _merge_start_end_from_provided_data(params, provided_data)
                return {"action": "create_event", "params": params}
        if tool_args.get("calendar_id") and tool_args.get("q") and (
            tool_args.get("action") == "search_events" or "search" in step_action
        ):
            params = {"calendar_id": str(tool_args.get("calendar_id") or "primary"), "q": str(tool_args["q"])}
            if tool_args.get("timeMin"):
                params["time_min"] = tool_args["timeMin"]
            if tool_args.get("timeMax"):
                params["time_max"] = tool_args["timeMax"]
            if tool_args.get("maxResults"):
                params["max_results"] = int(tool_args["maxResults"])
            return {"action": "search_events", "params": params}
        if tool_args.get("calendar_id") or "list event" in step_action or "list event" in (user_query or "").lower():
            params = {"calendar_id": str(tool_args.get("calendar_id") or "primary")}
            if tool_args.get("timeMin"):
                params["time_min"] = tool_args["timeMin"]
            if tool_args.get("timeMax"):
                params["time_max"] = tool_args["timeMax"]
            if tool_args.get("maxResults"):
                params["max_results"] = int(tool_args["maxResults"])
            if tool_args.get("q"):
                params["q"] = str(tool_args["q"])
            return {"action": "list_events", "params": params}
        if "list calendar" in step_action or "list calendar" in (user_query or "").lower() or not (tool_args.get("calendar_id") or tool_args.get("event_id")):
            return {"action": "list_calendars", "params": {}}
        # Default: try LLM for semantic intent when query is non-trivial, else list events on primary
        if (user_query or "").strip() and len((user_query or "").strip()) > 10:
            interpreted = await self._interpret_intent_with_llm(user_query, tool_args, provided_data)
            if interpreted:
                if interpreted.get("plan") and isinstance(interpreted["plan"], list) and len(interpreted["plan"]) > 0:
                    return {"action": "_multi_step", "plan": interpreted["plan"], "params": {}}
                if interpreted.get("action") and interpreted.get("params") is not None:
                    # Merge tool_args into params so planner-provided values (e.g. calendar_id, attendees) are kept
                    params = dict(tool_args)
                    params.update({k: v for k, v in interpreted["params"].items() if v is not None})
                    _merge_start_end_from_provided_data(params, provided_data)
                    return {"action": interpreted["action"], "params": params}
        return {"action": "list_events", "params": {"calendar_id": "primary", "max_results": DEFAULT_MAX_EVENTS}}

    # ----- Handlers -----
    async def _do_list_calendars(self, user_query: str, params: Dict) -> Dict:
        items = await self._list_calendar_list()
        if not items:
            return {"success": True, "response": "No calendars found.", "calendars": [], "count": 0}
        out = []
        for cal in items:
            out.append({
                "id": cal.get("id"),
                "summary": cal.get("summary"),
                "primary": cal.get("primary", False),
                "accessRole": cal.get("accessRole"),
            })
        return {"success": True, "response": f"Found {len(out)} calendar(s).", "calendars": out, "count": len(out)}

    async def _do_list_events(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        time_min = params.get("time_min") or params.get("timeMin")
        time_max = params.get("time_max") or params.get("timeMax")
        max_results = int(params.get("max_results") or params.get("maxResults") or DEFAULT_MAX_EVENTS)
        max_results = min(max_results, MAX_EVENTS_CAP)
        q = params.get("q")
        events, _ = await self._list_events(
            calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            q=q,
        )
        if not events:
            return {"success": True, "response": "No events found.", "events": [], "count": 0}
        out = []
        lines = []
        for i, ev in enumerate(events):
            start = ev.get("start") or {}
            end = ev.get("end") or {}
            start_str = start.get("dateTime") or start.get("date")
            end_str = end.get("dateTime") or end.get("date")
            title = ev.get("summary") or "Untitled"
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
            user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
            time_part = f"{_format_datetime_in_user_tz(start_str, user_tz)} to {_format_datetime_in_user_tz(end_str, user_tz)}" if (start_str and end_str) else "Time not set"
            lines.append(f"Meeting: {title}\nTime: {time_part}")
        response = f"There are {len(out)} meeting(s) scheduled:\n\n" + "\n\n".join(lines)
        return {"success": True, "response": response, "events": out, "count": len(out)}

    def _build_event_body(self, params: Dict) -> Dict:
        body = {}
        if params.get("summary") is not None:
            body["summary"] = str(params["summary"])
        if params.get("description") is not None:
            body["description"] = str(params["description"])
        if params.get("location") is not None:
            body["location"] = str(params["location"])
        start = _normalize_event_datetime(params.get("start"))
        end = _normalize_event_datetime(params.get("end"))
        if start is not None:
            body["start"] = start
        if end is not None:
            body["end"] = end
        if params.get("attendees"):
            att = params["attendees"]
            raw = att if isinstance(att, list) else [att]
            emails = []
            for e in raw:
                email = _normalize_attendee_email(e)
                if email:
                    emails.append({"email": email})
                else:
                    logger.warning("Calendar: skipped invalid attendee %r", e)
            if emails:
                body["attendees"] = emails
        return body

    async def _do_create_event(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        body = self._build_event_body(params)
        # Validate start/end: must be present and valid RFC3339 or date (no None, no stringified dicts)
        start_val = body.get("start")
        end_val = body.get("end")
        raw_start = params.get("start")
        raw_end = params.get("end")
        err_resp = "Start and end date/time are required and must be valid. Use RFC3339 (e.g. 2026-02-19T14:00:00Z) or YYYY-MM-DD for all-day."
        if not start_val or not end_val:
            return _calendar_date_resolution_response(user_query, raw_start, raw_end, err_resp)
        dt_start = start_val.get("dateTime") or start_val.get("date")
        dt_end = end_val.get("dateTime") or end_val.get("date")
        if not dt_start or not dt_end or "None" in str(dt_start) or "None" in str(dt_end) or str(dt_start).startswith("{"):
            err_resp = "Start and end must be valid RFC3339 datetime strings (e.g. 2026-02-19T14:00:00Z) or YYYY-MM-DD. Invalid or missing values were provided."
            return _calendar_date_resolution_response(user_query, raw_start, raw_end, err_resp)
        # Ensure end is after start (API returns timeRangeEmpty otherwise)
        parsed = _parse_event_time_for_compare(start_val, end_val)
        if parsed:
            start_ob, end_ob, is_all_day = parsed
            if end_ob <= start_ob:
                if not is_all_day and start_val.get("dateTime"):
                    # Timed event: set end to start + 1 hour
                    start_dt = datetime.fromisoformat(str(dt_start).replace("Z", "+00:00"))
                    end_dt = start_dt + timedelta(hours=1)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    end_iso = end_dt.isoformat().replace("+00:00", "Z")
                    body["end"] = {"dateTime": end_iso, "timeZone": "UTC"}
                    logger.info("Calendar: end was before start; set end to start + 1 hour: %s", end_iso)
                else:
                    return {
                        "success": False,
                        "response": "End date/time must be after start. The specified time range was invalid (end <= start).",
                        "result": {"success": False},
                    }
        if not body.get("summary"):
            body["summary"] = "Untitled Event"
        # Optional: check for conflicts before creating (app_config calendar_check_conflicts)
        if self.app_config.get("calendar_check_conflicts") and dt_start and dt_end:
            busy_body = {"timeMin": dt_start, "timeMax": dt_end, "items": [{"id": calendar_id}]}
            freebusy_data = await self._freebusy_query(busy_body)
            if freebusy_data:
                cal_busy = (freebusy_data.get("calendars") or {}).get(calendar_id) or {}
                busy_periods = cal_busy.get("busy") or []
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
                        "response": f"The requested time conflicts with existing event(s) on your calendar: {conflict_summary}. Please choose another time.",
                        "conflict_detected": True,
                        "conflicts": conflicts[:10],
                        "need_context": True,
                        "suggested_context_steps": [
                            {
                                "tool_name": "Calculator & DateTime",
                                "query": f"User requested: {str(user_query or '')[:200]}. Find an alternative 1-hour slot today or this week (avoid conflicts). Return start and end as RFC3339.",
                                "action": "Suggest alternative slot",
                            },
                            {
                                "tool_name": "Google Calendar",
                                "query": f"Check availability (get_freebusy) for calendar {calendar_id} for a wider time range, then create the event in a free slot.",
                                "action": "get_freebusy then create_event",
                            },
                        ],
                        "result": {"success": False},
                    }
        created = await self._create_event(calendar_id, body)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create the event."}
        event_detail = _event_to_detail(created)
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        response_text = _format_event_created_response(created, user_tz)
        llm_summary = await self._summarize_event_with_llm("create", {"event": event_detail, "response": response_text}, user_query)
        if llm_summary:
            response_text = llm_summary
        return {
            "success": True,
            "response": response_text,
            "event": event_detail,
        }

    async def _do_update_event(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        event_id = params.get("event_id") or params.get("eventId")
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        body = self._build_event_body(params)
        if not body:
            return {"success": False, "response": "Provide at least one field to update (summary, start, end, description, location)."}
        if body.get("start") and body.get("end"):
            parsed = _parse_event_time_for_compare(body["start"], body["end"])
            if parsed:
                start_ob, end_ob, is_all_day = parsed
                if end_ob <= start_ob:
                    if not is_all_day and body["start"].get("dateTime"):
                        start_dt = datetime.fromisoformat(
                            str(body["start"].get("dateTime")).replace("Z", "+00:00")
                        )
                        end_dt = start_dt + timedelta(hours=1)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        body["end"] = {"dateTime": end_dt.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
                        logger.info("Calendar update: end was before start; set end to start + 1 hour")
                    else:
                        return {
                            "success": False,
                            "response": "End date/time must be after start.",
                            "result": {"success": False},
                        }
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
        llm_summary = await self._summarize_event_with_llm("update", {"event": event_detail, "response": response_text}, user_query)
        if llm_summary:
            response_text = llm_summary
        return {"success": True, "response": response_text, "event": event_detail}

    async def _do_delete_event(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        event_id = params.get("event_id") or params.get("eventId")
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        ok = await self._delete_event(calendar_id, str(event_id))
        if not ok:
            return {"success": False, "response": "Could not delete the event."}
        response_text = "Event deleted."
        llm_summary = await self._summarize_event_with_llm("delete", {"event_id": event_id, "response": response_text}, user_query)
        if llm_summary:
            response_text = llm_summary
        return {"success": True, "response": response_text, "event_id": event_id}

    async def _do_get_event(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        event_id = params.get("event_id") or params.get("eventId")
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        ev = await self._get_event(calendar_id, str(event_id))
        if not ev:
            return {"success": False, "response": "Event not found.", "event_id": event_id}
        event_detail = _event_to_detail(ev)
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        start_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
        end_str = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date")
        response_text = f"Event: {ev.get('summary') or 'Untitled'}. "
        response_text += " ".join(
            p
            for p in [
                f"Start: {_format_datetime_in_user_tz(start_str, user_tz)}." if start_str else None,
                f"End: {_format_datetime_in_user_tz(end_str, user_tz)}." if end_str else None,
                f"Location: {ev.get('location')}." if ev.get("location") else None,
                f"Link: {ev.get('htmlLink')}" if ev.get("htmlLink") else None,
            ]
            if p
        )
        return {"success": True, "response": response_text, "event": event_detail}

    async def _do_search_events(self, user_query: str, params: Dict) -> Dict:
        """Search events by query (delegate to list_events with q)."""
        return await self._do_list_events(user_query, params)

    async def _do_respond_to_event(self, user_query: str, params: Dict) -> Dict:
        calendar_id = params.get("calendar_id") or "primary"
        event_id = params.get("event_id") or params.get("eventId")
        response_status = (params.get("response_status") or "").lower()
        if not event_id:
            return {"success": False, "response": "event_id is required."}
        if response_status not in ("accepted", "declined", "tentative"):
            return {"success": False, "response": "response_status must be accepted, declined, or tentative."}
        updated = await self._respond_to_event(
            calendar_id, str(event_id), response_status, params.get("attendee_email")
        )
        if not updated:
            return {"success": False, "response": "Could not update response (event not found or you are not an attendee)."}
        label = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentative"}[response_status]
        return {"success": True, "response": f"Invitation {label.lower()}.", "event": _event_to_detail(updated)}

    async def _do_get_freebusy(self, user_query: str, params: Dict) -> Dict:
        time_min = params.get("time_min") or params.get("timeMin")
        time_max = params.get("time_max") or params.get("timeMax")
        calendar_ids = params.get("calendar_ids") or []
        if not calendar_ids and params.get("calendar_id"):
            calendar_ids = [str(params["calendar_id"])]
        if not time_min or not time_max:
            return {"success": False, "response": "timeMin and timeMax (RFC3339) are required."}
        if not calendar_ids:
            return {"success": False, "response": "At least one calendar_id or calendar_ids is required."}
        # Reject placeholders or invalid RFC3339 (e.g. {step1_result}T00:00:00Z from planner)
        time_min_s = str(time_min).strip()
        time_max_s = str(time_max).strip()
        if "{" in time_min_s or "{" in time_max_s or _rfc3339_to_naive_dt(time_min_s) is None or _rfc3339_to_naive_dt(time_max_s) is None:
            return {
                "success": False,
                "response": "timeMin and timeMax must be valid RFC3339 (e.g. 2026-02-20T00:00:00Z). Placeholders like {step1_result} are not supported—run Calculator & DateTime first to get a date, then retry.",
                "need_context": True,
                "suggested_context_steps": [
                    {
                        "tool_name": "Calculator & DateTime",
                        "query": f"User request: {str(user_query or '')[:200]}. Return current date or the requested date as YYYY-MM-DD (or ISO datetime). Use for calendar free/busy range.",
                        "action": "Get date for freebusy timeMin/timeMax",
                    },
                ],
                "result": {"success": False},
            }
        body = {"timeMin": time_min_s, "timeMax": time_max_s, "items": [{"id": cid} for cid in calendar_ids]}
        data = await self._freebusy_query(body)
        if not data:
            return {"success": False, "response": "Could not fetch free/busy."}
        user_tz = (self.app_config.get("user_timezone") or "").strip() or _get_machine_timezone_iana()
        cal_map = data.get("calendars") or {}
        out = {}
        busy_slots_readable = []
        free_slots_readable = []
        for cid in calendar_ids:
            busy = (cal_map.get(cid) or {}).get("busy") or []
            slots = [{"start": b.get("start"), "end": b.get("end")} for b in busy if b.get("start") and b.get("end")]
            out[cid] = slots
            for b in slots:
                busy_slots_readable.append(f"{_format_datetime_in_user_tz(b['start'], user_tz)} - {_format_datetime_in_user_tz(b['end'], user_tz)}")
            # Compute free slots as gaps between time_min, time_max and busy ranges
            t_min = _rfc3339_to_naive_dt(time_min_s)
            t_max = _rfc3339_to_naive_dt(time_max_s)
            if t_min is not None and t_max is not None and t_min < t_max:
                # Sort by start, keep original strings for display
                sorted_slots = sorted(slots, key=lambda s: (_rfc3339_to_naive_dt(s["start"]) or datetime.min, _rfc3339_to_naive_dt(s["end"]) or datetime.max))
                free_start_dt = t_min
                for b in sorted_slots:
                    bs_dt = _rfc3339_to_naive_dt(b["start"])
                    be_dt = _rfc3339_to_naive_dt(b["end"])
                    if bs_dt is None or be_dt is None:
                        continue
                    if free_start_dt < bs_dt and (bs_dt - free_start_dt).total_seconds() >= 60:
                        start_str = time_min_s if free_start_dt == t_min else (free_start_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z")
                        free_slots_readable.append(f"{_format_datetime_in_user_tz(start_str, user_tz)} - {_format_datetime_in_user_tz(b['start'], user_tz)}")
                    if be_dt > free_start_dt:
                        free_start_dt = be_dt
                if free_start_dt < t_max and (t_max - free_start_dt).total_seconds() >= 60:
                    start_str = free_start_dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
                    free_slots_readable.append(f"{_format_datetime_in_user_tz(start_str, user_tz)} - {_format_datetime_in_user_tz(time_max_s, user_tz)}")
        if not busy_slots_readable and not free_slots_readable:
            response = "No busy or free slots in the requested range (or range is empty)."
        else:
            parts = []
            if busy_slots_readable:
                parts.append("Busy time slots:\n" + "\n".join(f"- {x}" for x in busy_slots_readable))
            if free_slots_readable:
                parts.append("Free time slots:\n" + "\n".join(f"- {x}" for x in free_slots_readable))
            response = "Summary of your calendar availability:\n\n" + "\n\n".join(parts)
        return {"success": True, "response": response, "freebusy": out}

    async def _do_list_colors(self, user_query: str, params: Dict) -> Dict:
        data = await self._list_colors()
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

    async def _summarize_event_with_llm(self, action_type: str, result: Dict, user_query: str) -> Optional[str]:
        """Generate a short natural-language summary; optionally include recommendations or warnings (e.g. conflict with lunch)."""
        if not self.app_config.get("calendar_llm_summary"):
            return None
        summary = (result.get("event") or result.get("response") or "")
        if isinstance(summary, dict):
            summary = json.dumps(summary, default=str)[:400]
        else:
            summary = str(summary)[:400]
        include_recommendations = self.app_config.get("calendar_llm_summary_recommendations", True)
        prompt = f"""User asked: "{str(user_query or '')[:200]}"
Calendar action: {action_type}. Result: {summary}
Reply with 1-2 short, friendly sentences for the user (e.g. "Your meeting with Alice is scheduled for Feb 19 at 2 PM.")."""
        if include_recommendations:
            prompt += """ If the time might be awkward (e.g. conflicts with typical lunch 12-1, or very early/late), add a brief recommendation (e.g. "Consider moving to 2:30 PM if that works better."). Do not invent conflicts; only suggest if the time could be improved."""
        prompt += " No preamble."
        out = await self._llm_generate(prompt, max_tokens=150)
        return (out.strip()[:400] if out else None) or None

    async def _run_one_action(self, action_type: str, params: Dict, user_query: str) -> Dict:
        """Execute a single calendar action and return the result dict. Used for multi-step plans."""
        action_type = (action_type or "").strip().lower()
        if action_type == "list_calendars":
            return await self._do_list_calendars(user_query, params)
        if action_type == "list_events":
            return await self._do_list_events(user_query, params)
        if action_type == "create_event":
            return await self._do_create_event(user_query, params)
        if action_type == "update_event":
            return await self._do_update_event(user_query, params)
        if action_type == "delete_event":
            return await self._do_delete_event(user_query, params)
        if action_type == "get_event":
            return await self._do_get_event(user_query, params)
        if action_type == "search_events":
            return await self._do_search_events(user_query, params)
        if action_type == "respond_to_event":
            return await self._do_respond_to_event(user_query, params)
        if action_type == "get_freebusy":
            return await self._do_get_freebusy(user_query, params)
        if action_type == "list_colors":
            return await self._do_list_colors(user_query, params)
        return {"success": False, "response": f"Unknown action: {action_type}."}

    async def initialize(self) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Google Calendar agent init: connected_service_id=%s user_id=%s", self._connected_service_id, self.user_id)
        if not self._access_token:
            logger.warning("Google Calendar Agent: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("Google Calendar Agent Tool initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        logger.debug("Google Calendar Agent Tool cleaned up")

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        try:
            if not self._access_token:
                err_result = {"success": False, "error": "Google Calendar not connected.", "query": user_query}
                yield event(AgentEvent.RESULT, err_result)
                yield event(AgentEvent.FINAL, {"success": False, "response": json.dumps(err_result, default=str), "result": err_result})
                return
            yield event(AgentEvent.THOUGHT, "Understanding your calendar request and choosing an action...")
            action_result = await self._decide_action(user_query, provided_data, tool_args=tool_args)
            action_type = action_result.get("action") or "list_calendars"
            params = action_result.get("params") or {}
            if action_type == "list_calendars":
                yield event(AgentEvent.PLAN, "Listing your calendars.")
                result = await self._do_list_calendars(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "list_events":
                yield event(AgentEvent.PLAN, "Listing events.")
                result = await self._do_list_events(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "create_event":
                yield event(AgentEvent.PLAN, "Creating the event.")
                result = await self._do_create_event(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "update_event":
                yield event(AgentEvent.PLAN, "Updating the event.")
                result = await self._do_update_event(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "delete_event":
                yield event(AgentEvent.PLAN, "Deleting the event.")
                result = await self._do_delete_event(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "get_event":
                yield event(AgentEvent.PLAN, "Fetching event details.")
                result = await self._do_get_event(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "search_events":
                yield event(AgentEvent.PLAN, "Searching events.")
                result = await self._do_search_events(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "respond_to_event":
                yield event(AgentEvent.PLAN, "Updating invitation response.")
                result = await self._do_respond_to_event(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "get_freebusy":
                yield event(AgentEvent.PLAN, "Checking availability.")
                result = await self._do_get_freebusy(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "list_colors":
                yield event(AgentEvent.PLAN, "Listing colors.")
                result = await self._do_list_colors(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return
            if action_type == "_multi_step":
                plan = action_result.get("plan") or []
                step_results: List[Dict] = []
                continue_on_failure = self.app_config.get("calendar_multi_step_continue_on_failure", False)
                for idx, step in enumerate(plan):
                    if not isinstance(step, dict):
                        continue
                    st_action = (step.get("action") or "").strip()
                    st_params = dict(step.get("params") or {})
                    _merge_start_end_from_provided_data(st_params, step_results)
                    _merge_freebusy_times_from_provided_data(st_params, step_results)
                    if not st_action:
                        continue
                    yield event(AgentEvent.PLAN, f"Step {idx + 1}/{len(plan)}: {st_action}.")
                    result = await self._run_one_action(st_action, st_params, user_query)
                    result["_step_index"] = idx + 1
                    result["_step_action"] = st_action
                    step_results.append(result)
                    yield event(AgentEvent.RESULT, result)
                    if not result.get("success", True) and not continue_on_failure:
                        break
                # Aggregate: partial success when any step failed but at least one ran
                success_count = sum(1 for r in step_results if r.get("success", False))
                failed_count = len(step_results) - success_count
                last = step_results[-1] if step_results else {"success": False, "error": "No steps executed."}
                if failed_count > 0 and success_count > 0:
                    combined = {
                        "success": False,
                        "partial_success": True,
                        "result": {
                            "partial_success": True,
                            "steps_completed": success_count,
                            "steps_failed": failed_count,
                            "step_results": step_results,
                            "last_result": last,
                        },
                    }
                else:
                    combined = {"success": last.get("success", False), "result": last}
                    if step_results and len(step_results) > 1:
                        combined["result"] = {"step_results": step_results, "last_result": last}
                raw_response = json.dumps(combined, default=str)
                yield event(AgentEvent.FINAL, {"success": combined.get("success", False), "response": raw_response, "result": combined.get("result", last)})
                return
            yield event(AgentEvent.PLAN, "Listing your calendars.")
            result = await self._do_list_calendars(user_query, {})
            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
        except Exception as e:
            logger.exception("Google Calendar agent run error: %s", e)
            err_result = {"success": False, "error": str(e), "query": user_query}
            yield event(AgentEvent.RESULT, err_result)
            yield event(AgentEvent.FINAL, {"success": False, "response": json.dumps(err_result, default=str), "result": {"success": False}})


def _calendar_date_resolution_response(user_query: str, raw_start: Any, raw_end: Any, err_resp: str) -> Dict:
    """Return need_context + suggested_context_steps so executor runs Calculator & DateTime then retries."""
    query_parts = [f"User request: {str(user_query or '').strip()[:300]}"]
    if raw_start is not None:
        query_parts.append(f"Start from request: {raw_start}")
    if raw_end is not None:
        query_parts.append(f"End from request: {raw_end}")
    query_parts.append(
        "Parse start and end into RFC3339 (e.g. 2026-02-19T14:00:00Z). Return JSON with keys 'start' and 'end' only."
    )
    return {
        "success": False,
        "response": err_resp,
        "need_context": True,
        "suggested_context_steps": [
            {
                "tool_name": "Calculator & DateTime",
                "query": " ".join(query_parts),
                "action": "Parse start/end dates (RFC3339) for calendar event",
            }
        ],
        "result": {
            "success": False,
            "response": err_resp,
            "need_context": True,
            "suggested_context_steps": [
                {
                    "tool_name": "Calculator & DateTime",
                    "query": " ".join(query_parts),
                    "action": "Parse start/end dates (RFC3339) for calendar event",
                }
            ],
        },
    }


def _event_to_detail(ev: Dict) -> Dict:
    """Build a structured event detail dict from Google Calendar API event (for sharing after create/update)."""
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    start_dt = start.get("dateTime") or start.get("date") or ""
    end_dt = end.get("dateTime") or end.get("date") or ""
    attendees = ev.get("attendees") or []
    attendee_emails = [a.get("email") for a in attendees if a.get("email")]
    attendees_with_status = [
        {"email": a.get("email"), "responseStatus": a.get("responseStatus")}
        for a in attendees
        if a.get("email")
    ]
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start": start_dt,
        "end": end_dt,
        "start_timeZone": start.get("timeZone"),
        "end_timeZone": end.get("timeZone"),
        "attendees": attendee_emails,
        "attendees_with_status": attendees_with_status,
        "htmlLink": ev.get("htmlLink"),
        "status": ev.get("status"),
        "created": ev.get("created"),
        "updated": ev.get("updated"),
    }


def _get_machine_timezone_iana() -> str:
    """Return the server's current (local) timezone as IANA name. Used when auth timezone is not present."""
    try:
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is not None and hasattr(local_tz, "key"):
            return getattr(local_tz, "key", "UTC") or "UTC"
    except Exception:
        pass
    return "UTC"


def _format_datetime_readable(s: Optional[str]) -> str:
    """Format RFC3339 or YYYY-MM-DD to human-readable (e.g. 'Feb 20, 2026, 2:00 PM' or 'Feb 20, 2026'). No timezone conversion."""
    if not s or not isinstance(s, str) or "None" in s or "{" in s:
        return str(s) if s else ""
    s = s.strip().replace("Z", "+00:00")
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%b %d, %Y, %I:%M %p") if hasattr(dt, "strftime") else s
        if len(s) >= 10 and s.count("-") >= 2:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
            return d.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        pass
    return s[:25] if len(s) > 25 else s


def _format_datetime_in_user_tz(s: Optional[str], user_tz_iana: Optional[str]) -> str:
    """
    Convert RFC3339/ISO datetime to user's local timezone and format for display.
    Uses user_tz_iana from auth (e.g. app_config.user_timezone); if missing, uses machine timezone.
    Date-only strings (YYYY-MM-DD) are formatted without conversion.
    """
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
                    user_tz = ZoneInfo(tz_str)
                    local_dt = dt.astimezone(user_tz)
                    return local_dt.strftime("%b %d, %Y, %I:%M %p")
                except Exception:
                    pass
            return dt.strftime("%b %d, %Y, %I:%M %p")
        if len(s_clean) >= 10 and s_clean.count("-") >= 2:
            d = datetime.strptime(s_clean[:10], "%Y-%m-%d")
            return d.strftime("%b %d, %Y")
    except (ValueError, TypeError):
        pass
    return _format_datetime_readable(s)


def _format_event_created_response(ev: Dict, user_tz_iana: Optional[str] = None) -> str:
    """Format a human-readable event summary to share after booking. Times in user local time."""
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
    """Parse RFC3339 or date string to naive datetime for comparison. Returns None if invalid."""
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
    """True if [start1,end1) overlaps [start2,end2). Uses naive datetime comparison."""
    a, b = _rfc3339_to_naive_dt(start1), _rfc3339_to_naive_dt(end1)
    c, d = _rfc3339_to_naive_dt(start2), _rfc3339_to_naive_dt(end2)
    if a is None or b is None or c is None or d is None:
        return False
    return a < d and b > c


def _normalize_attendee_email(value: Any) -> Optional[str]:
    """
    Normalize an attendee to a valid Google Calendar email string.
    Handles: "Name email@domain.com", "Name <email@domain.com>", or raw email.
    Invalid (e.g. with spaces) are fixed by taking the segment that contains @.
    Returns None if no valid email can be extracted.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("email")
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or "None" in s:
        return None
    # "Name <email@domain.com>"
    if " <" in s and ">" in s:
        start = s.index(" <") + 2
        end = s.index(">", start)
        s = s[start:end].strip()
    # "Name email@domain.com" or "Hussain Hussaintabani786@gmail.com" -> take part with @
    if " " in s:
        parts = s.split()
        for part in parts:
            if "@" in part and " " not in part:
                s = part
                break
        else:
            s = s.replace(" ", "")  # fallback: remove spaces
    if "@" not in s or " " in s or len(s) < 5:
        return None
    return s.strip()


def _extract_date_or_datetime_from_item(item: Any) -> Optional[str]:
    """Extract a YYYY-MM-DD or RFC3339 string from a prior step result item. Returns None if not found."""
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
    """Resolve time_min/time_max from prior step results when they are placeholders (e.g. {step1_result}) or missing. Mutates params."""
    if not provided_data:
        return
    items = provided_data if isinstance(provided_data, list) else [provided_data]
    resolved_base: Optional[str] = None  # YYYY-MM-DD or RFC3339
    for item in reversed(items):
        resolved_base = _extract_date_or_datetime_from_item(item)
        if resolved_base:
            break
    if not resolved_base:
        return
    # Normalize to date (YYYY-MM-DD) for full-day range, or keep datetime
    if "T" in resolved_base:
        base_date = resolved_base.split("T")[0][:10]
    else:
        base_date = resolved_base[:10]
    time_min = params.get("time_min") or params.get("timeMin")
    time_max = params.get("time_max") or params.get("timeMax")
    has_placeholder = isinstance(time_min, str) and "{" in time_min or isinstance(time_max, str) and "{" in time_max
    missing = not time_min or not time_max
    if not has_placeholder and not missing:
        return
    # Replace placeholder or set default range for the day
    if has_placeholder:
        # e.g. "{step1_result}T00:00:00Z" -> "2026-02-20T00:00:00Z"
        placeholders = ("{step1_result}", "{step_1_result}", "{previous_result}", "{date}")
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
    # Normalize key names for downstream
    if "timeMin" in params and "time_min" not in params:
        params["time_min"] = params["timeMin"]
    if "timeMax" in params and "time_max" not in params:
        params["time_max"] = params["timeMax"]


def _merge_start_end_from_provided_data(params: Dict, provided_data: Optional[Any]) -> None:
    """Merge start/end from previous step results (e.g. Calculator & DateTime) into params. Mutates params."""
    if not provided_data or not isinstance(provided_data, list):
        return
    for item in reversed(provided_data):
        if not isinstance(item, dict):
            continue
        result = item.get("result")
        start_val = item.get("start")
        end_val = item.get("end")
        if isinstance(result, dict):
            start_val = start_val or result.get("start")
            end_val = end_val or result.get("end")
            # Calculator & DateTime returns iso, date, or datetime_iso (no start/end keys)
            if start_val is None:
                start_val = (
                    result.get("start")
                    or result.get("datetime_iso")
                    or result.get("iso")
                    or result.get("date")
                )
            if end_val is None:
                end_val = result.get("end") or result.get("datetime_iso") or result.get("iso") or result.get("date")
        elif result is not None and not isinstance(result, dict):
            # Previous step returned a plain value (e.g. date string from Calculator)
            s = str(result).strip()
            if s and "None" not in s and not s.startswith("{"):
                start_val = start_val or s
                if end_val is None and start_val:
                    end_val = s  # same value for end; planner/NLP can interpret as same-day or add duration
        if start_val is not None and str(start_val).strip() and "None" not in str(start_val):
            params["start"] = start_val if isinstance(start_val, str) else str(start_val)
        if end_val is not None and str(end_val).strip() and "None" not in str(end_val):
            params["end"] = end_val if isinstance(end_val, str) else str(end_val)
        if params.get("start") and params.get("end"):
            return


def _normalize_event_datetime(value: Any) -> Optional[Dict]:
    """
    Normalize start/end to Google Calendar format. Returns a dict with either
    dateTime (RFC3339 string) + timeZone, or date (YYYY-MM-DD for all-day).
    Returns None if value is missing or invalid (caller must validate).
    """
    if value is None:
        return None
    # Already a dict from upstream (e.g. planner) — extract valid dateTime or date only
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
    # Python datetime -> RFC3339
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return {"dateTime": dt.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"}
    # Python date -> all-day
    if isinstance(value, date) and not isinstance(value, datetime):
        return {"date": value.isoformat()}
    # String: must be RFC3339 or YYYY-MM-DD
    s = str(value).strip()
    if not s or "None" in s or s.startswith("{"):
        return None
    if len(s) <= 12 and s.count("-") >= 2 and "T" not in s:
        return {"date": s[:10]}
    return _str_to_event_datetime(s)


def _str_to_event_datetime(s: str) -> Optional[Dict]:
    """Convert a string to {dateTime: RFC3339, timeZone: 'UTC'}."""
    if not s or "None" in s or s.startswith("{"):
        return None
    s = s.strip()
    # RFC3339 uses T between date and time; normalize space to T
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
    """
    Parse start/end event dicts to comparable values. Returns (start_ob, end_ob, is_all_day)
    or None if parsing fails. For dateTime compares as datetime; for date as date.
    """
    dt_s = start_val.get("dateTime") or start_val.get("date")
    dt_e = end_val.get("dateTime") or end_val.get("date")
    if not dt_s or not dt_e:
        return None
    is_all_day = bool(start_val.get("date") and not start_val.get("dateTime"))
    try:
        if is_all_day:
            start_ob = datetime.strptime(dt_s[:10], "%Y-%m-%d").date()
            end_ob = datetime.strptime(dt_e[:10], "%Y-%m-%d").date()
        else:
            start_ob = datetime.fromisoformat(str(dt_s).replace("Z", "+00:00"))
            end_ob = datetime.fromisoformat(str(dt_e).replace("Z", "+00:00"))
        return (start_ob, end_ob, is_all_day)
    except (ValueError, TypeError):
        return None
