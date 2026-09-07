"""
Calculator & DateTime Agent Tool (app_code: calculator_datetime)

Built-in app agent that is always part of the AI agent. Provides:
- Math arithmetic: add, subtract, multiply, divide, modulo, power, integer division (and safe expression evaluation)
- Current date and time (with optional timezone)
- Date/time calculations: add/subtract days, difference between dates, parse and format dates
- Multi-step reasoning, LLM date parsing fallback, and safer AST-based expression evaluation.
"""

import ast
import json
import logging
import math
import re
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Safe math functions for AST-based expression evaluation (no eval of arbitrary code)
_SAFE_MATH = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "factorial": math.factorial,
    "floor": math.floor,
    "ceil": math.ceil,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
}
_ALLOWED_BINOP = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOP = (ast.UAdd, ast.USub)

# Strict ISO date/datetime for LLM fallback (no trailing text)
_ISO_STRICT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?Z?$")

# Use zoneinfo for timezone names (Python 3.9+)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# Optional: dateutil for natural language dates (e.g. "tomorrow at 10 PM")
try:
    from dateutil import parser as dateutil_parser
    _HAS_DATEUTIL = True
except ImportError:
    dateutil_parser = None  # type: ignore
    _HAS_DATEUTIL = False


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
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "content": content,
    }


from backend.services.ai_agents.base_tool_agent import BaseToolAgent


