"""
Gmail functions class for functions_wrapper plugin.

Each public @tool method is one Gmail action exposed to the LLM (mirrors the
structure of google_calendar_functions.py). Private helpers handle HTTP, token
refresh, MIME building, activity logging, and response formatting.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider —
all natural-language-to-tool-call reasoning (which action to call, how to fill
its parameters, e.g. building a Gmail search query or composing an email body)
is done by the outer ReAct brain (MCPAgentTool), the same way it is done for
every other functions_wrapper tool such as GoogleCalendarFunctions. Tool
docstrings below are written to make that translation reliable (e.g. the full
Gmail search operator reference is inlined into gmail_search_query's
description). `action_by_query` is a heuristic (non-LLM) catch-all for callers
that hand off a single free-text instruction instead of picking a specific tool.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import date, datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

# Base for Gmail REST API v1 (https://developers.google.com/workspace/gmail/api/reference/rest).
# All paths below are relative to this base (userId "me" = authenticated user).
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_BATCH_URL = "https://gmail.googleapis.com/batch/gmail/v1"
GMAIL_BATCH_CHUNK_SIZE = 50
DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_CAP = 100

# analyze_emails scans up to ANALYZE_MAX_MESSAGES messages and extracts compact
# structured data per email via regex pattern matching (no in-process LLM is
# available to this subprocess), then deterministically aggregates in Python.
ANALYZE_MAX_MESSAGES = 1000
ANALYZE_DEFAULT_MESSAGES = 200

_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
_COMPANY_URL = (os.getenv("COMPANY_URL") or _AUTH_URL).rstrip("/")

GMAIL_SEARCH_OPERATORS = (
    "in:", "to:", "from:", "subject:", "list:", "is:", "has:",
    "newer_than:", "older_than:", "after:", "before:", "around:",
    "deliveredto:", "rfc822msgid:", "cc:", "bcc:", "category:",
    "label:", "filename:", "larger:", "smaller:", "size:",
)

# Matches any recognized Gmail search operator immediately followed by its
# value, e.g. "from:boss", "-is:read", "label:work". Allows an optional
# leading "-" (negation) since negation prefixes an operator, not a word.
_GMAIL_OPERATOR_RE = re.compile(
    r"(?:^|[\s(])-?(?:" + "|".join(re.escape(op) for op in GMAIL_SEARCH_OPERATORS) + r")\S"
)

GMAIL_SEARCH_SYNTAX_REFERENCE = """Gmail search operator string (NOT plain English). Build using:
- in:inbox | in:sent | in:drafts | in:trash | in:spam | in:anywhere
- to:, from:, cc:, bcc:, deliveredto: — match an address or name
- subject: — match words in the subject line
- label: — match a Gmail label, e.g. label:work
- category: — promotions, social, updates, forums, primary
- has:attachment, has:userlabels, has:drive, has:document, has:spreadsheet, has:presentation, has:youtube
- is:read, is:unread, is:starred, is:important, is:muted, is:snoozed
- filename: — attachment filename or type, e.g. filename:pdf
- larger:, smaller:, size: — message size, e.g. larger:10M
- newer_than:Xd / Xm / Xy and older_than:Xd / Xm / Xy — relative time (d=days, m=months, y=years)
- after:YYYY/MM/DD and before:YYYY/MM/DD — absolute date range (before: is exclusive)
- around:YYYY/MM/DD — messages near a specific date
- "exact phrase" — quote multi-word phrases to match exactly
- -term or -operator:value — exclude a term or operator match
- {term1 term2} or term1 OR term2 — match any of several terms
- (...) — parentheses group an OR/boolean expression, e.g. (from:alice OR from:bob) -subject:spam
- rfc822msgid: — match a specific message by its RFC822 Message-ID header
Combine multiple operators with spaces (AND), e.g. 'in:inbox from:boss after:2026/06/01'.
Resolve relative dates ("last week" -> newer_than:7d) and absolute dates yourself before calling.
Leave empty to use the most recent inbox messages."""


# ---------------------------------------------------------------------------
# Module-level pure helpers (no I/O, no LLM) — payload/header parsing, search
# query validation, batch response parsing, regex-based amount extraction.
# ---------------------------------------------------------------------------


def _decode_payload_part(part: Dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_snippet_or_body(msg: Dict) -> str:
    snippet = (msg.get("snippet") or "").strip()
    payload = msg.get("payload") or {}
    parts = payload.get("parts") or []
    if not parts and payload.get("body", {}).get("data"):
        return snippet or _decode_payload_part(payload)
    for p in parts:
        if (p.get("mimeType") or "").lower() == "text/plain":
            return _decode_payload_part(p) or snippet
    if parts:
        return _decode_payload_part(parts[0]) or snippet
    return snippet


def _parse_email_headers(msg: Dict) -> Dict[str, str]:
    out = {}
    for h in (msg.get("payload") or {}).get("headers") or []:
        name = (h.get("name") or "").lower()
        if name in ("from", "to", "subject", "date", "cc", "bcc"):
            out[name] = h.get("value") or ""
    return out


def _normalize_address_list(value: Any) -> str:
    """Normalize To/Cc/Bcc from an array or string into a comma-separated string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(x).strip() for x in value if x)
    return ""


def _extract_email_from_header(header_value: str) -> str:
    """Extract email address from 'Name <email@domain.com>' or return as-is if no angle brackets."""
    if not header_value or "<" not in header_value or ">" not in header_value:
        return (header_value or "").strip()
    match = re.search(r"<([^>]+)>", header_value)
    return (match.group(1).strip() if match else header_value.strip()) or header_value.strip()


def _is_placeholder_content(val: Optional[str]) -> bool:
    """True if val looks like a placeholder (e.g. '[Content from step 4]'), not real content."""
    if not val or not isinstance(val, str):
        return True
    v = val.strip().lower()
    if len(v) < 20 and any(x in v for x in ("[content", "from step", "step ", "placeholder", "see step")):
        return True
    if re.match(r"^\[.*step\s*\d+\s*\]$", v) or re.match(r"^\[content\s+from\s+step", v):
        return True
    return False


def _validate_gmail_search(q: Optional[str]) -> bool:
    """Return True only if q contains recognized Gmail search syntax.

    Checks are derived entirely from GMAIL_SEARCH_OPERATORS (operator
    detection via _GMAIL_OPERATOR_RE), plus Gmail's generic grouping/boolean
    syntaxes: exact-phrase quotes ("..."), OR-groups ({...} / OR), and
    parenthesized boolean groups (...). No fixed word-count or punctuation
    thresholds — any string built from Gmail's documented search syntax
    passes, anything else (plain English) does not.
    """
    if not q or not isinstance(q, str):
        return False
    q = q.strip()
    if not q or len(q) > 200:
        return False
    if _GMAIL_OPERATOR_RE.search(q):
        return True
    if '"' in q:
        return True
    if "{" in q and "}" in q:
        return True
    if "(" in q and ")" in q:
        return True
    return bool(re.search(r"\bOR\b", q))


def _sanitize_gmail_q(q: str) -> str:
    if not q or not isinstance(q, str):
        return ""
    return q.strip().replace("\n", " ").strip()[:200]


_CALCULATED_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _collect_date_strings(value: Any, _depth: int = 0) -> List[str]:
    """Recursively collect candidate date strings from nested dicts/lists/scalars."""
    if _depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: List[str] = []
        for v in value.values():
            out.extend(_collect_date_strings(v, _depth=_depth + 1))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_collect_date_strings(item, _depth=_depth + 1))
        return out
    return []


def _build_query_from_hints(hints: Optional[Dict[str, Any]]) -> str:
    """
    Best-effort fallback: scan a dict of caller-supplied hints (including
    nested dicts/lists) for any date/datetime-shaped values (YYYY-MM-DD...)
    and build a Gmail after:/before:/around: query from them. Used by
    action_by_query when the free-text query itself isn't valid Gmail syntax.
    """
    if not hints or not isinstance(hints, dict):
        return ""
    dates: List[date] = []
    for val in hints.values():
        for candidate in _collect_date_strings(val):
            m = _CALCULATED_DATE_RE.match(candidate.strip())
            if not m:
                continue
            try:
                dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                continue
    if not dates:
        return ""
    dates = sorted(set(dates))
    if len(dates) == 1:
        return f"around:{dates[0].strftime('%Y/%m/%d')}"
    start, end = dates[0], dates[-1]
    before = end + timedelta(days=1)
    return f"after:{start.strftime('%Y/%m/%d')} before:{before.strftime('%Y/%m/%d')}"


def _best_effort_query(explicit_q: Optional[str], free_text: str, hints: Optional[Dict[str, Any]] = None) -> str:
    """Prefer an explicit Gmail query; else use free_text if it already looks like valid Gmail
    syntax; else derive after:/before:/around: from any date-shaped hints; else empty (recent inbox)."""
    q = _sanitize_gmail_q(explicit_q or "")
    if q:
        return q
    if _validate_gmail_search(free_text or ""):
        return _sanitize_gmail_q(free_text)
    return _build_query_from_hints(hints)


def _parse_batch_response(content: bytes, content_type: str) -> List[Dict]:
    """
    Parse multipart/mixed batch response. Handles boundary with optional encoding.
    Returns list of successfully parsed JSON bodies (e.g. message objects).
    """
    results: List[Dict] = []
    ct_str = content_type if isinstance(content_type, str) else (content_type.decode("utf-8", errors="replace") if content_type else "")
    if not content or "boundary=" not in ct_str:
        return results
    ct = ct_str
    idx = ct.lower().find("boundary=")
    if idx == -1:
        return results
    rest = ct[idx + 9:].strip().strip('"').strip("'")
    boundary = rest.split(";")[0].split()[0].strip().strip('"').strip("'")
    if not boundary:
        return results
    sep = b"--" + boundary.encode("utf-8", errors="replace")
    raw = content
    start = raw.find(sep)
    if start == -1:
        return results
    current = raw[start + len(sep):].lstrip(b"\r\n")
    while current and not current.startswith(b"--"):
        head_end = current.find(b"\r\n\r\n")
        if head_end == -1:
            head_end = current.find(b"\n\n")
        if head_end == -1:
            break
        body_start = head_end + 4
        next_sep = current.find(sep, body_start)
        if next_sep == -1:
            part_body = current[body_start:].rstrip(b"\r\n")
            current = b""
        else:
            part_body = current[body_start:next_sep].rstrip(b"\r\n")
            current = current[next_sep + len(sep):].lstrip(b"\r\n")
        part_str = part_body.decode("utf-8", errors="replace")
        json_start = part_str.find("\r\n\r\n")
        if json_start == -1:
            json_start = part_str.find("\n\n")
        if json_start >= 0:
            json_str = part_str[json_start + 4:].lstrip("\r\n")
            try:
                obj = json.loads(json_str)
                if obj.get("id"):
                    results.append(obj)
            except json.JSONDecodeError:
                pass
    return results


# ----- analyze_emails: regex-based extraction + reduce (no in-process LLM available) -----
_AMOUNT_RE = re.compile(
    r"(?:(?P<sym>[$€£₹])\s?(?P<num1>[\d,]+(?:\.\d{1,2})?))"
    r"|(?:\b(?P<cur>USD|PKR|EUR|GBP|INR|AED|SAR|CAD|AUD)\s?(?P<num2>[\d,]+(?:\.\d{1,2})?))"
    r"|(?:\bRs\.?\s?(?P<num3>[\d,]+(?:\.\d{1,2})?))",
    re.IGNORECASE,
)
_SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def _regex_extract_record(msg: Dict, extract_focus: str = "") -> Dict[str, Any]:
    """Best-effort amount extraction via regex (the only extraction strategy available
    to this subprocess, since it has no in-process LLM to call)."""
    headers = _parse_email_headers(msg)
    text = _get_snippet_or_body(msg) or ""
    amount = None
    currency = None
    note = ""
    match = _AMOUNT_RE.search(text)
    if match:
        num = match.group("num1") or match.group("num2") or match.group("num3")
        try:
            amount = float(num.replace(",", ""))
        except (TypeError, ValueError):
            amount = None
        if amount is not None:
            if match.group("sym"):
                currency = _SYMBOL_CURRENCY.get(match.group("sym"))
            elif match.group("cur"):
                currency = match.group("cur").upper()
            elif match.group("num3") is not None:
                currency = "PKR"
            note = "amount detected via pattern match"
    return {
        "id": msg.get("id"),
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "amount": amount,
        "currency": currency,
        "note": note,
    }


def _aggregate_analysis_records(records: List[Dict]) -> Tuple[List[Dict], Dict[str, float]]:
    """Reduce step: keep records with a non-null amount and sum totals per currency."""
    matched: List[Dict] = []
    totals: Dict[str, float] = {}
    for r in records:
        amount = r.get("amount")
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        r = {**r, "amount": amount}
        currency = (r.get("currency") or "UNKNOWN").strip().upper() or "UNKNOWN"
        totals[currency] = totals.get(currency, 0.0) + amount
        matched.append(r)
    return matched, totals


def _parse_subject_body_text(text: str) -> Tuple[str, str]:
    """Parse 'Subject: X\\n\\nBody' or similar formats. Returns (subject, body)."""
    if not text or not isinstance(text, str):
        return ("", "")
    t = text.strip()
    m = re.match(r"^(?:subject\s*:\s*)(.+?)(?:\n{2,}|\n\n)(.+)$", t, re.DOTALL | re.IGNORECASE)
    if m:
        return ((m.group(1) or "").strip(), (m.group(2) or "").strip())
    lines = t.split("\n")
    if lines:
        first = lines[0].strip()
        if first.lower().startswith("subject"):
            subj = first.split(":", 1)[-1].strip() if ":" in first else first
            body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
            return (subj, body)
    return ("", t)


# ---------------------------------------------------------------------------
# GmailFunctions
# ---------------------------------------------------------------------------