class CalculatorDatetimeAgentTool(BaseToolAgent):
    """
    Calculator & DateTime agent: arithmetic and date/time operations.
    Always available as part of the AI agent; no credentials required.
    """

    TOOL_NAME = "Calculator & DateTime"
    APP_CODE = "calculator_datetime"

    TOOL_DESCRIPTION = """MANDATORY: Always call this tool FIRST whenever the task requires the current date/time, or any date/time calculation relative to today (tomorrow, next week, N days from now, scheduling, deadlines, time differences, business days, etc.). Also handles arithmetic and logic.

Calculator & DateTime: math arithmetic and date/time operations (always available). Accepts JSON or query-string from planner (e.g. {"operation":"current_datetime","format":"iso8601"} or operation=parse&date=tomorrow).

MATH: operation="arithmetic", expression or a,b,op. Functions: sqrt, log, sin, cos, etc.

CURRENT DATE/TIME (operation=current_datetime or current_date/current_time/current_timezone):
- Returns: date (YYYY-MM-DD), time (HH:MM:SS), timezone, iso (RFC3339), weekday.
- Optional format: iso8601|iso|rfc3339 (full ISO), yyyy-mm-dd|date (date only), time (time only), timezone|tz.
- Optional timezone param (e.g. Asia/Tokyo). single_step with action=current_datetime is supported.

DATE/TIME: operation="date_calculate", action: add_days, subtract_days, add_business_days, subtract_business_days, add_minutes, subtract_minutes, difference, parse, format, day_of_week. Use "days" or "value" for add_days; "minutes" for add_minutes/subtract_minutes; "format" or "value" for format. Natural language date_string (e.g. "tomorrow", "next Monday") supported. For add_minutes/subtract_minutes, "date" can come from previous step (current_datetime result).

MULTI-STEP: operation="multi_step", steps: [{"operation":"current_datetime"}, {"operation":"date_calculate", "action":"add_days", "days":1}, ...]. Previous step result fills date when missing.
"""

    _REQUIRED_DATE_FIELDS = {
        "add_days": ["date", "days"],
        "subtract_days": ["date", "days"],
        "add_business_days": ["date", "days"],
        "subtract_business_days": ["date", "days"],
        "add_minutes": ["date", "minutes"],
        "subtract_minutes": ["date", "minutes"],
        "difference": ["date_a", "date_b"],
        "format": ["date"],
        "day_of_week": ["date"],
        "parse": ["date_string"],
    }

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def get_description(self) -> str:
        return self.get_tool_responsibility()

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

    async def _llm_generate(self, prompt: str, max_tokens: int = 800) -> Optional[str]:
        """Call LLM and return single string output. Used to interpret natural language into structured params."""
        if not self.llm_provider:
            return None
        try:
            # Prefer get_response (standard provider interface); fall back to generate if present
            if hasattr(self.llm_provider, "get_response"):
                result = await self.llm_provider.get_response(prompt, max_tokens=max_tokens)
                if result is None:
                    return None
                if isinstance(result, str):
                    return result.strip() or None
                if isinstance(result, dict):
                    return (result.get("text") or result.get("content") or result.get("response") or "").strip() or None
                return str(result).strip() or None
            if hasattr(self.llm_provider, "generate"):
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
            logger.debug("Calculator/Datetime LLM interpret failed: %s", e)
        return None

    def _parse_json_query(self, query: str) -> Optional[Dict[str, Any]]:
        """Parse JSON query from planner (e.g. {"operation":"current_datetime","format":"iso8601"}). Returns dict to merge into args or None."""
        if not query or not isinstance(query, str):
            return None
        s = query.strip()
        if not s.startswith("{") or not s.endswith("}"):
            return None
        # Reject extremely deep or large JSON to avoid RecursionError in json.loads
        if len(s) > 100_000:
            return None
        depth = 0
        for ch in s:
            if ch in "{[":
                depth += 1
                if depth > 100:
                    return None
            elif ch in "}]":
                depth -= 1
        try:
            obj = json.loads(s)
            if not isinstance(obj, dict):
                return None
            out = {}
            for k, v in obj.items():
                if v is None:
                    continue
                key = str(k).strip().lower()
                if key == "operation":
                    out["operation"] = str(v).strip().lower() if v else ""
                elif key in ("action", "date_action"):
                    out["action"] = str(v).strip() if v else ""
                elif key in ("date", "date_string"):
                    out["date_string"] = str(v).strip()
                    out["date"] = out["date_string"]
                elif key in ("timezone", "tz"):
                    out["timezone"] = str(v).strip()
                elif key == "format":
                    out["format"] = str(v).strip()
                elif key == "days":
                    try:
                        out["days"] = int(float(v))
                    except (ValueError, TypeError):
                        out["days"] = v
                elif key == "value":
                    # Planner often sends value for days or format
                    out["value"] = v
                elif key == "steps" and isinstance(v, list):
                    out["steps"] = v
                elif key == "expression":
                    out["expression"] = str(v).strip()
            return out if out else None
        except (json.JSONDecodeError, RecursionError):
            return None

    def _parse_querystring_params(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Parse query-string style input (e.g. operation=date_parse&action=parse&date=tomorrow)
        from resolver/planner into structured args. Returns dict to merge into args, or None.
        """
        if not query or not isinstance(query, str):
            return None
        s = query.strip()
        if "operation=" not in s and "action=" not in s:
            return None
        out = {}
        for part in re.split(r"[\s&]+", s):
            if "=" in part:
                key, _, value = part.partition("=")
                key = key.strip().lower()
                value = value.strip().strip('"\'')
                if not key or value is None:
                    continue
                if key == "operation":
                    if value in ("date_parse", "parse_date", "date_calc"):
                        out["operation"] = "date_calculate"
                    elif value in ("current_datetime", "current_date", "current_time", "current_timezone", "now"):
                        out["operation"] = "current_datetime"
                    elif value == "single_step":
                        out["operation"] = "single_step"
                    else:
                        out["operation"] = value
                elif key == "action" or key == "date_action":
                    out["action"] = value
                elif key == "date":
                    out["date_string"] = value
                    out["date"] = value
                elif key == "date_string":
                    out["date_string"] = value
                elif key == "timezone" or key == "tz":
                    out["timezone"] = value
                elif key == "format":
                    out["format"] = value
                elif key == "days":
                    try:
                        out["days"] = int(float(value))
                    except (ValueError, TypeError):
                        out["days"] = value
                elif key == "expression":
                    out["expression"] = value
                elif key == "steps" and value.startswith("[") and value.endswith("]"):
                    try:
                        steps_json = value.replace("'", '"')
                        out["steps"] = json.loads(steps_json)
                    except json.JSONDecodeError:
                        pass
        return out if out else None

    def _normalize_operation_and_params(self, args: Dict[str, Any]) -> str:
        """Normalize operation (single_step -> action, current_* -> current_datetime) and params (value -> days/format). Returns operation."""
        operation = (args.get("operation") or "").strip().lower()
        if operation == "single_step":
            action = (args.get("action") or "").strip().lower()
            if action:
                args["operation"] = action
                operation = action
            else:
                # Planner often uses single_step to mean "one calendar/date step" -> default current_datetime
                args["operation"] = "current_datetime"
                operation = "current_datetime"
        if operation in ("current_date", "current_time", "current_timezone", "current_time_zone"):
            args["operation"] = "current_datetime"
            operation = "current_datetime"
        # Map planner "value" to days or format for date_calculate (no hardcoded action list beyond what tool supports)
        if operation == "date_calculate":
            action = (args.get("action") or "").strip().lower()
            if "value" in args:
                v = args["value"]
                if action in self._REQUIRED_DATE_FIELDS and "days" in (self._REQUIRED_DATE_FIELDS.get(action) or []):
                    try:
                        args["days"] = int(float(v))
                    except (ValueError, TypeError):
                        args["days"] = v
                elif action == "format":
                    args["format"] = args.get("format") or str(v).strip()
            if "minutes" in args and action in self._REQUIRED_DATE_FIELDS and "minutes" in (self._REQUIRED_DATE_FIELDS.get(action) or []):
                try:
                    args["minutes"] = int(float(args["minutes"]))
                except (ValueError, TypeError):
                    pass
        return operation

    def _has_explicit_params(self, operation: str, args: Dict[str, Any]) -> bool:
        """Return True if args already contain enough to execute without LLM interpretation."""
        if not operation:
            return False
        if operation == "arithmetic":
            expr = args.get("expression")
            if expr is not None and str(expr).strip():
                return True
            return all(args.get(k) is not None for k in ("a", "b", "op"))
        if operation == "current_datetime":
            return True
        if operation == "date_calculate":
            action = (args.get("action") or args.get("date_action") or "").strip().lower()
            required = self._REQUIRED_DATE_FIELDS.get(action)
            if not required:
                return False
            return all(args.get(field) is not None for field in required)
        if operation == "parse_calendar_dates":
            return bool((args.get("start_string") or args.get("start")) and (args.get("end_string") or args.get("end")))
        if operation == "date_range":
            return bool(args.get("start") and args.get("end"))
        if operation == "multi_step":
            return isinstance(args.get("steps"), list)
        return False

    async def _interpret_with_llm(self, user_query: str, tool_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Use LLM to interpret natural language into structured operation + params.
        Returns a dict with operation and relevant keys; can return multi_step with a list of steps.
        """
        prompt = """You are a calculator and date/time interpreter. Given the user message, output a single JSON object. Output ONLY valid JSON, no markdown or explanation.

1) ARITHMETIC: Use when the user asks for a math calculation.
   Output: {"operation": "arithmetic", "expression": "<math expression>"}
   OR {"operation": "arithmetic", "a": <number>, "b": <number>, "op": "add"|"subtract"|"multiply"|"divide"|"modulo"|"power"|"integer_divide"}
   Allowed in expression: numbers, +, -, *, /, **, %, //, (), ., and functions: sqrt, log, log10, sin, cos, tan, floor, ceil, abs (e.g. sqrt(16), 200*0.15).
   Examples: "15% of 200" -> {"operation": "arithmetic", "expression": "200 * 0.15"}; "sqrt(100)" -> {"operation": "arithmetic", "expression": "sqrt(100)"}.

2) CURRENT DATE/TIME: Use when the user asks for current date, time, today, now, or "what time is it in <city/region>".
   Output: {"operation": "current_datetime"} or {"operation": "current_datetime", "timezone": "America/New_York"}.
   For "what time in Tokyo" use timezone "Asia/Tokyo"; London -> "Europe/London"; New York -> "America/New_York"; Sydney -> "Australia/Sydney"; Dubai -> "Asia/Dubai"; Karachi -> "Asia/Karachi".

3) DATE CALCULATION: Use when the user asks to add/subtract days or minutes, difference between dates, day of week, parse or format a date. For "business days" use action "add_business_days" or "subtract_business_days". For "in N minutes" or "N minutes from now" use action "add_minutes" with "date" (or omit and use multi_step with current_datetime first) and "minutes".
   Output: {"operation": "date_calculate", "action": "add_days"|"subtract_days"|"add_business_days"|"subtract_business_days"|"add_minutes"|"subtract_minutes"|"difference"|"parse"|"format"|"day_of_week", ...params}
   Params: add_days/subtract_days need "date" and "days". add_minutes/subtract_minutes need "date" and "minutes" (date can be filled from previous step). difference needs "date_a" and "date_b". For "next Monday" use "date_string": "next Monday".

4) MULTI-STEP: Use when the query needs several steps (e.g. "10 days after next Monday" or "add 5 days to today then tell me the day of week").
   Output: {"operation": "multi_step", "steps": [{"operation": "...", ...params}, ...]}
   Example: "day of week 10 days after next Monday" -> {"operation": "multi_step", "steps": [{"operation": "date_calculate", "action": "parse", "date_string": "next Monday"}, {"operation": "date_calculate", "action": "add_days", "days": 10}, {"operation": "date_calculate", "action": "day_of_week"}]}
   Use "date" or "date_string" in later steps; the system will fill "date" from the previous step result when missing.

5) PARSE CALENDAR DATES: Use when the user asks to parse start and end for a calendar event.
   Output: {"operation": "parse_calendar_dates", "start_string": "<start phrase>", "end_string": "<end phrase>"}

6) DATE RANGE: Use when the user asks to determine the start and end dates of a relative or named PERIOD for filtering/reporting purposes (e.g. "last month", "this week", "next quarter", "second last month", "previous year", "last 2 weeks") — NOT a single calendar event's start/end time.
   Output: {"operation": "date_range", "period_description": "<phrase describing the period, e.g. 'second last month'>"}

User message: """
        prompt += (user_query or "").strip()[:600]
        if tool_args:
            prompt += "\n\nAdditional context (merge if relevant): " + json.dumps({k: v for k, v in tool_args.items() if k not in ("step_action",)})[:300]
        _ctx = getattr(self, "_agent_context", "")
        if _ctx:
            prompt += f"\n\nAgent context (guidelines from the main agent — use for disambiguation only):\n{_ctx[:400]}"
        prompt += "\n\nJSON:"
        out = await self._llm_generate(prompt, max_tokens=800)
        if not out:
            return None
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix):].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            logger.debug("Calculator/Datetime LLM returned invalid JSON: %s", out[:200])
        return None

    async def _suggest_failure_recovery(self, operation: str, user_query: str, context: str) -> Optional[str]:
        """Ask LLM for a brief suggestion when a calculation or parse failed. Returns one short sentence or None."""
        if not user_query or not (self.llm_provider and hasattr(self.llm_provider, "generate")):
            return None
        prompt = f"""The user asked: "{user_query[:200]}"
Operation: {operation}. What happened: {context[:150]}
Reply with exactly one short sentence suggesting how to fix or rephrase (e.g. "Try using a date in YYYY-MM-DD format" or "Use only numbers and + - * / in the expression"). No preamble."""
        out = await self._llm_generate(prompt, max_tokens=120)
        return (out.strip()[:300] if out else None) or None

    async def _parse_date_with_llm_fallback(self, s: str) -> Optional[datetime]:
        """
        When deterministic parsing fails, ask LLM to interpret the phrase and return an ISO date/datetime.
        Returns None if LLM is unavailable or returns invalid format.
        """
        if not s or not isinstance(s, str) or not s.strip():
            return None
        s = s.strip()[:400]
        ref_date = date.today().isoformat()
        prompt = f"""You are a date/time interpreter. Given the phrase below, output exactly one ISO 8601 date or datetime string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Use the current date as reference when needed: {ref_date}. Output ONLY the ISO string, no explanation or quotes.

Phrase: {s}

ISO string:"""
        out = await self._llm_generate(prompt, max_tokens=80)
        if not out:
            return None
        out = out.strip().strip('"\'')
        if not _ISO_STRICT_PATTERN.match(out):
            return None
        try:
            return datetime.fromisoformat(out.replace("Z", "+00:00"))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(out.strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    async def _resolve_date_range_with_llm(self, phrase: str) -> Optional[Dict[str, str]]:
        """
        Resolve a relative/named period phrase (e.g. 'second last month', 'this week',
        'last quarter') into start/end calendar-boundary dates via LLM. Generic — no
        hardcoded phrase-to-date mapping.
        """
        if not phrase or not isinstance(phrase, str) or not phrase.strip():
            return None
        phrase = phrase.strip()[:200]
        ref_date = date.today().isoformat()
        prompt = f"""You are a date range interpreter. Given the phrase below, compute the start and end dates of that PERIOD as full calendar boundaries. For example: "last month" = first day to last day of the previous calendar month; "second last month" = first day to last day of the month two months before the current month; "this week" = Monday to Sunday of the current week; "last quarter" = first to last day of the previous calendar quarter; "next year" = January 1 to December 31 of next year. Use the current date as reference: {ref_date}. Output ONLY a JSON object: {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}. No explanation or markdown.

Phrase: {phrase}

JSON:"""
        out = await self._llm_generate(prompt, max_tokens=100)
        if not out:
            return None
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix):].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        start = parsed.get("start")
        end = parsed.get("end")
        if not (isinstance(start, str) and isinstance(end, str)):
            return None
        start = start.strip()
        end = end.strip()
        if not (_ISO_STRICT_PATTERN.match(start) and _ISO_STRICT_PATTERN.match(end)):
            return None
        return {"start": start, "end": end}

    def _parse_natural_datetime(self, s: str) -> Optional[datetime]:
        """Parse natural language date/time (e.g. 'tomorrow at 10 PM') without relying on LLM. Uses dateutil if available, else deterministic rules."""
        if not s or not isinstance(s, str):
            return None
        s = s.strip()
        if not s or "None" in s or s.startswith("{"):
            return None
        # Try ISO / fixed formats first (existing logic)
        out = self._parse_date_input(s)
        if out is not None:
            return out
        # Try dateutil for natural language
        if _HAS_DATEUTIL and dateutil_parser:
            try:
                dt = dateutil_parser.parse(s, fuzzy=True)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                pass
        # Deterministic fallback: "tomorrow", "today", "at 10 PM", "tomorrow at 10 PM"
        today = date.today()
        base_date = today
        time_hour, time_min = 0, 0
        q = s.lower()
        # Relative day
        if "tomorrow" in q:
            base_date = today + timedelta(days=1)
        elif "yesterday" in q:
            base_date = today - timedelta(days=1)
        # Time: "at 10 pm", "10:30 am" (12-hour) or "22:00" (24-hour) — separate patterns to avoid ambiguous groups
        match_12 = re.search(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", q, re.I)
        match_24 = re.search(r"\b(\d{1,2}):(\d{2})\b", q)
        if match_12:
            h = int(match_12.group(1))
            m = int(match_12.group(2) or 0)
            ampm = (match_12.group(3) or "").lower()
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
            time_hour, time_min = h, m
        elif match_24:
            time_hour = int(match_24.group(1))
            time_min = int(match_24.group(2))
        try:
            dt = datetime(base_date.year, base_date.month, base_date.day, time_hour, time_min, 0, tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    def _extract_calendar_dates_from_query(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """Extract start and end date/time phrases from Calendar context-retriever query. Uses logic only (no LLM)."""
        if not query or "User request:" not in query:
            return None, None
        # Extract "User request: ..." (up to "Start from request" or "Parse start" or end)
        user_match = re.search(r"User request:\s*(.+?)(?=\s*Start from request:|\s*End from request:|\s*Parse start|$)", query, re.S | re.I)
        user_text = (user_match.group(1).strip() if user_match else query)[:500]
        if not user_text:
            return None, None
        # Check if "Start from request:" / "End from request:" have real values (not empty dict/empty string)
        start_from = re.search(r"Start from request:\s*(.+?)(?=\s*End from request:|\s*Parse start|$)", query, re.S | re.I)
        end_from = re.search(r"End from request:\s*(.+?)(?=\s*Parse start|$)", query, re.S | re.I)
        start_val = (start_from.group(1).strip() if start_from else "").strip()
        end_val = (end_from.group(1).strip() if end_from else "").strip()
        # If they contain actual date strings (not empty or {'dateTime': '', ...}), use them
        if start_val and end_val and "dateTime': ''" not in start_val and "dateTime': ''" not in end_val and "{}" not in start_val:
            # Still could be dict string; if it looks like a single time/date phrase, use it
            if len(start_val) < 80 and len(end_val) < 80 and not start_val.startswith("{"):
                return start_val, end_val
        # Extract from user_text: "starting X and ending Y" or "from X to Y"
        start_phrase = None
        end_phrase = None
        # Pattern: "starting ... and ending ..." or "start ... end ..."
        start_end = re.search(
            r"(?:starting|start|from)\s+(.+?)\s+and\s+(?:ending|end|to)\s+(.+?)(?:\s+with|\s+with attendee|,|$)",
            user_text,
            re.I,
        )
        if start_end:
            start_phrase = start_end.group(1).strip()
            end_phrase = start_end.group(2).strip()
        if not start_phrase or not end_phrase:
            # Fallback: "tomorrow at 10 PM" and "11 PM" (end might be just time)
            start_m = re.search(r"(?:starting|start|from)\s+(.+?)(?:\s+and\s+ending|\s+and\s+end|,|$)", user_text, re.I)
            if start_m:
                start_phrase = start_m.group(1).strip()
            end_m = re.search(r"(?:ending|end|to)\s+(.+?)(?=\s+with\s+attendee|\s+with\s|,|$)", user_text, re.S | re.I)
            if end_m:
                end_phrase = end_m.group(1).strip()
        if not start_phrase:
            # Try: "start datetime set to X, end datetime Y"
            start_m2 = re.search(r"start\s+datetime\s+(?:set\s+to\s+)?(.+?)(?:\s*,\s*end\s+datetime|\s+and\s+end|\s*,\s*$)", user_text, re.I)
            if start_m2:
                start_phrase = start_m2.group(1).strip()
            end_m2 = re.search(r"end\s+datetime\s+(.+?)(?:\s+and\s+attendee|\s*,|\s+with\s+attendee|$)", user_text, re.I)
            if end_m2:
                end_phrase = end_m2.group(1).strip()
        if start_phrase and end_phrase:
            # If end_phrase is "one hour later", "1 hour later", "X hours later" — keep as-is; we'll add hours in parse
            end_lower = end_phrase.lower()
            if "hour" in end_lower and "later" in end_lower:
                return start_phrase, end_phrase
            if "minute" in end_lower and "later" in end_lower:
                return start_phrase, end_phrase
            # If end_phrase is only a time ("11 PM"), use same date as start for end
            if re.match(r"^\d{1,2}(?::\d{2})?\s*(?:am|pm)?$", end_phrase.strip()) or ("at " in end_lower and "tomorrow" not in end_lower and "today" not in end_lower and "later" not in end_lower):
                if "tomorrow" in (start_phrase or "").lower():
                    end_phrase = "tomorrow at " + end_phrase if "at " not in end_phrase else end_phrase
                elif "today" in (start_phrase or "").lower():
                    end_phrase = "today at " + end_phrase if "at " not in end_phrase else end_phrase
                else:
                    end_phrase = start_phrase.split(" at ")[0] + " at " + end_phrase if " at " in start_phrase else (start_phrase + " " + end_phrase)
            return start_phrase, end_phrase
        return None, None

    def _parse_relative_end(self, end_phrase: str, start_dt: datetime) -> Optional[datetime]:
        """Parse relative end phrases like 'one hour later', '2 hours later', '30 minutes later'. Returns start_dt + offset. No static defaults."""
        if not end_phrase or not start_dt:
            return None
        q = end_phrase.lower().strip()
        # "one hour later" / "an hour later" / "a hour later"
        if re.search(r"(?:one|an?)\s+hour\s+later", q):
            return start_dt + timedelta(hours=1)
        # X hour(s) later (digit)
        hour_match = re.search(r"(\d+)\s*hours?\s+later", q)
        if hour_match:
            return start_dt + timedelta(hours=int(hour_match.group(1)))
        if "hour" in q and "later" in q:
            return start_dt + timedelta(hours=1)
        # X minute(s) later
        min_match = re.search(r"(\d+)\s*minutes?\s+later", q)
        if min_match:
            return start_dt + timedelta(minutes=int(min_match.group(1)))
        if "half hour" in q and "later" in q:
            return start_dt + timedelta(minutes=30)
        return None

    # ---------- Math arithmetic (safe AST-based evaluation) ----------
    def _safe_eval_expression(self, expression: str) -> Optional[float]:
        """
        Evaluate a math expression safely using AST. Allows numbers, + - * / // % ** ( ),
        and whitelisted math functions: sqrt, log, log10, log2, sin, cos, tan, factorial,
        floor, ceil, abs, round, min, max, pow. No eval() of arbitrary strings.
        """
        if not expression or not isinstance(expression, str):
            return None
        s = expression.strip()
        if not s:
            return None
        try:
            tree = ast.parse(s, mode="eval")
            result = self._eval_ast_safe(tree.body)
            if result is None:
                return None
            return float(result)
        except (SyntaxError, ValueError, TypeError, ZeroDivisionError) as e:
            logger.debug("Safe eval failed for %r: %s", s[:80], e)
            return None

    def _eval_ast_safe(self, node: ast.AST) -> Optional[float]:
        """Recursively evaluate AST node; only allow literals, allowed BinOp/UnaryOp, and whitelisted Calls."""
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, (int, float)):
                return float(v)
            return None
        _ast_num = getattr(ast, "Num", None)
        if _ast_num is not None and isinstance(node, _ast_num):  # Python < 3.8 compat
            return float(node.n)
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_BINOP:
                return None
            left = self._eval_ast_safe(node.left)
            right = self._eval_ast_safe(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    return None
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                if right == 0:
                    return None
                return float(int(left) // int(right))
            if isinstance(node.op, ast.Mod):
                if right == 0:
                    return None
                return left % right
            if isinstance(node.op, ast.Pow):
                try:
                    return left ** right
                except (ValueError, OverflowError):
                    return None
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in _ALLOWED_UNARYOP:
                return None
            operand = self._eval_ast_safe(node.operand)
            if operand is None:
                return None
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.USub):
                return -operand
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _SAFE_MATH:
                fn = _SAFE_MATH[func.id]
                args = []
                for arg in node.args:
                    v = self._eval_ast_safe(arg)
                    if v is None:
                        return None
                    args.append(v)
                try:
                    return float(fn(*args))
                except (ValueError, TypeError, ZeroDivisionError):
                    return None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "math" and func.attr in _SAFE_MATH:
                fn = _SAFE_MATH[func.attr]
                args = []
                for arg in node.args:
                    v = self._eval_ast_safe(arg)
                    if v is None:
                        return None
                    args.append(v)
                try:
                    return float(fn(*args))
                except (ValueError, TypeError, ZeroDivisionError):
                    return None
        return None

    def _format_number(self, value: float) -> str:
        """Format numeric result without floating-point artifacts (e.g. 0.30000000000000004)."""
        if value != value:  # NaN
            return str(value)
        if value == float("inf") or value == float("-inf"):
            return str(value)
        if value == int(value):
            return str(int(value))
        return f"{value:.12g}"

    def _do_arithmetic(
        self, expression: Optional[str] = None, a: Optional[float] = None, b: Optional[float] = None, op: Optional[str] = None
    ) -> Optional[float]:
        if expression is not None and expression != "":
            return self._safe_eval_expression(str(expression).strip())
        if a is not None and b is not None and op:
            op = str(op).strip().lower()
            try:
                x, y = float(a), float(b)
                if op in ("add", "+"):
                    return x + y
                if op in ("subtract", "-"):
                    return x - y
                if op in ("multiply", "*"):
                    return x * y
                if op in ("divide", "/"):
                    if y == 0:
                        return None
                    return x / y
                if op in ("modulo", "%", "remainder"):
                    if y == 0:
                        return None
                    return x % y
                if op in ("power", "**", "pow"):
                    return x ** y
                if op in ("integer_divide", "//", "floor_divide"):
                    if y == 0:
                        return None
                    return float(int(x) // int(y))
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        return None

    # ---------- Current date/time ----------
    def _get_current_datetime(self, timezone_name: Optional[str] = None) -> Dict[str, Any]:
        if timezone_name and str(timezone_name).strip() and ZoneInfo:
            try:
                tz = ZoneInfo(str(timezone_name).strip())
                now = datetime.now(tz)
            except Exception:
                now = datetime.now(timezone.utc)
                timezone_name = "UTC (invalid zone requested)"
        else:
            now = datetime.now(timezone.utc)
            timezone_name = timezone_name or "UTC"
        return {
            "iso": now.isoformat(),
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M:%S"),
            "timezone": str(timezone_name),
            "weekday": now.strftime("%A"),
        }

    # ---------- Date/time calculations ----------

    def _merge_date_from_provided_data(self, args: Dict[str, Any], provided_data: Any) -> None:
        """Fill args['date'] from previous step when missing or when date is not a parseable value (placeholder dict or non-parseable string). No hardcoded keys."""
        if not provided_data:
            return
        items = provided_data if isinstance(provided_data, list) else [provided_data]
        date_val = args.get("date") or args.get("date_string")
        use_from_context = not date_val
        if date_val and isinstance(date_val, str):
            # If the string does not parse as a date/datetime, resolve from context
            if self._parse_date_input(date_val) is None:
                use_from_context = True
                args.pop("date", None)
                args.pop("date_string", None)
        if date_val and isinstance(date_val, dict):
            # If dict has a parseable date field (iso, datetime_iso, date), use it
            ref = date_val.get("iso") or date_val.get("datetime_iso") or date_val.get("date")
            if ref is not None and str(ref).strip() and len(str(ref)) >= 10:
                args["date"] = ref
                args["date_string"] = ref
                return
            # Otherwise treat as placeholder: resolve from context
            use_from_context = True
            args.pop("date", None)
            args.pop("date_string", None)
        if not use_from_context:
            return
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            res = item.get("result") or item
            if not isinstance(res, dict):
                if res is not None and isinstance(res, str) and len(res) >= 10 and res.count("-") >= 2:
                    args["date"] = res
                    args["date_string"] = res
                    return
                continue
            ref = res.get("iso") or res.get("datetime_iso") or res.get("date")
            if ref is not None and str(ref).strip():
                args["date"] = ref
                args["date_string"] = ref
                return

    async def _extract_date_from_context_llm(
        self, provided_data: Any, tool_args: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Use LLM to extract a single ISO datetime from previous-step context (e.g. Scheduler
        api_response, execution.nextRunAt, or any structure). No hardcoded keys; LLM decides.
        Returns the ISO string or None.
        """
        if not provided_data or not (self.llm_provider and hasattr(self.llm_provider, "generate")):
            return None
        items = provided_data if isinstance(provided_data, list) else [provided_data]
        # Build a JSON-serializable snapshot (cap size to avoid huge prompts)
        def _snapshot(obj: Any, depth: int = 0, max_depth: int = 4) -> Any:
            if depth >= max_depth:
                return "<nested>"
            if isinstance(obj, dict):
                return {k: _snapshot(v, depth + 1, max_depth) for k, v in list(obj.items())[:30]}
            if isinstance(obj, list):
                return [_snapshot(x, depth + 1, max_depth) for x in obj[:20]]
            if isinstance(obj, (str, int, float, bool)) or obj is None:
                return obj
            return str(type(obj).__name__)

        try:
            context_str = json.dumps([_snapshot(i) for i in items if isinstance(i, dict)], default=str)
        except (TypeError, ValueError):
            context_str = str(items)[:6000]
        if len(context_str) > 6000:
            context_str = context_str[:6000] + "..."
        prompt = f"""From the following JSON context (outputs from previous steps, e.g. schedules, API responses), extract exactly one ISO 8601 datetime string (e.g. YYYY-MM-DDTHH:MM:SS or with Z) that should be used as the base date for a date calculation (e.g. add_minutes). The datetime might appear under any key (e.g. nextRunAt, startDate, execution time, iso, datetime_iso, date, etc.). Reply with ONLY a JSON object: {{"date": "<iso string>"}} or {{"date": null}} if no datetime is found. No other text.

Context:
{context_str}

JSON:"""
        out = await self._llm_generate(prompt, max_tokens=120)
        if not out or not isinstance(out, str):
            return None
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix) :].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            parsed = json.loads(out)
            date_val = parsed.get("date") if isinstance(parsed, dict) else None
            if not date_val or not isinstance(date_val, str) or not date_val.strip():
                return None
            date_val = date_val.strip().strip('"\'')
            if self._parse_date_input(date_val) is not None:
                return date_val
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _parse_date_input(self, value: Any) -> Optional[datetime]:
        """Parse date from string or ISO date/datetime. Accepts dict with iso/datetime_iso/date from step results."""
        if value is None:
            return None
        if isinstance(value, (datetime, date)):
            if isinstance(value, date) and not isinstance(value, datetime):
                return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
            return value
        if isinstance(value, dict):
            s = value.get("iso") or value.get("datetime_iso") or value.get("date")
            if s is not None:
                value = s
            else:
                value = None
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        # Strip trailing " (Weekday)" from step 1 current_datetime response
        s = re.sub(r"\s*\(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday\)\s*$", "", s, flags=re.IGNORECASE).strip()
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%B %d, %Y",
            "%d %B %Y",
            "%b %d, %Y",
        ):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        return None

    def _validate_date_action(self, action: str, params: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate that required parameters for the date action are present. Returns (ok, error_message)."""
        required = self._REQUIRED_DATE_FIELDS.get(action)
        if not required:
            return False, "Unsupported date action"
        missing = [r for r in required if params.get(r) is None or (isinstance(params.get(r), str) and not params.get(r).strip())]
        if missing:
            return False, f"Missing required parameters: {', '.join(missing)}"
        return True, None

    def _do_date_calculate(self, action: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action = (action or "").strip().lower()

        def _add_business_days(start: datetime, n: int) -> datetime:
            """Add n business days (skip weekends). Preserves timezone and time component. No holiday calendar."""
            current = start
            step = 1 if n >= 0 else -1
            remaining = abs(n)
            while remaining > 0:
                current = current + timedelta(days=step)
                if current.weekday() < 5:  # Monday=0 .. Friday=4
                    remaining -= 1
            return current

        if action == "add_days":
            d = self._parse_date_input(params.get("date"))
            days = params.get("days")
            if d is not None and days is not None:
                try:
                    delta = timedelta(days=int(float(days)))
                    result = d + delta
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        elif action == "subtract_days":
            d = self._parse_date_input(params.get("date"))
            days = params.get("days")
            if d is not None and days is not None:
                try:
                    delta = timedelta(days=int(float(days)))
                    result = d - delta
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        elif action == "add_business_days":
            d = self._parse_date_input(params.get("date"))
            days = params.get("days")
            if d is not None and days is not None:
                try:
                    n = int(float(days))
                    result = _add_business_days(d, n)
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        elif action == "subtract_business_days":
            d = self._parse_date_input(params.get("date"))
            days = params.get("days")
            if d is not None and days is not None:
                try:
                    n = int(float(days))
                    result = _add_business_days(d, -n)
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        elif action == "difference":
            da = self._parse_date_input(params.get("date_a"))
            db = self._parse_date_input(params.get("date_b"))
            if da is not None and db is not None:
                delta = (da.replace(tzinfo=None) if da.tzinfo else da) - (
                    db.replace(tzinfo=None) if db.tzinfo else db
                )
                return {"days": delta.days, "result": delta.days}
        elif action == "parse":
            s = params.get("date_string") or params.get("date")
            if s is None:
                return None
            d = self._parse_date_input(s)
            if d is None and isinstance(s, str) and s.strip():
                d = self._parse_natural_datetime(s.strip())
            if d is not None:
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return {"result": d.date().isoformat(), "datetime_iso": d.isoformat()}
        elif action == "format":
            d = self._parse_date_input(params.get("date_string") or params.get("date"))
            fmt = params.get("format") or "%B %d, %Y"
            if d is not None:
                try:
                    return {"result": d.strftime(str(fmt))}
                except (ValueError, TypeError):
                    return {"result": d.date().isoformat()}
        elif action == "day_of_week":
            d = self._parse_date_input(params.get("date_string") or params.get("date"))
            if d is not None:
                return {"result": d.strftime("%A"), "weekday": d.strftime("%A")}
        elif action == "add_minutes":
            d = self._parse_date_input(params.get("date"))
            minutes = params.get("minutes")
            if d is not None and minutes is not None:
                try:
                    delta = timedelta(minutes=int(float(minutes)))
                    result = d + delta
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        elif action == "subtract_minutes":
            d = self._parse_date_input(params.get("date"))
            minutes = params.get("minutes")
            if d is not None and minutes is not None:
                try:
                    delta = timedelta(minutes=int(float(minutes)))
                    result = d - delta
                    return {"result": result.date().isoformat(), "datetime_iso": result.isoformat()}
                except (TypeError, ValueError):
                    pass
        return None

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        agent_context: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Run calculator or date/time operation. When params are not fully specified,
        uses LLM to interpret natural language (user_query) into structured operation and params.
        agent_context: identity/guidelines from the main agent, injected by the runner.
        """
        self._agent_context = (agent_context or "").strip()[:800]
        try:
            args = dict(tool_args or {})
            operation = (args.get("operation") or "").strip().lower()

            # Parse JSON query from planner (e.g. {"operation":"current_datetime","format":"iso8601"})
            if user_query and user_query.strip().startswith("{"):
                parsed = self._parse_json_query(user_query)
                if parsed:
                    args.update(parsed)
                    operation = (args.get("operation") or operation).strip().lower()
            # Parse query-string style (e.g. operation=date_parse&action=parse&date=tomorrow)
            # Skip when tool_args already supplies a valid operation — natural-language queries often
            # contain "operation='x'" as prose, which the querystring parser mis-extracts (trailing
            # punctuation / quotes) and corrupts the already-correct operation from tool_args.
            elif user_query and ("operation=" in user_query or "action=" in user_query) and not operation:
                parsed = self._parse_querystring_params(user_query)
                if parsed:
                    args.update(parsed)
                    operation = (args.get("operation") or operation).strip().lower()

            operation = self._normalize_operation_and_params(args)

            # Merge date from previous step results when doing date_calculate (e.g. "based on current date")
            if operation == "date_calculate" and provided_data:
                self._merge_date_from_provided_data(args, provided_data)
                # If date still missing or unparseable (e.g. placeholder "{result_from_step_1}"), extract from context via LLM
                action = (args.get("action") or "").strip().lower()
                if action in self._REQUIRED_DATE_FIELDS and "date" in (self._REQUIRED_DATE_FIELDS.get(action) or []):
                    if self._parse_date_input(args.get("date")) is None:
                        extracted = await self._extract_date_from_context_llm(provided_data, tool_args)
                        if extracted:
                            args["date"] = extracted
                            args["date_string"] = extracted

            # Use LLM only for decision making / output structuring when params are not explicit
            # For parse_calendar_dates with Calendar context query, extract and parse logically (no LLM)
            if operation == "parse_calendar_dates" and user_query and "User request:" in user_query and ("Parse start and end" in user_query or "Start from request:" in user_query):
                # Extract start/end from query in parse_calendar_dates branch below; skip LLM
                pass
            elif not self._has_explicit_params(operation, args) and (user_query or args):
                yield event(AgentEvent.THOUGHT, "Interpreting your request...")
                interpreted = await self._interpret_with_llm(user_query or "", args)
                if interpreted and isinstance(interpreted, dict):
                    args.update({k: v for k, v in interpreted.items() if v is not None})
                    operation = (args.get("operation") or operation).strip().lower()

            # Multi-step: run each step in sequence, passing previous result as context for the next
            if operation == "multi_step":
                steps = args.get("steps") or []
                if not isinstance(steps, list):
                    steps = []
                last_result = provided_data
                for idx, step in enumerate(steps):
                    if not isinstance(step, dict):
                        continue
                    step_op = (step.get("operation") or "").strip().lower()
                    if not step_op:
                        continue
                    step_args = dict(step)
                    yield event(AgentEvent.THOUGHT, f"Step {idx + 1}/{len(steps)}: {step_op}...")
                    # Pass empty query so child run() uses only step_args; parent's user_query may be full multi_step JSON and would overwrite args and recurse
                    async for ev in self.run(
                        user_query="",
                        provided_data=last_result,
                        tool_args=step_args,
                        session_id=session_id,
                    ):
                        yield ev
                        if ev.get("type") == "result":
                            c = ev.get("content") or {}
                            if c.get("success") and "result" in c:
                                last_result = c.get("result")
                out = {"success": True, "result": last_result, "operation": "multi_step"}
                yield event(AgentEvent.RESULT, out)
                raw_response = json.dumps(out, default=str)
                yield event(AgentEvent.FINAL, {"success": True, "response": raw_response, "result": out})
                return

            if operation == "arithmetic":
                yield event(AgentEvent.THOUGHT, "Computing arithmetic result...")
                result = self._do_arithmetic(
                    expression=args.get("expression"),
                    a=args.get("a"),
                    b=args.get("b"),
                    op=args.get("op"),
                )
                if result is not None:
                    out = {"success": True, "result": result, "operation": "arithmetic"}
                else:
                    suggestion = await self._suggest_failure_recovery(
                        "arithmetic", user_query or "", "Expression or operands invalid or unsupported."
                    )
                    out = {"success": False, "operation": "arithmetic", "error": "Could not evaluate expression or operands." + (" " + suggestion if suggestion else "")}

            elif operation == "current_datetime":
                yield event(AgentEvent.THOUGHT, "Getting current date and time...")
                tz = args.get("timezone") or args.get("tz") or (self.app_config.get("user_timezone") or "UTC").strip() or "UTC"
                data = self._get_current_datetime(tz)
                # Always include date, time, timezone, iso, weekday for downstream (calendar, etc.)
                fmt = (args.get("format") or "").strip().lower().replace("-", "_")
                out = {"success": True, "result": data, "operation": "current_datetime"}

            elif operation == "date_calculate":
                yield event(AgentEvent.THOUGHT, "Performing date/time calculation...")
                action = args.get("action") or args.get("date_action")
                params = {k: v for k, v in args.items() if k not in ("operation", "action", "date_action")}
                # If query asks for "tomorrow at X", use parse with natural date_string so we get the right time (e.g. 5 PM)
                if user_query and "tomorrow at" in user_query.lower():
                    match = re.search(r"tomorrow\s+at\s+\d{1,2}\s*(?:am|pm)?", user_query, re.IGNORECASE)
                    if match:
                        params["date_string"] = match.group(0).strip()
                        action = "parse"
                # LLM fallback for parse: when deterministic parsing fails, try LLM to interpret natural language date
                if (action or "parse").strip().lower() == "parse":
                    s = params.get("date_string") or params.get("date")
                    if isinstance(s, str) and s.strip():
                        d = self._parse_date_input(s) or self._parse_natural_datetime(s.strip())
                        if d is None:
                            yield event(AgentEvent.THOUGHT, "Interpreting date phrase...")
                            d = await self._parse_date_with_llm_fallback(s.strip())
                            if d is not None:
                                params["date_string"] = d.isoformat()
                data = self._do_date_calculate(action or "parse", params)
                if data is not None:
                    out = {"success": True, "result": data, "operation": "date_calculate"}
                else:
                    suggestion = await self._suggest_failure_recovery(
                        "date_calculate", user_query or "", "Parse or calculation failed for the given date/params."
                    )
                    out = {"success": False, "operation": "date_calculate", "error": "Date calculation failed. Check date format and parameters." + (" " + suggestion if suggestion else "")}

            elif operation == "date_range":
                yield event(AgentEvent.THOUGHT, "Resolving date range for the requested period...")
                phrase = (args.get("period_description") or args.get("period") or user_query or "").strip()
                resolved = await self._resolve_date_range_with_llm(phrase)
                if resolved:
                    out = {
                        "success": True,
                        "result": resolved,
                        "start": resolved.get("start"),
                        "end": resolved.get("end"),
                        "operation": "date_range",
                    }
                else:
                    suggestion = await self._suggest_failure_recovery(
                        "date_range", user_query or "", "Could not resolve start/end dates for the requested period."
                    )
                    out = {"success": False, "operation": "date_range", "error": "Could not determine the date range for the requested period." + (" " + suggestion if suggestion else "")}

            elif operation == "parse_calendar_dates":
                yield event(AgentEvent.THOUGHT, "Parsing start and end dates for calendar event...")
                start_s = (args.get("start_string") or args.get("start")) if args.get("start_string") or args.get("start") else ""
                end_s = (args.get("end_string") or args.get("end")) if args.get("end_string") or args.get("end") else ""
                start_s = str(start_s).strip() if start_s else ""
                end_s = str(end_s).strip() if end_s else ""
                # When Calendar sends empty Start/End from request, extract from "User request: ..." and parse logically (no LLM)
                if not start_s or not end_s or "dateTime': ''" in start_s or "dateTime': ''" in end_s or "dateTime': None" in start_s or "dateTime': None" in end_s or start_s.startswith("{"):
                    start_phrase, end_phrase = self._extract_calendar_dates_from_query(user_query or "")
                    if start_phrase:
                        start_s = start_phrase
                    if end_phrase:
                        end_s = end_phrase
                start_dt = self._parse_date_input(start_s) if start_s else None
                if start_dt is None and start_s:
                    start_dt = self._parse_natural_datetime(start_s)
                if start_dt is None and start_s:
                    yield event(AgentEvent.THOUGHT, "Interpreting start date/time...")
                    start_dt = await self._parse_date_with_llm_fallback(start_s)
                end_dt = None
                if end_s and start_dt:
                    end_dt = self._parse_relative_end(end_s, start_dt)
                if end_dt is None and end_s:
                    end_dt = self._parse_date_input(end_s)
                if end_dt is None and end_s:
                    end_dt = self._parse_natural_datetime(end_s)
                if end_dt is None and end_s:
                    yield event(AgentEvent.THOUGHT, "Interpreting end date/time...")
                    end_dt = await self._parse_date_with_llm_fallback(end_s)
                if start_dt and end_dt:
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    start_iso = start_dt.isoformat().replace("+00:00", "Z")
                    end_iso = end_dt.isoformat().replace("+00:00", "Z")
                    out = {
                        "success": True,
                        "result": {"start": start_iso, "end": end_iso},
                        "start": start_iso,
                        "end": end_iso,
                        "operation": "parse_calendar_dates",
                    }
                else:
                    out = {"success": False, "operation": "parse_calendar_dates", "error": "Could not parse start and/or end date."}

            else:
                out = {
                    "success": False,
                    "operation": None,
                    "result": None,
                    "error": "Unknown operation. Use operation: arithmetic, current_datetime, or date_calculate with appropriate params.",
                }

            yield event(AgentEvent.RESULT, out)
            # Return raw result for main LLM to convert to natural language (no tool-side NL)
            raw_response = json.dumps(out, default=str)
            yield event(AgentEvent.FINAL, {"success": out.get("success", False), "response": raw_response, "result": out})

        except RecursionError as e:
            # Do not use logger.exception() — traceback formatting can trigger another RecursionError
            logger.error("Calculator/Datetime agent failed (recursion limit): %s", e)
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "response": None})
        except Exception as e:
            logger.exception("Calculator/Datetime agent failed")
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("Calculator & DateTime agent initialized")

    async def cleanup(self) -> None:
        logger.debug("Calculator & DateTime agent cleaned up")


__all__ = ["CalculatorDatetimeAgentTool"]