class GmailFunctions(ConnectedServiceToolAgent):
    """
    Gmail tool functions. Each @tool method is a Gmail capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Gmail: list/search/read/summarize emails and threads, draft/compose/send/schedule "
        "email, manage drafts, labels, filters, and attachments."
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

        active_config = (
            self.app_config.get("app_config")
            or self.app_config.get("shared_config")
            or self.app_config.get("personal_config")
        )
        if isinstance(active_config, str):
            try:
                active_config = json.loads(active_config) if active_config else {}
            except json.JSONDecodeError:
                active_config = {}
        permissions = (active_config.get("permissions") or "full") if isinstance(active_config, dict) else "full"
        if isinstance(permissions, str):
            permissions = permissions.strip().lower()
        if permissions not in ("full", "read-only", "readonly"):
            permissions = "full"
        if permissions == "readonly":
            permissions = "read-only"
        self._permissions = permissions

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("GmailFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GmailFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GmailFunctions initialized")

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

    def _write_guard(self) -> Optional[Dict]:
        """Return an error dict if the connected account is read-only, else None."""
        if self._permissions == "read-only":
            return {
                "success": False,
                "response": (
                    "Gmail app is set to read-only. Sending, drafting, updating/deleting drafts, "
                    "changing labels, deleting emails, batch operations, and creating/updating/"
                    "deleting labels or filters are not allowed."
                ),
            }
        return None

    async def _gmail_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Optional[Dict]:
        url = f"{GMAIL_API_BASE}{path}" if path.startswith("/") else f"{GMAIL_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._gmail_request(method, path, json_body=json_body, params=params, retry_401=False)
        if r.status_code >= 400:
            logger.warning("Gmail API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            return None
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except Exception:
            return None

    async def _gmail_batch_get_messages(self, message_ids: List[str], fmt: str = "metadata") -> List[Dict]:
        if not message_ids or not self._httpx_client:
            return []
        boundary = "batch_gmail_" + str(uuid.uuid4()).replace("-", "")
        results: List[Dict] = []
        for chunk_start in range(0, len(message_ids), GMAIL_BATCH_CHUNK_SIZE):
            chunk = message_ids[chunk_start: chunk_start + GMAIL_BATCH_CHUNK_SIZE]
            parts = [
                f"Content-Type: application/http\r\n\r\nGET /gmail/v1/users/me/messages/{mid}?format={fmt} HTTP/1.1\r\n"
                for mid in chunk
            ]
            body = "\r\n".join([f"--{boundary}\r\n" + p for p in parts]) + f"\r\n--{boundary}--\r\n"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": f"multipart/mixed; boundary={boundary}",
            }
            try:
                r = await self._httpx_client.request(
                    "POST", GMAIL_BATCH_URL, content=body.encode("utf-8"), headers=headers
                )
            except Exception as e:
                logger.warning("Gmail batch request failed: %s", e)
                break
            if r.status_code == 401 and await self._refresh_access_token():
                self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
                return await self._gmail_batch_get_messages(message_ids, fmt=fmt)
            if r.status_code >= 400:
                logger.warning("Gmail batch API %s: %s", r.status_code, (r.text or "")[:500])
                break
            ct = r.headers.get("content-type", "")
            results.extend(_parse_batch_response(r.content, ct))
        return results

    # ------------------------------------------------------------------
    # Raw API layer (private)
    # ------------------------------------------------------------------

    async def _list_messages(
        self,
        max_results: int = DEFAULT_MAX_RESULTS,
        q: Optional[str] = None,
        page_token: Optional[str] = None,
        fmt: str = "metadata",
    ) -> Tuple[List[Dict], Optional[str]]:
        """Returns (ordered messages with metadata, next_page_token).

        fmt="full" also fetches body content (needed for analyze_emails); the
        default "metadata" is cheaper and sufficient for headers/snippet only.
        """
        params: Dict[str, Any] = {"maxResults": min(max_results, MAX_RESULTS_CAP)}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        data = await self._gmail_request("GET", "/messages", params=params)
        if not data or "messages" not in data:
            return [], None
        msg_list = data.get("messages", [])[:max_results]
        message_ids = [m.get("id") for m in msg_list if m.get("id")]
        if not message_ids:
            return [], data.get("nextPageToken")
        messages = await self._gmail_batch_get_messages(message_ids, fmt=fmt)
        id_to_msg = {m.get("id"): m for m in messages if m.get("id")}
        ordered = [id_to_msg[mid] for mid in message_ids if mid in id_to_msg]
        return ordered, data.get("nextPageToken")

    async def _get_message(self, message_id: str, format: str = "full") -> Optional[Dict]:
        return await self._gmail_request("GET", f"/messages/{message_id}", params={"format": format})

    async def _get_thread_raw(self, thread_id: str) -> Optional[Dict]:
        return await self._gmail_request("GET", f"/threads/{thread_id}", params={"format": "full"})

    async def _list_threads_raw(
        self, max_results: int = DEFAULT_MAX_RESULTS, q: Optional[str] = None
    ) -> Tuple[List[Dict], Optional[str]]:
        params: Dict[str, Any] = {"maxResults": min(max_results, MAX_RESULTS_CAP)}
        if q:
            params["q"] = q
        data = await self._gmail_request("GET", "/threads", params=params)
        if not data or "threads" not in data:
            return [], None
        return data.get("threads", [])[:max_results], data.get("nextPageToken")

    async def _trash_message_raw(self, message_id: str) -> Optional[Dict]:
        """Move a message to Trash (recoverable). Gmail API: messages.trash."""
        return await self._gmail_request("POST", f"/messages/{message_id}/trash")

    async def _untrash_message_raw(self, message_id: str) -> Optional[Dict]:
        """Restore a message from Trash. Gmail API: messages.untrash."""
        return await self._gmail_request("POST", f"/messages/{message_id}/untrash")

    async def _modify_thread_labels_raw(
        self, thread_id: str, add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> Optional[Dict]:
        body: Dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not body:
            return await self._get_thread_raw(thread_id)
        return await self._gmail_request("POST", f"/threads/{thread_id}/modify", json_body=body)

    async def _send_message_with_status(
        self, raw: str, thread_id: Optional[str] = None, retry_401: bool = True
    ) -> Tuple[Optional[Dict], Optional[int]]:
        body: Dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        url = f"{GMAIL_API_BASE}/messages/send"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        r = await self._httpx_client.request("POST", url, json=body)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._send_message_with_status(raw, thread_id=thread_id, retry_401=False)
        if r.status_code >= 400:
            logger.warning("Gmail API POST /messages/send -> %s %s", r.status_code, (r.text or "")[:500])
            return None, r.status_code
        if r.status_code == 204 or not r.content:
            return {}, r.status_code
        try:
            return r.json(), r.status_code
        except Exception:
            return None, r.status_code

    def _to_int_or_none(self, value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def _resolve_sender_email(self) -> str:
        candidates = [
            self.credentials.get("email"),
            self.credentials.get("emailAddress"),
            self.credentials.get("user_email"),
            self.credentials.get("account_email"),
            self.credentials.get("username"),
            self.service_credential.get("email"),
            self.service_credential.get("emailAddress"),
            self.service_credential.get("user_email"),
        ]
        for val in candidates:
            s = (str(val).strip() if val is not None else "")
            if "@" in s:
                return s
        return ""

    def _resolve_tracking_base_url(self) -> str:
        """Public base URL for open-tracking pixel callback."""
        candidates = [
            os.getenv("EMAIL_OPEN_TRACK_BASE_URL"),
            os.getenv("CALL_WEBHOOK_BASE_URL"),
            os.getenv("BACKEND_URL"),
            os.getenv("COMPANY_URL"),
        ]
        for c in candidates:
            v = (c or "").strip().rstrip("/")
            if v.startswith("http://") or v.startswith("https://"):
                return v
        return ""

    def _resolve_tracking_secret(self) -> str:
        """HMAC secret for signed tracking URLs."""
        return (os.getenv("EMAIL_TRACKING_SECRET") or os.getenv("CALL_SERVICE_TOKEN") or "").strip()

    def _build_open_tracking_url(
        self,
        *,
        activity_id: Optional[int],
        project_fid: Optional[int],
        connected_account_fid: Optional[int],
        external_message_id: Optional[str] = None,
    ) -> Optional[str]:
        """Create a signed tracking URL for the email-open pixel."""
        if activity_id is None or project_fid is None or connected_account_fid is None or not self.company_id:
            return None
        base = self._resolve_tracking_base_url()
        secret = self._resolve_tracking_secret()
        if not base or not secret:
            return None
        ts = int(datetime.utcnow().timestamp())
        cid = str(self.company_id)
        data = f"{activity_id}:{project_fid}:{connected_account_fid}:{cid}:{ts}"
        sig = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
        params = {
            "aid": str(activity_id),
            "pid": str(project_fid),
            "caf": str(connected_account_fid),
            "cid": cid,
            "ts": str(ts),
            "sig": sig,
        }
        if external_message_id:
            params["mid"] = str(external_message_id).strip()
        return f"{base}/email/track/open.gif?{urlencode(params)}"

    def _inject_open_tracking_pixel(
        self, *, body_text: str, is_html: bool, tracking_url: Optional[str]
    ) -> Tuple[str, Optional[str]]:
        """
        Ensure the outgoing email includes the open-tracking pixel.
        Returns (body_text, secondary_html_body). secondary_html_body is only set
        for a plain-text email, where a parallel HTML part carrying the invisible
        pixel is needed alongside the plain part (multipart/alternative) since a
        pixel <img> can't be embedded in plain text.
        """
        if not tracking_url:
            return body_text, None
        pixel = f'<img src="{tracking_url}" width="1" height="1" style="display:none;max-height:1px;max-width:1px;" alt="" />'
        if is_html:
            html_text = body_text or ""
            if re.search(r"</body\s*>", html_text, flags=re.IGNORECASE):
                html_text = re.sub(r"</body\s*>", pixel + "</body>", html_text, count=1, flags=re.IGNORECASE)
            else:
                html_text = html_text + pixel
            return html_text, None
        escaped = html.escape(body_text or "").replace("\n", "<br>")
        return body_text, f"<div>{escaped}</div>{pixel}"

    async def _log_email_activity_before_send(
        self, *, to_address: str, subject: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[int]:
        if not self.token or not self.company_id or not _COMPANY_URL:
            return None
        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(
            self.app_config.get("project_fid") or self.app_config.get("projectFid") or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            logger.warning(
                "Skipping email activity log: missing project_fid/connected_account_fid (project_fid=%s connected_account_fid=%s)",
                project_fid, connected_account_fid,
            )
            return None
        activity_payload: Dict[str, Any] = {
            "agent_activity_id": None,
            "project_fid": project_fid,
            "connected_account_fid": connected_account_fid,
            "activity_type": "email",
            "direction": "outbound",
            "external_message_id": f"gmail-pre-{uuid.uuid4().hex[:16]}",
            "to_address": (to_address or "").strip(),
            "from_address": self._resolve_sender_email(),
            "subject": (subject or "").strip(),
            "content": (content or "").strip(),
            "status": "queued",
            "metadata": metadata or {},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_COMPANY_URL}/aiagentchat/activity",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": json.dumps([str(self.company_id)]),
                    },
                    json=activity_payload,
                )
            if resp.status_code != 200:
                logger.warning("Email activity pre-send log failed: status=%s body=%s", resp.status_code, resp.text[:500])
                return None
            data = resp.json()
            activity_id = ((data.get("data") or {}).get("agent_activity_id")) if isinstance(data, dict) else None
            return self._to_int_or_none(activity_id)
        except Exception as e:
            logger.warning("Email activity pre-send log error: %s", e)
            return None

    async def _log_email_activity_after_send(
        self,
        *,
        activity_id: Optional[int],
        to_address: str,
        subject: str,
        content: str,
        external_message_id: Optional[str],
        gmail_status_code: Optional[int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        if not self.token or not self.company_id or not _COMPANY_URL:
            return None
        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(
            self.app_config.get("project_fid") or self.app_config.get("projectFid") or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            logger.warning(
                "Skipping email activity post-send log: missing project_fid/connected_account_fid (project_fid=%s connected_account_fid=%s)",
                project_fid, connected_account_fid,
            )
            return None
        post_metadata = {
            **(metadata or {}),
            "send_result": {"status_code": gmail_status_code, "external_message_id": external_message_id},
        }
        activity_payload: Dict[str, Any] = {
            "agent_activity_id": activity_id,
            "project_fid": project_fid,
            "connected_account_fid": connected_account_fid,
            "activity_type": "email",
            "direction": "outbound",
            "external_message_id": external_message_id or f"gmail-{uuid.uuid4().hex[:16]}",
            "to_address": (to_address or "").strip(),
            "from_address": self._resolve_sender_email(),
            "subject": (subject or "").strip(),
            "content": (content or "").strip(),
            "status": "sent",
            "sent_at": datetime.utcnow().isoformat(),
            "metadata": post_metadata,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_COMPANY_URL}/aiagentchat/activity",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": json.dumps([str(self.company_id)]),
                    },
                    json=activity_payload,
                )
            if resp.status_code != 200:
                logger.warning("Email activity post-send log failed: status=%s body=%s", resp.status_code, resp.text[:500])
                return None
            data = resp.json()
            return self._to_int_or_none(((data.get("data") or {}).get("agent_activity_id")) if isinstance(data, dict) else None)
        except Exception as e:
            logger.warning("Email activity post-send log error: %s", e)
            return None

    async def _log_email_activity_opened(
        self,
        *,
        external_message_id: str,
        from_address: str,
        to_address: str,
        subject: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Log an email opened/read activity with an opened_at timestamp."""
        if not self.token or not self.company_id or not _COMPANY_URL:
            return None
        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(
            self.app_config.get("project_fid") or self.app_config.get("projectFid") or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            return None
        activity_payload: Dict[str, Any] = {
            "agent_activity_id": None,
            "project_fid": project_fid,
            "connected_account_fid": connected_account_fid,
            "activity_type": "email",
            "direction": "inbound",
            "external_message_id": (external_message_id or "").strip(),
            "to_address": (to_address or "").strip(),
            "from_address": (from_address or "").strip(),
            "subject": (subject or "").strip(),
            "content": (content or "").strip(),
            "status": "opened",
            "opened_at": datetime.utcnow().isoformat(),
            "metadata": {**(metadata or {}), "open_result": {"source": "gmail_tool", "opened_via": "read"}},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_COMPANY_URL}/aiagentchat/activity",
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": json.dumps([str(self.company_id)]),
                    },
                    json=activity_payload,
                )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return self._to_int_or_none(((data.get("data") or {}).get("agent_activity_id")) if isinstance(data, dict) else None)
        except Exception as e:
            logger.warning("Email activity opened log error: %s", e)
            return None

    async def _create_draft(self, raw: str, thread_id: Optional[str] = None) -> Optional[Dict]:
        message: Dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return await self._gmail_request("POST", "/drafts", json_body={"message": message})

    async def _list_drafts_raw(
        self, max_results: int = 20, q: Optional[str] = None
    ) -> Tuple[List[Dict], Optional[str]]:
        params: Dict[str, Any] = {"maxResults": min(max_results, MAX_RESULTS_CAP)}
        if q:
            params["q"] = q
        data = await self._gmail_request("GET", "/drafts", params=params)
        if not data or "drafts" not in data:
            return [], data.get("nextPageToken") if data else None
        return data.get("drafts", []), data.get("nextPageToken")

    async def _get_draft_raw(self, draft_id: str) -> Optional[Dict]:
        return await self._gmail_request("GET", f"/drafts/{draft_id}")

    async def _update_draft_raw(self, draft_id: str, raw: str, thread_id: Optional[str] = None) -> Optional[Dict]:
        body: Dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        return await self._gmail_request("PUT", f"/drafts/{draft_id}", json_body=body)

    async def _delete_draft_raw(self, draft_id: str) -> bool:
        r = await self._gmail_request("DELETE", f"/drafts/{draft_id}")
        return r is not None

    async def _list_labels_raw(self) -> List[Dict]:
        data = await self._gmail_request("GET", "/labels")
        return (data or {}).get("labels", []) if data else []

    async def _modify_message_labels(
        self, message_id: str, add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> Optional[Dict]:
        body: Dict[str, Any] = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not body:
            return await self._get_message(message_id, format="metadata")
        return await self._gmail_request("POST", f"/messages/{message_id}/modify", json_body=body)

    def _looks_like_gmail_label_id(self, s: str) -> bool:
        """True if s looks like a Gmail label ID (Label_xxx or system label), not a name."""
        if not s or not isinstance(s, str):
            return False
        s = s.strip()
        if "@" in s:
            return False
        if s.startswith("Label_"):
            return True
        system = ("INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "UNREAD", "IMPORTANT", "CATEGORY_")
        return any(s.upper() == x or s.upper().startswith(x) for x in system)

    async def _resolve_label_names_to_ids(self, id_or_name_list: List[str]) -> List[str]:
        """Resolve a list of label IDs or names to Gmail API label IDs, creating unknown names."""
        out = []
        for x in (id_or_name_list or []):
            s = (str(x).strip() if x is not None else "") or ""
            if not s:
                continue
            if self._looks_like_gmail_label_id(s):
                out.append(s)
                continue
            labels = await self._list_labels_raw()
            found = next((lb.get("id") for lb in (labels or []) if (lb.get("name") or "").strip().lower() == s.lower()), None)
            if found:
                out.append(found)
            else:
                created = await self._create_label_raw(name=s, message_list_visibility="show", label_list_visibility="labelShow")
                if created and created.get("id"):
                    out.append(created["id"])
        return out

    async def _get_profile_raw(self) -> Optional[Dict]:
        return await self._gmail_request("GET", "/profile")

    async def _list_filters_raw(self) -> List[Dict]:
        data = await self._gmail_request("GET", "/settings/filters")
        return (data or {}).get("filter", []) if data else []

    async def _delete_message_raw(self, message_id: str) -> bool:
        r = await self._gmail_request("DELETE", f"/messages/{message_id}")
        return r is not None

    async def _batch_modify_messages_raw(
        self, message_ids: List[str], add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> bool:
        body: Dict[str, Any] = {"ids": message_ids}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        r = await self._gmail_request("POST", "/messages/batchModify", json_body=body)
        return r is not None

    async def _batch_delete_messages_raw(self, message_ids: List[str]) -> bool:
        if not message_ids:
            return True
        r = await self._gmail_request("POST", "/messages/batchDelete", json_body={"ids": message_ids})
        return r is not None

    async def _create_label_raw(
        self, name: str, message_list_visibility: str = "show", label_list_visibility: str = "labelShow"
    ) -> Optional[Dict]:
        body = {"name": name, "messageListVisibility": message_list_visibility, "labelListVisibility": label_list_visibility}
        return await self._gmail_request("POST", "/labels", json_body=body)

    async def _update_label_raw(
        self,
        label_id: str,
        name: Optional[str] = None,
        message_list_visibility: Optional[str] = None,
        label_list_visibility: Optional[str] = None,
    ) -> Optional[Dict]:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if message_list_visibility is not None:
            body["messageListVisibility"] = message_list_visibility
        if label_list_visibility is not None:
            body["labelListVisibility"] = label_list_visibility
        if not body:
            return await self._gmail_request("GET", f"/labels/{label_id}")
        return await self._gmail_request("PUT", f"/labels/{label_id}", json_body=body)

    async def _delete_label_raw(self, label_id: str) -> bool:
        r = await self._gmail_request("DELETE", f"/labels/{label_id}")
        return r is not None

    async def _get_filter_raw(self, filter_id: str) -> Optional[Dict]:
        return await self._gmail_request("GET", f"/settings/filters/{filter_id}")

    async def _delete_filter_raw(self, filter_id: str) -> bool:
        r = await self._gmail_request("DELETE", f"/settings/filters/{filter_id}")
        return r is not None

    async def _create_filter_raw(self, criteria: Dict, action: Dict) -> Optional[Dict]:
        return await self._gmail_request("POST", "/settings/filters", json_body={"criteria": criteria, "action": action})

    async def _get_attachment_raw(self, message_id: str, attachment_id: str) -> Optional[Dict]:
        return await self._gmail_request("GET", f"/messages/{message_id}/attachments/{attachment_id}")

    def _extract_attachment_list(self, msg: Dict) -> List[Dict]:
        """From a full message payload, extract attachments: filename, mimeType, size, attachmentId."""
        out = []
        payload = msg.get("payload") or {}
        parts = payload.get("parts") or []
        if not parts and payload.get("filename"):
            bid = (payload.get("body") or {}).get("attachmentId")
            if bid:
                out.append({
                    "filename": payload.get("filename") or "attachment",
                    "mimeType": (payload.get("mimeType") or "application/octet-stream"),
                    "size": (payload.get("body") or {}).get("size", 0),
                    "attachmentId": bid,
                })
            return out
        for p in parts:
            filename = p.get("filename")
            if not filename:
                continue
            body = p.get("body") or {}
            aid = body.get("attachmentId")
            if aid:
                out.append({
                    "filename": filename,
                    "mimeType": (p.get("mimeType") or "application/octet-stream"),
                    "size": body.get("size", 0),
                    "attachmentId": aid,
                })
        return out

    def _build_raw_message(
        self,
        to: str,
        subject: str,
        body_text: str,
        *,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
        references: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
        is_html: bool = False,
        secondary_html_body: Optional[str] = None,
    ) -> str:
        """Build a base64url-encoded RFC 2822 message. body_text is plain text unless
        is_html=True (then it is the HTML content). secondary_html_body is internal-only:
        used by the plain-text send path to attach a parallel HTML part carrying the
        open-tracking pixel (multipart/alternative), never exposed as a tool parameter."""
        to = _normalize_address_list(to)
        cc = _normalize_address_list(cc) if cc else None
        bcc = _normalize_address_list(bcc) if bcc else None
        use_attachments = bool(attachments and isinstance(attachments, list))
        use_multipart_alt = (not is_html) and bool(secondary_html_body and secondary_html_body.strip())

        msg = MIMEMultipart("mixed") if use_attachments else MIMEMultipart("alternative")
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        if is_html:
            msg.attach(MIMEText(body_text or "", "html", "utf-8"))
        elif use_multipart_alt:
            msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
            msg.attach(MIMEText(secondary_html_body.strip(), "html", "utf-8"))
        else:
            msg.attach(MIMEText(body_text or "", "plain", "utf-8"))

        if use_attachments:
            for att in attachments:
                filename = (att.get("filename") or att.get("name") or "attachment").strip()
                data_b64 = att.get("contentBase64") or att.get("data") or att.get("content")
                if not data_b64:
                    continue
                try:
                    raw_bytes = base64.urlsafe_b64decode(data_b64 + "==")
                except Exception:
                    try:
                        raw_bytes = base64.b64decode(data_b64)
                    except Exception:
                        continue
                att_mime = (att.get("mimeType") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
                main_type, sub_type = att_mime.split("/", 1) if "/" in att_mime else ("application", "octet-stream")
                part = MIMEBase(main_type, sub_type)
                part.set_payload(raw_bytes)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(part)
        if reply_to_message_id:
            msg["In-Reply-To"] = in_reply_to or reply_to_message_id
            msg["References"] = references or reply_to_message_id
        raw = msg.as_bytes()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _validate_email_for_send(
        self, to: str, subject: str, body: str, is_reply: bool = False
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Validate email fields before send/draft. Returns (valid, error_response_or_none)."""
        to_str = (to or "").strip()
        if not to_str or "@" not in to_str:
            return False, {"success": False, "response": "Email has no valid recipient (to)."}
        subj = (subject or "").strip()
        if not subj or _is_placeholder_content(subj):
            return False, {"success": False, "response": "Email subject is empty or a placeholder."}
        body_str = (body or "").strip()
        min_body_len = 15 if is_reply else 30
        if not body_str or _is_placeholder_content(body_str):
            return False, {"success": False, "response": "Email body is empty or a placeholder."}
        if len(body_str) < min_body_len:
            return False, {"success": False, "response": f"Email body is too short ({len(body_str)} chars); likely truncated."}
        return True, None

    # ------------------------------------------------------------------
    # @tool public methods — one per Gmail action
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Search or list email MESSAGES in the mailbox using Gmail search syntax. "
            "Returns each message's id, thread_id, from, to, subject, date, snippet, and label ids. "
            "Use this for any 'show me emails about X', 'find emails from Y', 'unread emails', "
            "'emails with attachments', etc. "
            "Next: use read_email or summarize_email with the returned message id to view a full "
            "email, mark_as_read / archive_email / trash_email to act on a result, or draft_reply / "
            "send_email to reply."
        ),
        params={
            "gmail_search_query": GMAIL_SEARCH_SYNTAX_REFERENCE,
            "max_results": "Number of messages to return, 5-100 (default 20).",
        },
    )
    async def list_emails(self, gmail_search_query: Optional[str] = None, max_results: int = DEFAULT_MAX_RESULTS) -> Dict:
        max_results = min(max(5, int(max_results or DEFAULT_MAX_RESULTS)), MAX_RESULTS_CAP)
        q = _sanitize_gmail_q(gmail_search_query or "")
        messages, next_page_token = await self._list_messages(max_results=max_results, q=q if q else None)
        if not messages:
            return {"success": True, "response": "No emails found for the given criteria.", "messages": [], "count": 0}
        items = []
        for m in messages:
            headers = _parse_email_headers(m)
            snippet = _get_snippet_or_body(m)
            items.append({
                "id": m.get("id"),
                "threadId": m.get("threadId"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "snippet": (snippet[:500] if snippet else (m.get("snippet") or "")),
            })
        response = f"Found {len(items)} email(s). " + "; ".join(f"[{m.get('from', '')}] {m.get('subject', '')}" for m in items[:5])
        result = {"success": True, "response": response, "messages": items, "count": len(items)}
        if next_page_token:
            result["next_page_token"] = next_page_token
        return result

    @tool(
        description=(
            "Fetch the full content (headers, body text, attachment list) of ONE email message. "
            "Requires message_id from a prior list_emails or list_threads/get_thread result. "
            "Next: draft_reply (reply), send_email (forward/reply), download_attachment if it has "
            "attachments, mark_as_read / archive_email / trash_email to act on it, or "
            "summarize_email for a shorter version."
        ),
        params={"message_id": "The Gmail message id to read. Obtain from a list_emails result's 'id' field."},
    )
    async def read_email(self, message_id: str) -> Dict:
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        full = await self._get_message(str(message_id), format="full")
        if not full:
            return {"success": False, "response": "Could not fetch that email.", "message_id": message_id}
        headers = _parse_email_headers(full)
        body_text = _get_snippet_or_body(full)
        attachments = self._extract_attachment_list(full)
        att_str = ""
        if attachments:
            att_str = "\n\nAttachments:\n" + "\n".join(
                f"- {a.get('filename', 'file')} ({a.get('mimeType', '')}, {a.get('size', 0)} bytes, ID: {a.get('attachmentId', '')})"
                for a in attachments
            )
        # Only log "opened" for inbound emails, never for messages sent by this account —
        # otherwise reading back a just-sent message would immediately mark it as opened.
        label_ids: list = full.get("labelIds") or []
        is_outbound = (
            "SENT" in label_ids
            or "DRAFT" in label_ids
            or (
                headers.get("from")
                and self._resolve_sender_email()
                and self._resolve_sender_email().lower() in (headers.get("from") or "").lower()
            )
        )
        if not is_outbound:
            await self._log_email_activity_opened(
                external_message_id=str(full.get("id") or message_id),
                from_address=headers.get("from") or "",
                to_address=headers.get("to") or "",
                subject=headers.get("subject") or "",
                content=(body_text or "")[:4000],
                metadata={"thread_id": full.get("threadId"), "message_id": full.get("id") or message_id},
            )
        return {
            "success": True,
            "response": f"From: {headers.get('from')}\nTo: {headers.get('to')}\nSubject: {headers.get('subject')}\nDate: {headers.get('date')}\n\n{body_text or ''}{att_str}",
            "message_id": full.get("id"),
            "thread_id": full.get("threadId"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "body": body_text,
            "attachments": attachments,
        }

    @tool(
        description=(
            "Produce a condensed view of a single email: same content as read_email but truncated "
            "to ~1500 characters, useful when you only need the gist of a long email. "
            "Next: draft_reply to reply based on it, or modify_labels to file it."
        ),
        params={"message_id": "Message id to condense. Obtain from list_emails."},
    )
    async def summarize_email(self, message_id: str) -> Dict:
        if not message_id:
            return {"success": False, "response": "message_id is required.", "summary": None}
        msg = await self._get_message(str(message_id))
        if not msg:
            return {"success": False, "response": "Could not fetch that email.", "summary": None}
        headers = _parse_email_headers(msg)
        text = _get_snippet_or_body(msg) or msg.get("snippet") or ""
        summary = text[:1500] if len(text) > 1500 else text
        return {
            "success": True,
            "response": f"Summary: {summary}",
            "summary": summary,
            "from": headers.get("from"),
            "subject": headers.get("subject"),
            "message_id": message_id,
        }

    @tool(
        description=(
            "List conversation THREADS (grouped messages) matching a Gmail search query, instead of "
            "individual messages. Use when the user wants conversations grouped together, e.g. "
            "'show me my recent conversations with X'. Returns thread ids and snippets only. "
            "Next: get_thread or summarize_thread with the returned thread id for the full conversation."
        ),
        params={
            "gmail_search_query": GMAIL_SEARCH_SYNTAX_REFERENCE,
            "max_results": "Number of threads to return, 5-100 (default 20).",
        },
    )
    async def list_threads(self, gmail_search_query: Optional[str] = None, max_results: int = DEFAULT_MAX_RESULTS) -> Dict:
        max_results = min(max(5, int(max_results or DEFAULT_MAX_RESULTS)), MAX_RESULTS_CAP)
        q = _sanitize_gmail_q(gmail_search_query or "")
        threads, next_page_token = await self._list_threads_raw(max_results=max_results, q=q if q else None)
        if not threads:
            return {"success": True, "response": "No conversation threads found for the given criteria.", "threads": [], "count": 0}
        items = [{"id": t.get("id"), "snippet": t.get("snippet") or "", "historyId": t.get("historyId")} for t in threads]
        response = f"Found {len(items)} thread(s). " + "; ".join(f"{t.get('snippet', '')[:80]}" for t in items[:5])
        result = {"success": True, "response": response, "threads": items, "count": len(items)}
        if next_page_token:
            result["next_page_token"] = next_page_token
        return result

    @tool(
        description=(
            "Retrieve every message in a conversation thread (headers, snippets, label ids) by "
            "thread_id. Use after list_threads, or when a list_emails/read_email result includes a "
            "thread_id and the user wants the whole conversation. "
            "Next: summarize_thread to condense it, or draft_reply/send_email to reply to the latest "
            "message (use its message_id)."
        ),
        params={"thread_id": "The Gmail thread id. Obtain from list_emails, list_threads, or read_email results."},
    )
    async def get_thread(self, thread_id: str) -> Dict:
        if not thread_id:
            return {"success": False, "response": "thread_id is required."}
        thread = await self._get_thread_raw(str(thread_id))
        if not thread or "messages" not in thread:
            return {"success": False, "response": "Could not fetch that thread.", "thread_id": thread_id}
        messages = []
        for m in thread.get("messages", []):
            headers = _parse_email_headers(m)
            messages.append({
                "message_id": m.get("id"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "snippet": (m.get("snippet") or "")[:300],
                "labelIds": m.get("labelIds", []),
            })
        return {
            "success": True,
            "response": f"Thread has {len(messages)} message(s).",
            "thread_id": thread.get("id"),
            "messages": messages,
            "count": len(messages),
        }

    @tool(
        description=(
            "Produce a condensed view of an entire conversation thread: headers and body text "
            "(truncated) from up to the first 10 messages, concatenated. Use when the user asks "
            "'catch me up on this thread'. "
            "Next: draft_reply to reply to the latest message in the thread."
        ),
        params={"thread_id": "Thread id to condense. Obtain from list_threads or get_thread."},
    )
    async def summarize_thread(self, thread_id: str) -> Dict:
        if not thread_id:
            return {"success": False, "response": "thread_id is required.", "summary": None}
        thread = await self._get_thread_raw(str(thread_id))
        if not thread or "messages" not in thread:
            return {"success": False, "response": "Could not fetch that thread.", "summary": None}
        parts = []
        for m in thread.get("messages", [])[:10]:
            mid = m.get("id")
            if not mid:
                continue
            full_msg = await self._get_message(mid)
            if full_msg:
                h = _parse_email_headers(full_msg)
                text = _get_snippet_or_body(full_msg)
                parts.append(f"From: {h.get('from')}\nSubject: {h.get('subject')}\n{text[:2000]}")
        combined = "\n\n---\n\n".join(parts) if parts else "No messages in thread."
        summary = combined[:1500]
        return {"success": True, "response": f"Thread summary: {summary}", "summary": summary, "thread_id": thread_id}

    async def _send_email_impl(
        self,
        *,
        to: Optional[str],
        subject: Optional[str],
        body: Optional[str],
        cc: Optional[str],
        bcc: Optional[str],
        reply_to_message_id: Optional[str],
        is_html: bool,
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        to = _normalize_address_list(to)
        subject = (subject or "").strip()
        body = (body or "").strip()
        cc = _normalize_address_list(cc)
        bcc = _normalize_address_list(bcc)
        thread_id: Optional[str] = None
        in_reply_to_header: Optional[str] = None
        is_reply = bool(reply_to_message_id)

        if reply_to_message_id:
            orig = await self._get_message(str(reply_to_message_id))
            if not orig:
                return {"success": False, "response": "Could not load the message being replied to."}
            headers = _parse_email_headers(orig)
            to = to or _extract_email_from_header(headers.get("from", ""))
            subject = subject or headers.get("subject", "")
            if subject and not subject.startswith("Re:"):
                subject = f"Re: {subject}"
            thread_id = orig.get("threadId")
            headers_list = (orig.get("payload") or {}).get("headers") or []
            in_reply_to_header = next(
                (h.get("value") for h in headers_list if (h.get("name") or "").lower() == "message-id"), None
            )

        valid, issue = self._validate_email_for_send(to, subject, body, is_reply=is_reply)
        if not valid:
            return issue

        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(self.app_config.get("project_fid") or self.app_config.get("projectFid") or self.agent_id)
        pre_activity_id = await self._log_email_activity_before_send(
            to_address=to, subject=subject, content=body,
            metadata={"source": "gmail_tool", "mode": "reply" if is_reply else "new_email"},
        )
        tracking_url = self._build_open_tracking_url(
            activity_id=pre_activity_id, project_fid=project_fid, connected_account_fid=connected_account_fid,
        )
        body, secondary_html_body = self._inject_open_tracking_pixel(
            body_text=body, is_html=is_html, tracking_url=tracking_url,
        )
        raw = self._build_raw_message(
            to=to, subject=subject, body_text=body, cc=cc, bcc=bcc,
            reply_to_message_id=reply_to_message_id,
            in_reply_to=in_reply_to_header, references=in_reply_to_header,
            is_html=is_html, secondary_html_body=secondary_html_body,
        )
        sent, send_status_code = await self._send_message_with_status(raw, thread_id=thread_id)
        if not sent or not sent.get("id"):
            return {"success": False, "response": "Failed to send the email."}
        tracking_url_sent = self._build_open_tracking_url(
            activity_id=pre_activity_id, project_fid=project_fid, connected_account_fid=connected_account_fid,
            external_message_id=sent.get("id"),
        )
        await self._log_email_activity_after_send(
            activity_id=pre_activity_id, to_address=to, subject=subject, content=body,
            external_message_id=sent.get("id"), gmail_status_code=send_status_code,
            metadata={
                "source": "gmail_tool", "mode": "reply" if is_reply else "new_email",
                "open_tracking": {"enabled": bool(tracking_url_sent or tracking_url), "tracking_url": tracking_url_sent or tracking_url},
            },
        )
        return {
            "success": True,
            "response": "Reply sent successfully." if is_reply else "Email sent successfully.",
            "message_id": sent.get("id"),
        }

    @tool(
        description=(
            "Send a PLAIN TEXT email immediately. Either sends a brand-new email (provide "
            "to/subject/body), or, if reply_to_message_id is set, sends a threaded reply to that "
            "message directly (skips drafting). Compose the full subject and body yourself before "
            "calling — this tool sends exactly what you provide, it does not generate content. "
            "Use send_html_email instead if the content contains HTML markup/formatting. "
            "Next: usually the final step — nothing further needed unless the user also wants the "
            "sent message labeled/archived (use modify_labels with the returned message_id)."
        ),
        params={
            "to": "Recipient email address(es), comma-separated for multiple. Required for a new email; "
                  "if omitted for a reply, defaults to the original sender.",
            "subject": "Email subject line. Required for a new email; if omitted for a reply, defaults to 'Re: <original subject>'.",
            "body": "Plain text email body (no HTML tags). Required.",
            "cc": "Optional CC email address(es), comma-separated.",
            "bcc": "Optional BCC email address(es), comma-separated.",
            "reply_to_message_id": "Optional. If set, sends this as a threaded reply to that message id instead of a new email.",
        },
    )
    async def send_email(
        self,
        *,
        body: str,
        to: Optional[str] = None,
        subject: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        return await self._send_email_impl(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, reply_to_message_id=reply_to_message_id, is_html=False,
        )

    @tool(
        description=(
            "Send an HTML email immediately. Either sends a brand-new email (provide to/subject/"
            "html_body), or, if reply_to_message_id is set, sends a threaded reply to that message "
            "directly (skips drafting). Compose the full subject and complete HTML markup yourself "
            "before calling — this tool sends exactly what you provide, it does not generate content. "
            "Use send_email instead for plain text. "
            "Next: usually the final step — nothing further needed unless the user also wants the "
            "sent message labeled/archived (use modify_labels with the returned message_id)."
        ),
        params={
            "to": "Recipient email address(es), comma-separated for multiple. Required for a new email; "
                  "if omitted for a reply, defaults to the original sender.",
            "subject": "Email subject line. Required for a new email; if omitted for a reply, defaults to 'Re: <original subject>'.",
            "html_body": "Full HTML email body (e.g. '<p>Hello</p>'). Required.",
            "cc": "Optional CC email address(es), comma-separated.",
            "bcc": "Optional BCC email address(es), comma-separated.",
            "reply_to_message_id": "Optional. If set, sends this as a threaded reply to that message id instead of a new email.",
        },
    )
    async def send_html_email(
        self,
        *,
        html_body: str,
        to: Optional[str] = None,
        subject: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        return await self._send_email_impl(
            to=to, subject=subject, body=html_body, cc=cc, bcc=bcc, reply_to_message_id=reply_to_message_id, is_html=True,
        )

    @tool(
        description=(
            "Create a DRAFT reply to an existing message (does not send). Requires message_id of the "
            "email being replied to; threads the draft correctly (In-Reply-To/References/threadId). "
            "Compose the full reply body yourself before calling. "
            "Next: update_draft to edit it further, or get_draft then send_email (with reply_to_message_id) "
            "once the user approves."
        ),
        params={
            "message_id": "Message id being replied to. Obtain from a list_emails/read_email result.",
            "body": "Reply body text. Required.",
            "to": "Optional override for the reply recipient (defaults to the original sender).",
            "subject": "Optional override for the subject (defaults to 'Re: <original subject>').",
        },
    )
    async def draft_reply(self, message_id: str, body: str, to: Optional[str] = None, subject: Optional[str] = None) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        orig = await self._get_message(str(message_id))
        if not orig:
            return {"success": False, "response": "Could not load that email."}
        headers = _parse_email_headers(orig)
        to_addr = _normalize_address_list(to) or _extract_email_from_header(headers.get("from", ""))
        orig_subject = headers.get("subject", "")
        reply_subject = (subject or orig_subject or "")
        if reply_subject and not reply_subject.startswith("Re:"):
            reply_subject = f"Re: {reply_subject}"
        body = (body or "").strip() or "Thank you for your email. I will get back to you shortly."
        thread_id = orig.get("threadId")
        headers_list = (orig.get("payload") or {}).get("headers") or []
        ref_header = next((h.get("value") for h in headers_list if (h.get("name") or "").lower() == "message-id"), None)
        raw = self._build_raw_message(
            to=to_addr, subject=reply_subject, body_text=body,
            reply_to_message_id=message_id, references=ref_header, in_reply_to=ref_header,
        )
        draft = await self._create_draft(raw, thread_id=thread_id)
        if not draft or "id" not in draft:
            return {"success": False, "response": "Failed to create draft in Gmail."}
        return {
            "success": True,
            "response": f"Draft created. Reply to '{orig_subject}'. You can edit and send it in Gmail.",
            "draft_id": draft.get("id"),
            "message": draft.get("message"),
        }

    async def _compose_draft_impl(
        self, *, to: str, subject: str, body: str, cc: Optional[str], bcc: Optional[str], is_html: bool
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        to = _normalize_address_list(to)
        subject = (subject or "").strip() or "(No subject)"
        body = (body or "").strip()
        if not to:
            return {"success": False, "response": "Recipient (to) is required to create a draft."}
        raw = self._build_raw_message(
            to=to, subject=subject, body_text=body, cc=_normalize_address_list(cc), bcc=_normalize_address_list(bcc),
            is_html=is_html,
        )
        draft = await self._create_draft(raw)
        if not draft or "id" not in draft:
            return {"success": False, "response": "Failed to create draft in Gmail."}
        return {
            "success": True,
            "response": f"Draft created (To: {to[:50]}{'...' if len(to) > 50 else ''}, Subject: {subject}).",
            "draft_id": draft.get("id"),
            "message": draft.get("message"),
        }

    @tool(
        description=(
            "Create a brand-new PLAIN TEXT DRAFT email (not threaded to any existing message; does not "
            "send). Use when the user wants to prepare an email without sending it yet. Use "
            "compose_html_draft instead if the content contains HTML markup/formatting. "
            "Next: update_draft to revise, get_draft/list_drafts to review, or send_email once ready."
        ),
        params={
            "to": "Recipient email address(es), comma-separated. Required.",
            "subject": "Email subject line. Required.",
            "body": "Plain text email body (no HTML tags). Required.",
            "cc": "Optional CC email address(es), comma-separated.",
            "bcc": "Optional BCC email address(es), comma-separated.",
        },
    )
    async def compose_draft(
        self, to: str, subject: str, body: str, cc: Optional[str] = None, bcc: Optional[str] = None,
    ) -> Dict:
        return await self._compose_draft_impl(to=to, subject=subject, body=body, cc=cc, bcc=bcc, is_html=False)

    @tool(
        description=(
            "Create a brand-new HTML DRAFT email (not threaded to any existing message; does not "
            "send). Use when the user wants to prepare an HTML-formatted email without sending it yet. "
            "Use compose_draft instead for plain text. "
            "Next: update_draft to revise, get_draft/list_drafts to review, or send_html_email once ready."
        ),
        params={
            "to": "Recipient email address(es), comma-separated. Required.",
            "subject": "Email subject line. Required.",
            "html_body": "Full HTML email body (e.g. '<p>Hello</p>'). Required.",
            "cc": "Optional CC email address(es), comma-separated.",
            "bcc": "Optional BCC email address(es), comma-separated.",
        },
    )
    async def compose_html_draft(
        self, to: str, subject: str, html_body: str, cc: Optional[str] = None, bcc: Optional[str] = None,
    ) -> Dict:
        return await self._compose_draft_impl(to=to, subject=subject, body=html_body, cc=cc, bcc=bcc, is_html=True)

    @tool(
        description=(
            "Create a draft intended to be sent at a later time. Gmail's API has no native "
            "scheduled-send endpoint for apps, so this creates a fresh (non-reply) draft and returns "
            "send_at for a scheduler/workflow trigger to send it later. "
            "Next: a scheduler step should later call send_email (or get_draft then send_email) using "
            "the returned draft_id at the scheduled time."
        ),
        params={
            "to": "Recipient email address(es), comma-separated. Required.",
            "subject": "Email subject line. Required.",
            "body": "Email body text. Required.",
            "send_at": "ISO 8601 timestamp for when the email should be sent (used by the scheduler, not Gmail directly). Required.",
            "cc": "Optional CC email address(es), comma-separated.",
            "bcc": "Optional BCC email address(es), comma-separated.",
        },
    )
    async def schedule_email(
        self, to: str, subject: str, body: str, send_at: str, cc: Optional[str] = None, bcc: Optional[str] = None
    ) -> Dict:
        result = await self.compose_draft(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        if result.get("success") and send_at:
            result["scheduled_send_at"] = send_at
            result["response"] = (result.get("response") or "") + f" Scheduled to send at: {send_at}."
        return result

    @tool(
        description="List the user's saved Gmail drafts (ids and a snippet of each). "
                    "Next: get_draft for full content, update_draft to edit, delete_draft to discard, or send it.",
        params={
            "max_results": "Number of drafts to return, 5-100 (default 20).",
            "gmail_search_query": "Optional Gmail search query to filter drafts (same syntax as list_emails).",
        },
    )
    async def list_drafts(self, max_results: int = 20, gmail_search_query: Optional[str] = None) -> Dict:
        max_results = min(max(5, int(max_results or 20)), MAX_RESULTS_CAP)
        q = _sanitize_gmail_q(gmail_search_query or "")
        drafts, next_token = await self._list_drafts_raw(max_results=max_results, q=q if q else None)
        if not drafts:
            return {"success": True, "response": "No drafts found.", "drafts": [], "count": 0}
        items = []
        for d in drafts:
            msg = d.get("message") or {}
            headers = _parse_email_headers(msg)
            items.append({
                "draft_id": d.get("id"),
                "message_id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
            })
        result = {"success": True, "response": f"Found {len(items)} draft(s).", "drafts": items, "count": len(items)}
        if next_token:
            result["next_page_token"] = next_token
        return result

    @tool(
        description="Fetch the full content (to/subject/body preview) of one draft by draft_id. "
                    "Next: update_draft to change it, or delete_draft to remove it.",
        params={"draft_id": "The draft id. Obtain from list_drafts."},
    )
    async def get_draft(self, draft_id: str) -> Dict:
        if not draft_id:
            return {"success": False, "response": "draft_id is required."}
        draft = await self._get_draft_raw(str(draft_id))
        if not draft:
            return {"success": False, "response": "Could not fetch that draft."}
        msg = draft.get("message") or {}
        headers = _parse_email_headers(msg)
        body = _get_snippet_or_body(msg)
        return {
            "success": True,
            "response": f"Draft: {headers.get('subject', '(no subject)')}",
            "draft_id": draft_id,
            "message_id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "body_preview": (body or "")[:1000],
        }

    @tool(
        description="Replace the content of an existing draft (to/subject/body). "
                    "Next: get_draft to verify, or proceed to send the message.",
        params={
            "draft_id": "The draft id to update. Obtain from list_drafts or get_draft.",
            "to": "New recipient email address(es), comma-separated.",
            "subject": "New subject line.",
            "body": "New body text.",
        },
    )
    async def update_draft(
        self, draft_id: str, to: Optional[str] = None, subject: Optional[str] = None, body: Optional[str] = None
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not draft_id:
            return {"success": False, "response": "draft_id is required."}
        to = (to or "").strip()
        subject = (subject or "").strip()
        body = (body or "").strip()
        if not to and not subject and not body:
            return {"success": False, "response": "Provide to, subject, or body to update the draft."}
        existing = await self._get_draft_raw(str(draft_id))
        if not existing:
            return {"success": False, "response": "Could not load that draft."}
        msg = existing.get("message") or {}
        headers = _parse_email_headers(msg)
        to = to or headers.get("to", "")
        subject = subject or headers.get("subject", "(No subject)")
        body = body or _get_snippet_or_body(msg)
        raw = self._build_raw_message(to=to, subject=subject, body_text=body)
        updated = await self._update_draft_raw(str(draft_id), raw, thread_id=msg.get("threadId"))
        if not updated:
            return {"success": False, "response": "Failed to update draft."}
        return {"success": True, "response": "Draft updated.", "draft_id": draft_id}

    @tool(
        description="Permanently delete a draft (cannot be undone). Next: list_drafts to confirm it is gone.",
        params={"draft_id": "The draft id to delete. Obtain from list_drafts."},
    )
    async def delete_draft(self, draft_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not draft_id:
            return {"success": False, "response": "draft_id is required."}
        ok = await self._delete_draft_raw(str(draft_id))
        if not ok:
            return {"success": False, "response": "Could not delete that draft."}
        return {"success": True, "response": "Draft deleted.", "draft_id": draft_id}

    @tool(
        description=(
            "List every Gmail label (system labels like INBOX/SENT/TRASH/UNREAD/STARRED and "
            "user-created labels) with their ids and names. Use this BEFORE modify_labels/"
            "create_label/get_or_create_label to discover valid label ids. "
            "Next: get_or_create_label or create_label if the needed label doesn't exist, then "
            "modify_labels to apply it to messages."
        ),
    )
    async def list_labels(self) -> Dict:
        labels = await self._list_labels_raw()
        if not labels:
            return {"success": True, "response": "No labels found.", "labels": []}
        items = [{"id": lb.get("id"), "name": lb.get("name"), "type": lb.get("type")} for lb in labels]
        return {"success": True, "response": f"Found {len(items)} label(s).", "labels": items, "count": len(items)}

    @tool(
        description=(
            "Add and/or remove labels on a single message — the general-purpose way to archive "
            "(remove INBOX), mark read/unread (remove/add UNREAD), star (add STARRED), or file into "
            "a custom label. Label values may be Gmail label ids (e.g. 'Label_123', 'STARRED') or "
            "label names — names are resolved automatically (and created if they don't exist). "
            "Next: none required, or list_emails with an updated query to verify the change."
        ),
        params={
            "message_id": "The message id to modify. Obtain from a list_emails/read_email result.",
            "add_label_ids": "List of label ids or names to add, e.g. ['STARRED'] or ['Important Clients'].",
            "remove_label_ids": "List of label ids or names to remove, e.g. ['UNREAD'] or ['INBOX'] (to archive).",
        },
    )
    async def modify_labels(
        self, message_id: str, add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        add_label_ids = add_label_ids or []
        remove_label_ids = remove_label_ids or []
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "Provide add_label_ids and/or remove_label_ids."}
        add_ids = await self._resolve_label_names_to_ids(add_label_ids)
        remove_ids = await self._resolve_label_names_to_ids(remove_label_ids)
        if not add_ids and not remove_ids:
            return {"success": False, "response": "No valid label ids after resolving names."}
        updated = await self._modify_message_labels(str(message_id), add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)
        if not updated:
            return {"success": False, "response": "Could not update message labels."}
        return {"success": True, "response": "Message labels updated.", "message_id": message_id, "label_ids": updated.get("labelIds", [])}

    @tool(
        description="Like modify_labels but applies to ALL messages in a conversation thread at once "
                    "(e.g. archive or label an entire conversation). Next: none required, or get_thread to verify.",
        params={
            "thread_id": "The thread id to modify. Obtain from list_threads/get_thread or a list_emails result's threadId.",
            "add_label_ids": "List of label ids or names to add to every message in the thread.",
            "remove_label_ids": "List of label ids or names to remove from every message in the thread.",
        },
    )
    async def modify_thread_labels(
        self, thread_id: str, add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not thread_id:
            return {"success": False, "response": "thread_id is required."}
        add_label_ids = add_label_ids or []
        remove_label_ids = remove_label_ids or []
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "Provide add_label_ids and/or remove_label_ids."}
        add_ids = await self._resolve_label_names_to_ids(add_label_ids)
        remove_ids = await self._resolve_label_names_to_ids(remove_label_ids)
        if not add_ids and not remove_ids:
            return {"success": False, "response": "No valid label ids after resolving names."}
        updated = await self._modify_thread_labels_raw(str(thread_id), add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)
        if not updated:
            return {"success": False, "response": "Could not update thread labels."}
        return {"success": True, "response": "Thread labels updated.", "thread_id": thread_id}

    @tool(
        description="Mark one message as read (removes the UNREAD label). Next: none required.",
        params={"message_id": "The message id to mark as read. Obtain from a list_emails/read_email result."},
    )
    async def mark_as_read(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        updated = await self._modify_message_labels(str(message_id), remove_label_ids=["UNREAD"])
        if not updated:
            return {"success": False, "response": "Could not mark the message as read."}
        return {"success": True, "response": "Message marked as read.", "message_id": message_id}

    @tool(
        description="Mark one message as unread (adds the UNREAD label). Next: none required.",
        params={"message_id": "The message id to mark as unread. Obtain from a list_emails/read_email result."},
    )
    async def mark_as_unread(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        updated = await self._modify_message_labels(str(message_id), add_label_ids=["UNREAD"])
        if not updated:
            return {"success": False, "response": "Could not mark the message as unread."}
        return {"success": True, "response": "Message marked as unread.", "message_id": message_id}

    @tool(
        description=(
            "Archive a message (removes the INBOX label) without deleting it — still searchable "
            "in 'All Mail'. Next: none required, or trash_email if the user actually wants it deleted."
        ),
        params={"message_id": "The message id to archive. Obtain from a list_emails/read_email result."},
    )
    async def archive_email(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        updated = await self._modify_message_labels(str(message_id), remove_label_ids=["INBOX"])
        if not updated:
            return {"success": False, "response": "Could not archive the message."}
        return {"success": True, "response": "Message archived (removed from Inbox).", "message_id": message_id}

    @tool(
        description=(
            "Move a single message to Trash. This is REVERSIBLE — Gmail keeps trashed messages for "
            "30 days and untrash_email restores them. Prefer this over delete_email (permanent) "
            "unless the user explicitly says 'permanently delete' or 'delete forever'. "
            "Next: untrash_email if the user changes their mind."
        ),
        params={"message_id": "The message id to move to Trash. Obtain from a list_emails/read_email result."},
    )
    async def trash_email(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        result = await self._trash_message_raw(str(message_id))
        if not result:
            return {"success": False, "response": "Could not move the message to Trash."}
        return {"success": True, "response": "Message moved to Trash (recoverable for 30 days).", "message_id": message_id}

    @tool(
        description="Restore a previously trashed message back to its prior labels/location. Next: none required.",
        params={"message_id": "The message id to restore. Obtain from list_emails filtered with 'in:trash'."},
    )
    async def untrash_email(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        result = await self._untrash_message_raw(str(message_id))
        if not result:
            return {"success": False, "response": "Could not restore the message from Trash."}
        return {"success": True, "response": "Message restored from Trash.", "message_id": message_id}

    @tool(
        description=(
            "PERMANENTLY delete a single message — bypasses Trash entirely and cannot be recovered. "
            "Only use when the user explicitly says 'permanently delete' / 'delete forever'; "
            "otherwise prefer trash_email (reversible). Next: none — this is terminal."
        ),
        params={"message_id": "The message id to permanently delete. Obtain from a list_emails/read_email result."},
    )
    async def delete_email(self, message_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not message_id:
            return {"success": False, "response": "message_id is required."}
        ok = await self._delete_message_raw(str(message_id))
        if not ok:
            return {"success": False, "response": "Could not delete that email."}
        return {"success": True, "response": "Email permanently deleted.", "message_id": message_id}

    @tool(
        description=(
            "Return the authenticated user's email address, total message count, total thread count, "
            "and current history id. Use to confirm which account is connected or get baseline counts."
        ),
    )
    async def get_profile(self) -> Dict:
        profile = await self._get_profile_raw()
        if not profile:
            return {"success": False, "response": "Could not fetch Gmail profile."}
        return {
            "success": True,
            "response": f"Profile: {profile.get('emailAddress', '')} — {profile.get('messagesTotal', 0)} messages, {profile.get('threadsTotal', 0)} threads.",
            "emailAddress": profile.get("emailAddress"),
            "messagesTotal": profile.get("messagesTotal"),
            "threadsTotal": profile.get("threadsTotal"),
            "historyId": profile.get("historyId"),
        }

    @tool(
        description=(
            "List the user's mail filters (rules that auto-apply labels, forward, archive, or delete "
            "incoming mail matching criteria). "
            "Next: get_filter for one filter's full criteria/action, create_filter to add a new rule, "
            "or delete_filter to remove one."
        ),
    )
    async def list_filters(self) -> Dict:
        filters = await self._list_filters_raw()
        if not filters:
            return {"success": True, "response": "No filters found (or filters API not available).", "filters": []}
        items = [{"id": f.get("id"), "criteria": f.get("criteria") or {}, "action": f.get("action") or {}} for f in filters[:50]]
        return {"success": True, "response": f"Found {len(items)} filter(s).", "filters": items, "count": len(items)}

    @tool(
        description=(
            "Fetch the full criteria (from/to/subject/query/hasAttachment/size, etc.) and action "
            "(addLabelIds/removeLabelIds/forward) of a single filter by filter_id. "
            "Next: delete_filter to remove it, or create_filter to add a similar one with changes "
            "(filters cannot be updated in place — delete and recreate)."
        ),
        params={"filter_id": "The filter id. Obtain from list_filters."},
    )
    async def get_filter(self, filter_id: str) -> Dict:
        if not filter_id:
            return {"success": False, "response": "filter_id is required."}
        f = await self._get_filter_raw(str(filter_id))
        if not f:
            return {"success": False, "response": "Could not fetch that filter."}
        return {"success": True, "response": f"Filter: {f.get('criteria')} -> {f.get('action')}.", "filter": f}

    @tool(
        description="Permanently delete a filter by filter_id. Next: create_filter if a replacement rule is needed.",
        params={"filter_id": "The filter id to delete. Obtain from list_filters."},
    )
    async def delete_filter(self, filter_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not filter_id:
            return {"success": False, "response": "filter_id is required."}
        ok = await self._delete_filter_raw(str(filter_id))
        if not ok:
            return {"success": False, "response": "Could not delete that filter."}
        return {"success": True, "response": "Filter deleted.", "filter_id": filter_id}

    @tool(
        description=(
            "Create a new filter that automatically applies an action to incoming mail matching "
            "criteria (e.g. auto-label or auto-archive mail from a sender). If the action references "
            "a custom label, first run get_or_create_label to obtain its label id. "
            "Next: list_filters to verify the rule was created."
        ),
        params={
            "criteria": (
                "Dict describing which incoming messages match, e.g. "
                '{"from": "alerts@example.com", "subject": "invoice", "hasAttachment": true, '
                '"query": "older_than:1y", "size": 10485760, "sizeComparison": "larger"}. '
                "All keys optional; combine to AND-match."
            ),
            "filter_action": (
                "Dict describing what happens to matching mail, e.g. "
                '{"addLabelIds": ["Label_123"], "removeLabelIds": ["INBOX"], "forward": "someone@example.com"}. '
                "Use label ids from list_labels / get_or_create_label."
            ),
        },
    )
    async def create_filter(self, criteria: Dict[str, Any], filter_action: Dict[str, Any]) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not isinstance(criteria, dict) or not isinstance(filter_action, dict):
            return {"success": False, "response": "criteria and filter_action (both objects) are required."}
        created = await self._create_filter_raw(criteria=criteria, action=filter_action)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that filter."}
        return {"success": True, "response": f"Filter created (id: {created.get('id')}).", "filter": created}

    @tool(
        description=(
            "Apply the same label additions/removals to many messages in one call (e.g. archive all "
            "results of a search, or mark a batch as read). Get message_ids from a prior list_emails "
            "call with a broad query. Next: none required, or list_emails again to verify."
        ),
        params={
            "message_ids": "List of message ids to modify (up to ~1000). Obtain from a list_emails result.",
            "add_label_ids": "Label ids to add to all listed messages.",
            "remove_label_ids": "Label ids to remove from all listed messages (e.g. ['INBOX'] to archive all, ['UNREAD'] to mark all read).",
        },
    )
    async def batch_modify_emails(
        self, message_ids: List[str], add_label_ids: Optional[List[str]] = None, remove_label_ids: Optional[List[str]] = None
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        message_ids = message_ids or []
        add_label_ids = add_label_ids or []
        remove_label_ids = remove_label_ids or []
        if not message_ids:
            return {"success": False, "response": "No message ids provided."}
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "Provide add_label_ids and/or remove_label_ids."}
        batch_size = 50
        for i in range(0, len(message_ids), batch_size):
            chunk = message_ids[i:i + batch_size]
            ok = await self._batch_modify_messages_raw(chunk, add_label_ids=add_label_ids or None, remove_label_ids=remove_label_ids or None)
            if not ok:
                return {"success": False, "response": f"Batch modify failed for chunk at index {i}.", "processed": i}
        return {"success": True, "response": f"Labels updated for {len(message_ids)} email(s).", "count": len(message_ids)}

    @tool(
        description=(
            "PERMANENTLY delete many messages in one call — cannot be undone. Only use for explicit "
            "'permanently delete all ...' requests; otherwise prefer batch_modify_emails with "
            "remove_label_ids=['INBOX'] (archive) or add_label_ids=['TRASH'] (trash, reversible). "
            "Next: none — this is terminal."
        ),
        params={"message_ids": "List of message ids to permanently delete. Obtain from a list_emails result."},
    )
    async def batch_delete_emails(self, message_ids: List[str]) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        message_ids = message_ids or []
        if not message_ids:
            return {"success": False, "response": "No message ids provided."}
        batch_size = 50
        for i in range(0, len(message_ids), batch_size):
            chunk = message_ids[i:i + batch_size]
            ok = await self._batch_delete_messages_raw(chunk)
            if not ok:
                return {"success": False, "response": f"Batch delete failed for chunk at index {i}.", "processed": i}
        return {"success": True, "response": f"Permanently deleted {len(message_ids)} email(s).", "count": len(message_ids)}

    @tool(
        description=(
            "Create a new custom label (folder/category). Fails if a label with that exact name "
            "already exists — use get_or_create_label instead if unsure. "
            "Next: modify_labels to apply the new label (use the returned label id) to messages, or "
            "create_filter to auto-apply it to future mail."
        ),
        params={
            "name": "Label name, e.g. 'Invoices' or 'Clients/Acme' (use '/' for nested labels).",
            "message_list_visibility": "'show' (default) or 'hide' — whether messages with this label appear in the message list.",
            "label_list_visibility": "'labelShow' (default), 'labelShowIfUnread', or 'labelHide' — visibility of the label itself.",
        },
    )
    async def create_label(
        self, name: str, message_list_visibility: str = "show", label_list_visibility: str = "labelShow"
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        name = (name or "").strip()
        if not name:
            return {"success": False, "response": "Label name is required."}
        created = await self._create_label_raw(
            name=name, message_list_visibility=(message_list_visibility or "show").strip().lower(),
            label_list_visibility=(label_list_visibility or "labelShow").strip(),
        )
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that label."}
        return {"success": True, "response": f"Label '{name}' created.", "label": {"id": created.get("id"), "name": created.get("name")}}

    @tool(
        description=(
            "Look up a label by name (case-insensitive); if it doesn't exist, create it. Use this "
            "whenever the user references a label/folder by name (e.g. 'label this as Important "
            "Clients') to safely obtain its id without risking a duplicate. "
            "Next: modify_labels with the returned label id to apply it to a message."
        ),
        params={"name": "Label name to find or create, e.g. 'Important Clients'."},
    )
    async def get_or_create_label(self, name: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        name = (name or "").strip()
        if not name:
            return {"success": False, "response": "Label name is required."}
        labels = await self._list_labels_raw()
        for lb in labels or []:
            if (lb.get("name") or "").strip().lower() == name.lower():
                return {
                    "success": True, "response": f"Label '{name}' already exists.",
                    "label": {"id": lb.get("id"), "name": lb.get("name")}, "created": False,
                }
        created = await self._create_label_raw(name=name, message_list_visibility="show", label_list_visibility="labelShow")
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that label."}
        return {
            "success": True, "response": f"Label '{name}' created.",
            "label": {"id": created.get("id"), "name": created.get("name")}, "created": True,
        }

    @tool(
        description="Update an existing label's name and/or visibility. System labels (INBOX, SENT, etc.) "
                    "cannot be updated. Next: list_labels to verify the change.",
        params={
            "label_id": "The label id to update. Obtain from list_labels.",
            "name": "New label name (optional).",
            "message_list_visibility": "New 'show'/'hide' setting (optional).",
            "label_list_visibility": "New 'labelShow'/'labelShowIfUnread'/'labelHide' setting (optional).",
        },
    )
    async def update_label(
        self,
        label_id: str,
        name: Optional[str] = None,
        message_list_visibility: Optional[str] = None,
        label_list_visibility: Optional[str] = None,
    ) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not label_id:
            return {"success": False, "response": "label_id is required."}
        updated = await self._update_label_raw(
            str(label_id), name=name, message_list_visibility=message_list_visibility, label_list_visibility=label_list_visibility,
        )
        if not updated:
            return {"success": False, "response": "Could not update that label."}
        return {"success": True, "response": "Label updated.", "label": {"id": updated.get("id"), "name": updated.get("name")}}

    @tool(
        description="Permanently delete a user-created label (system labels cannot be deleted). Messages "
                    "that had this label keep their other labels. Next: list_labels to confirm removal.",
        params={"label_id": "The label id to delete. Obtain from list_labels."},
    )
    async def delete_label(self, label_id: str) -> Dict:
        guard = self._write_guard()
        if guard:
            return guard
        if not label_id:
            return {"success": False, "response": "label_id is required."}
        ok = await self._delete_label_raw(str(label_id))
        if not ok:
            return {"success": False, "response": "Could not delete that label (system labels cannot be deleted)."}
        return {"success": True, "response": "Label deleted.", "label_id": label_id}

    @tool(
        description=(
            "Fetch the raw base64url-encoded data and size of one attachment from a message. Requires "
            "both message_id and attachment_id (attachment ids are returned when reading a message "
            "that has attachments, via read_email). Next: none — the caller consumes the returned data."
        ),
        params={
            "message_id": "The message id containing the attachment. Obtain from read_email.",
            "attachment_id": "The attachment id within that message. Obtain from read_email's attachment list.",
        },
    )
    async def download_attachment(self, message_id: str, attachment_id: str) -> Dict:
        if not message_id or not attachment_id:
            return {"success": False, "response": "message_id and attachment_id are required."}
        att = await self._get_attachment_raw(str(message_id), str(attachment_id))
        if not att:
            return {"success": False, "response": "Could not fetch that attachment."}
        return {"success": True, "response": f"Attachment retrieved ({att.get('size', 0)} bytes).", "data": att.get("data"), "size": att.get("size", 0)}

    @tool(
        description=(
            "Scan a LARGE number of emails (up to 1000) matching a search/date range and extract "
            "structured data from each one (payment/invoice amounts and their currency), then "
            "aggregate it (totals per currency, counts, matching items). Use this instead of "
            "list_emails when the user asks to find/sum/count/total amounts, payments, or invoices "
            "across many messages -- e.g. 'how much did I receive in April', 'total invoices last "
            "quarter'. Extraction is pattern-based (currency symbols/codes followed by a number), so "
            "it works best for amounts stated plainly in the email body; it will not catch amounts "
            "phrased unusually or amounts only visible in an attachment. "
            "Next: read_email a specific message_id from 'items' for full detail."
        ),
        params={
            "gmail_search_query": GMAIL_SEARCH_SYNTAX_REFERENCE,
            "max_results": "Number of messages to scan, 5-1000 (default 200; use a higher value for an exhaustive scan).",
        },
    )
    async def analyze_emails(self, gmail_search_query: Optional[str] = None, max_results: int = ANALYZE_DEFAULT_MESSAGES) -> Dict:
        q = _sanitize_gmail_q(gmail_search_query or "")
        try:
            target_total = min(max(int(max_results or ANALYZE_DEFAULT_MESSAGES), 5), ANALYZE_MAX_MESSAGES)
        except (TypeError, ValueError):
            target_total = ANALYZE_DEFAULT_MESSAGES

        messages: List[Dict] = []
        next_page_token: Optional[str] = None
        while len(messages) < target_total:
            page_messages, next_page_token = await self._list_messages(
                max_results=min(MAX_RESULTS_CAP, target_total - len(messages)),
                q=q if q else None, page_token=next_page_token, fmt="full",
            )
            if not page_messages:
                break
            messages.extend(page_messages)
            if not next_page_token:
                break

        if not messages:
            return {
                "success": True, "response": "No emails found for the given criteria.",
                "scanned_count": 0, "matched_count": 0, "totals": {}, "items": [],
            }

        records = [_regex_extract_record(m) for m in messages]
        matched, totals = _aggregate_analysis_records(records)
        truncated = bool(next_page_token) and len(messages) >= target_total

        totals_str = ", ".join(f"{v:,.2f} {c}" for c, v in totals.items())
        response = (
            f"Scanned {len(messages)} email(s)"
            + (" (more match the criteria but were not scanned)" if truncated else "")
            + f". Found {len(matched)} matching item(s)"
            + (f" totaling {totals_str}." if totals else ".")
        )
        if matched:
            examples = "; ".join(
                f"[{m.get('from', '')}] {m.get('subject', '')} -> {m.get('amount')} {m.get('currency') or ''}".strip()
                for m in matched[:3]
            )
            response += f" Examples: {examples}"

        return {
            "success": True, "response": response, "scanned_count": len(messages),
            "matched_count": len(matched), "totals": totals, "items": matched[:50], "truncated": truncated,
        }

    @tool(
        description=(
            "Catch-all: interpret a single free-text Gmail instruction and carry it out end-to-end, "
            "picking whichever action (search, read, send, draft, label, trash, etc.) the instruction "
            "implies and returning its result. Use this ONLY when handing off a raw instruction instead "
            "of calling a specific Gmail tool directly -- every other tool in this toolset is more "
            "precise and should be preferred when the right action is already known. "
            "Routing here is heuristic (keyword- and id-based, not full free-form natural-language "
            "understanding): pass any ids (message_id, thread_id, draft_id) and structured fields (to, "
            "subject, body, label ids) already known as separate arguments -- they take precedence over "
            "parsing the query text and make routing reliable. If gmail_search_query/to/subject/body are "
            "omitted, this makes a best-effort attempt to extract them from the query text."
        ),
        params={
            "query": "The free-text instruction, e.g. 'archive the email from Alice about the invoice'.",
            "to": "Recipient email address(es), if this is a send/draft/compose instruction.",
            "subject": "Email subject, if known.",
            "body": "Email body text, if known.",
            "cc": "Optional CC address(es).",
            "bcc": "Optional BCC address(es).",
            "message_id": "A specific message id, if the instruction targets one email (read/reply/label/trash/...).",
            "thread_id": "A specific thread id, if the instruction targets a conversation.",
            "draft_id": "A specific draft id, if the instruction targets a saved draft.",
            "label_name": "A label name, if the instruction is about creating/finding a label.",
            "add_label_ids": "Label ids or names to add, if the instruction is about labeling.",
            "remove_label_ids": "Label ids or names to remove, if the instruction is about labeling.",
            "gmail_search_query": GMAIL_SEARCH_SYNTAX_REFERENCE + " Only used if the instruction is a search/list.",
            "max_results": "Number of results to return for list-type instructions (default 20).",
        },
    )
    async def action_by_query(
        self,
        query: str,
        to: Optional[str] = None,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        draft_id: Optional[str] = None,
        label_name: Optional[str] = None,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
        gmail_search_query: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> Dict:
        q_text = (query or "").strip()
        low = f" {q_text.lower()} "
        to_norm = _normalize_address_list(to)
        add_ids = add_label_ids or []
        remove_ids = remove_label_ids or []
        limit = max_results or DEFAULT_MAX_RESULTS

        # 1. Unambiguous send: explicit recipient + subject/body.
        if to_norm and "@" in to_norm and (subject or body):
            reply_id = message_id if (message_id and ("reply" in low or " re: " in low)) else None
            return await self.send_email(
                to=to_norm, subject=subject or "", body=body or "", cc=cc, bcc=bcc, reply_to_message_id=reply_id,
            )

        # 2. Unambiguous label create/get: a label name and no recipient.
        if label_name and not to_norm:
            if any(x in low for x in ("if it does not exist", "if not exist", "get or create", "or create")):
                return await self.get_or_create_label(label_name)
            return await self.create_label(label_name)

        # 3. Unambiguous label modification: an id plus label ids, no recipient.
        if message_id and not to_norm and (add_ids or remove_ids):
            return await self.modify_labels(message_id, add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)
        if thread_id and not to_norm and (add_ids or remove_ids):
            return await self.modify_thread_labels(thread_id, add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)

        # 4. Draft id present -> draft-scoped action.
        if draft_id:
            if any(x in low for x in ("delete", "discard", "remove")):
                return await self.delete_draft(draft_id)
            if any(x in low for x in ("update", "edit", "revise", "change")):
                return await self.update_draft(draft_id, to=to, subject=subject, body=body)
            return await self.get_draft(draft_id)

        # 5. Thread id present -> thread-scoped action.
        if thread_id:
            if "summar" in low:
                return await self.summarize_thread(thread_id)
            if add_ids or remove_ids:
                return await self.modify_thread_labels(thread_id, add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)
            return await self.get_thread(thread_id)

        # 6. Message id present -> message-scoped action.
        if message_id:
            if any(x in low for x in ("permanently delete", "delete forever", "delete permanently")):
                return await self.delete_email(message_id)
            if "untrash" in low or "restore" in low:
                return await self.untrash_email(message_id)
            if "trash" in low or (" delete " in low and "draft" not in low):
                return await self.trash_email(message_id)
            if "archive" in low:
                return await self.archive_email(message_id)
            if "mark" in low and "unread" in low:
                return await self.mark_as_unread(message_id)
            if "mark" in low and " read " in low:
                return await self.mark_as_read(message_id)
            if "summar" in low:
                return await self.summarize_email(message_id)
            if any(x in low for x in ("reply", "draft", "respond")):
                return await self.draft_reply(message_id, body=body or q_text, to=to, subject=subject)
            if "send" in low and (subject or body):
                return await self.send_email(to=to, subject=subject, body=body, cc=cc, bcc=bcc, reply_to_message_id=message_id)
            if add_ids or remove_ids:
                return await self.modify_labels(message_id, add_label_ids=add_ids or None, remove_label_ids=remove_ids or None)
            return await self.read_email(message_id)

        # 7. No ids at all -- infer intent from keywords in the free text.
        if any(x in low for x in ("list label", "list existing label", "list all label", "get labels", "show labels", "which labels")):
            return await self.list_labels()
        if "filter" in low and any(x in low for x in ("list", "show", "what")):
            return await self.list_filters()
        if "draft" in low and any(x in low for x in ("list", "show", "my drafts")) and not (to_norm and (subject or body)):
            return await self.list_drafts(max_results=limit)
        if any(x in low for x in ("thread", "conversation")) and any(x in low for x in ("list", "show", "find", "search")):
            q = _best_effort_query(gmail_search_query, q_text)
            return await self.list_threads(gmail_search_query=q, max_results=limit)
        if any(x in low for x in ("profile", "my email address", "account info", "how many emails")):
            return await self.get_profile()

        # 8. Attempt to compose a brand-new email purely from free text (e.g. "email john@x.com about ...").
        if not to_norm:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", q_text)
            if m and any(x in low for x in ("send", " email ", " mail ", "compose", "write to")):
                to_norm = m.group(0)
        if to_norm and "@" in to_norm:
            parsed_subject, parsed_body = _parse_subject_body_text(q_text)
            subj = subject or parsed_subject
            bod = body or parsed_body or q_text
            if any(x in low for x in ("draft", "prepare")) and "send" not in low:
                return await self.compose_draft(to=to_norm, subject=subj or "(No subject)", body=bod, cc=cc, bcc=bcc)
            return await self.send_email(to=to_norm, subject=subj or "(No subject)", body=bod, cc=cc, bcc=bcc)

        # 9. Default: treat the instruction as a mailbox search.
        q = _best_effort_query(gmail_search_query, q_text, {"to": to, "subject": subject, "body": body})
        return await self.list_emails(gmail_search_query=q, max_results=limit)
