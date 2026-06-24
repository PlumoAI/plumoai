"""
Gmail Agent Tool – Refactored implementation.

Read and understand emails; draft and send replies. Credentials from connected service.
Uses Gmail API REST: https://developers.google.com/workspace/gmail/api/reference/rest
Batching: https://developers.google.com/workspace/gmail/api/guides/batch

Design:
- Single intent pipeline: tool_args → provided_data → one LLM call → action + params.
- API layer: list (with pagination), get message, get thread, batch metadata, send, draft, build raw (To/CC/BCC).
- Handlers: list/read, summarize (single message or full thread), draft reply, send, schedule.
- Robust batch parsing; semantic filter validates IDs; _llm_generate_text used for all LLM text.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
import mimetypes
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

# ----- Constants (single config) -----
# Base for Gmail REST API v1 (https://developers.google.com/workspace/gmail/api/reference/rest).
# All paths below are relative to this base (userId "me" = authenticated user).
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_BATCH_URL = "https://gmail.googleapis.com/batch/gmail/v1"

# Endpoint mapping (aligned with Gmail API & Gmail-MCP-Server):
#   messages:     GET /messages (list), GET /messages/{id} (get), POST /messages/send (send),
#                 POST /messages/{id}/modify (modify), DELETE /messages/{id} (delete),
#                 POST /messages/batchModify (body: ids, addLabelIds?, removeLabelIds?),
#                 POST /messages/batchDelete (body: ids),
#                 GET /messages/{id}/attachments/{id} (attachment)
#   threads:      GET /threads/{id}
#   drafts:       GET /drafts (list), GET /drafts/{id} (get), POST /drafts (create), PUT /drafts/{id} (update), DELETE /drafts/{id} (delete)
#   labels:       GET /labels (list), POST /labels (create), GET/PUT/DELETE /labels/{id}
#   profile:      GET /profile
#   filters:      GET /settings/filters (list), GET/POST/DELETE /settings/filters, GET/DELETE /settings/filters/{id}
GMAIL_BATCH_CHUNK_SIZE = 50
DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_CAP = 50
_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
_COMPANY_URL = (os.getenv("COMPANY_URL") or _AUTH_URL).rstrip("/")

GMAIL_SEARCH_OPERATORS = (
    "in:", "to:", "from:", "subject:", "list:", "is:", "has:attachment",
    "newer_than:", "older_than:", "after:", "before:",
    "deliveredto:", "rfc822msgid:", "cc:", "bcc:", "category:",
)


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
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "content": content,
    }


# ----- Payload / header helpers -----
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
    """Normalize To/Cc/Bcc from Gmail-MCP style (array) or string to comma-separated string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(x).strip() for x in value if x)
    return ""


def _mask_for_log(s: Optional[str], visible: int = 4) -> str:
    if not s or not isinstance(s, str):
        return "<empty>"
    s = s.strip()
    if not s or len(s) <= visible * 2:
        return "***"
    return f"{s[:visible]}...{s[-visible:]}"

def _redact_secrets_for_log(value: Any) -> Any:
    """
    Best-effort redaction for logs (prevents leaking tokens / raw email bodies).
    """
    SENSITIVE_KEY_TOKENS = (
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "refresh_token",
        "authorization",
        "bearer",
        "private",
        "key",
        "client_secret",
        "service_credential",
        "credentials",
        # gmail-specific payloads that can contain lots of sensitive content
        "raw",
        "mime",
        "body",
        "htmlbody",
        "snippet",
        "payload",
    )

    def _looks_sensitive_key(k: str) -> bool:
        kl = (k or "").lower()
        return any(t in kl for t in SENSITIVE_KEY_TOKENS)

    def _mask_str(s: str) -> str:
        ss = (s or "").strip()
        if not ss:
            return ss
        if len(ss) >= 24:
            return ss[:6] + "…" + ss[-4:]
        return "***"

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _looks_sensitive_key(k):
                out[k] = _mask_str(str(v)) if v is not None else None
            else:
                out[k] = _redact_secrets_for_log(v)
        return out
    if isinstance(value, list):
        return [_redact_secrets_for_log(v) for v in value[:50]]
    if isinstance(value, str):
        # Avoid dumping whole email threads/bodies into logs
        if len(value) > 1200:
            return value[:600] + "…<truncated>…" + value[-120:]
        # Mask long strings that often are tokens/ids
        if len(value.strip()) >= 48:
            return _mask_str(value)
        return value
    return value


def _validate_gmail_search(q: Optional[str]) -> bool:
    if not q or not isinstance(q, str):
        return False
    q = q.strip()
    if not q:
        return False
    if any(q.startswith(op) or f" {op}" in q for op in GMAIL_SEARCH_OPERATORS):
        return True
    return len(q) <= 200


def _sanitize_gmail_q(q: str) -> str:
    if not q or not isinstance(q, str):
        return ""
    return q.strip().replace("\n", " ").strip()[:200]


def _normalize_gmail_search_fallback(query: str) -> str:
    return _sanitize_gmail_q(query or "")


def _extract_email_from_header(header_value: str) -> str:
    """Extract email address from 'Name <email@domain.com>' or return as-is if no angle brackets."""
    if not header_value or "<" not in header_value or ">" not in header_value:
        return (header_value or "").strip()
    match = re.search(r"<([^>]+)>", header_value)
    return (match.group(1).strip() if match else header_value.strip()) or header_value.strip()


# ----- Batch response parsing (robust) -----
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
    rest = ct[idx + 9 :].strip().strip('"').strip("'")
    boundary = rest.split(";")[0].split()[0].strip().strip('"').strip("'")
    if not boundary:
        return results
    sep = b"--" + boundary.encode("utf-8", errors="replace")
    raw = content
    start = raw.find(sep)
    if start == -1:
        return results
    current = raw[start + len(sep) :].lstrip(b"\r\n")
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
            current = current[next_sep + len(sep) :].lstrip(b"\r\n")
        part_str = part_body.decode("utf-8", errors="replace")
        json_start = part_str.find("\r\n\r\n")
        if json_start == -1:
            json_start = part_str.find("\n\n")
        if json_start >= 0:
            json_str = part_str[json_start + 4 :].lstrip("\r\n")
            try:
                obj = json.loads(json_str)
                if obj.get("id"):
                    results.append(obj)
            except json.JSONDecodeError:
                pass
    return results


# =============================================================================
# GmailAgentTool
# =============================================================================


class GmailAgentTool(ConnectedServiceToolAgent):
    """
    Gmail app agent. Capabilities:
    - Read & understand: list (with pagination), read, summarize message or full thread.
    - Draft & send: draft reply, send (To/CC/BCC), schedule (draft + send_at).
    Credentials from connected service (connected_service_id).
    """

    TOOL_NAME = "Gmail"
    TOOL_DESCRIPTION = """Gmail AI Agent: read, compose, send, draft, and manage Gmail.

USE WHEN: user mentions email, inbox, Gmail, draft, compose, send, reply, search, labels, filters, attachments, or profile.

ACTIONS: list (search emails), read (by message_id), summarize (message or thread), send (to+subject+body), draft (reply draft), compose_draft (new draft), schedule (draft+send_at), list_drafts, get_draft, update_draft, delete_draft, list_labels, modify_labels, create_label, get_or_create_label, update_label, delete_label, get_profile, list_filters, get_filter, create_filter, delete_filter, delete_email, batch_modify_emails, batch_delete_emails, download_attachment."""

    # Short action descriptions for LLM prompt (when choosing action)
    ACTION_DESCRIPTIONS = (
        "list=search/list emails by query; read=read one email by id; summarize=summarize message or thread; "
        "send=send email (to,subject,body); draft=reply draft to message; compose_draft=new draft (to,subject,body); "
        "schedule=draft+send_at; list_drafts/get_draft/update_draft/delete_draft=drafts; "
        "list_labels/modify_labels/create_label/get_or_create_label/update_label/delete_label=labels; "
        "get_profile=profile; list_filters/get_filter/create_filter/delete_filter=filters; "
        "delete_email=delete one; batch_modify_emails/batch_delete_emails=batch; download_attachment=get attachment data"
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
        super().__init__(token=token, company_id=company_id, user_id=user_id, app_config=app_config)
        self._httpx_client: Optional[httpx.AsyncClient] = None
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
        self._permissions = (active_config.get("permissions") or "full") if isinstance(active_config, dict) else "full"
        if isinstance(self._permissions, str):
            self._permissions = self._permissions.strip().lower()
        if self._permissions not in ("full", "read-only", "readonly"):
            self._permissions = "full"
        if self._permissions == "readonly":
            self._permissions = "read-only"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def _llm_generate_text(self, prompt: str, max_tokens: int = 500) -> Optional[str]:
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
                return out.strip() if out else None
            if isinstance(gen, str):
                return gen.strip() or None
        except Exception as e:
            logger.debug("Gmail LLM generate failed: %s", e)
        return None

    # ----- Gmail API layer -----
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

    async def _gmail_batch_get_messages(self, message_ids: List[str]) -> List[Dict]:
        if not message_ids or not self._httpx_client:
            return []
        boundary = "batch_gmail_" + str(uuid.uuid4()).replace("-", "")
        results: List[Dict] = []
        for chunk_start in range(0, len(message_ids), GMAIL_BATCH_CHUNK_SIZE):
            chunk = message_ids[chunk_start : chunk_start + GMAIL_BATCH_CHUNK_SIZE]
            parts = [
                f"Content-Type: application/http\r\n\r\nGET /gmail/v1/users/me/messages/{mid}?format=metadata HTTP/1.1\r\n"
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
                return await self._gmail_batch_get_messages(message_ids)
            if r.status_code >= 400:
                logger.warning("Gmail batch API %s: %s", r.status_code, (r.text or "")[:500])
                break
            ct = r.headers.get("content-type", "")
            part_results = _parse_batch_response(r.content, ct)
            results.extend(part_results)
        return results

    async def _list_messages(
        self,
        max_results: int = DEFAULT_MAX_RESULTS,
        q: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        page_token: Optional[str] = None,
        include_spam_trash: bool = False,
    ) -> Tuple[List[Dict], Optional[str]]:
        """Returns (ordered messages with metadata, next_page_token)."""
        params = {"maxResults": min(max_results, MAX_RESULTS_CAP)}
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = label_ids
        if page_token:
            params["pageToken"] = page_token
        if include_spam_trash:
            params["includeSpamTrash"] = "true"
        logger.info("Gmail list_messages: params=%s", {k: v for k, v in params.items()})
        data = await self._gmail_request("GET", "/messages", params=params)
        if not data or "messages" not in data:
            return [], None
        msg_list = data.get("messages", [])[:max_results]
        message_ids = [m.get("id") for m in msg_list if m.get("id")]
        if not message_ids:
            return [], data.get("nextPageToken")
        messages = await self._gmail_batch_get_messages(message_ids)
        id_to_msg = {m.get("id"): m for m in messages if m.get("id")}
        ordered = [id_to_msg[mid] for mid in message_ids if mid in id_to_msg]
        return ordered, data.get("nextPageToken")

    async def _get_message(self, message_id: str, format: str = "full") -> Optional[Dict]:
        return await self._gmail_request("GET", f"/messages/{message_id}", params={"format": format})

    async def _get_thread(self, thread_id: str) -> Optional[Dict]:
        """Fetch full thread (message ids; payloads fetched separately)."""
        return await self._gmail_request("GET", f"/threads/{thread_id}", params={"format": "full"})

    async def _send_message(self, raw: str, thread_id: Optional[str] = None) -> Optional[Dict]:
        sent, _ = await self._send_message_with_status(raw, thread_id=thread_id)
        return sent

    async def _send_message_with_status(
        self,
        raw: str,
        thread_id: Optional[str] = None,
        retry_401: bool = True,
    ) -> Tuple[Optional[Dict], Optional[int]]:
        body = {"raw": raw}
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
        # Prefer explicit values from credentials payload; avoid extra API call for low latency.
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
        """
        Create signed tracking URL for email open pixel.
        Requires activity/account ids so callback can mark activity as opened.
        """
        if activity_id is None or project_fid is None or connected_account_fid is None or not self.company_id:
            return None
        base = self._resolve_tracking_base_url()
        secret = self._resolve_tracking_secret()
        if not base or not secret:
            return None
        ts = int(datetime.now(timezone.utc).timestamp())
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
        self,
        *,
        body_text: str,
        mime_type: str,
        html_body: Optional[str],
        tracking_url: Optional[str],
    ) -> Tuple[str, str, Optional[str]]:
        """
        Ensure outgoing email includes tracking pixel in HTML part.
        Returns (mime_type, body_text, html_body).
        """
        if not tracking_url:
            return mime_type, body_text, html_body

        pixel = (
            f'<img src="{tracking_url}" width="1" height="1" style="display:none;max-height:1px;max-width:1px;" alt="" />'
        )
        mt = (mime_type or "text/plain").strip().lower()
        plain = body_text or ""

        if html_body and str(html_body).strip():
            html_text = str(html_body).strip()
        elif mt == "text/html":
            html_text = plain
        else:
            escaped = html.escape(plain).replace("\n", "<br>")
            html_text = f"<div>{escaped}</div>"

        if re.search(r"</body\s*>", html_text, flags=re.IGNORECASE):
            html_text = re.sub(r"</body\s*>", pixel + "</body>", html_text, count=1, flags=re.IGNORECASE)
        else:
            html_text = html_text + pixel

        if mt == "text/html":
            return "text/html", html_text, None
        return "multipart/alternative", plain, html_text

    async def _log_email_activity_before_send(
        self,
        *,
        to_address: str,
        subject: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        if not self.token or not self.company_id or not _COMPANY_URL:
            return None

        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(
            self.app_config.get("project_fid")
            or self.app_config.get("projectFid")
            or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            logger.warning(
                "Skipping email activity log: missing project_fid/connected_account_fid (project_fid=%s connected_account_fid=%s)",
                project_fid,
                connected_account_fid,
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
            logger.info("Email activity logged before send: to=%s subject=%s activity_id=%s", to_address, subject, activity_id)
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
            self.app_config.get("project_fid")
            or self.app_config.get("projectFid")
            or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            logger.warning(
                "Skipping email activity post-send log: missing project_fid/connected_account_fid (project_fid=%s connected_account_fid=%s)",
                project_fid,
                connected_account_fid,
            )
            return None

        post_metadata = {
            **(metadata or {}),
            "send_result": {
                "status_code": gmail_status_code,
                "external_message_id": external_message_id,
            },
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
            "sent_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
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
            logged_activity_id = ((data.get("data") or {}).get("agent_activity_id")) if isinstance(data, dict) else None
            logger.info(
                "Email activity logged after send: to=%s subject=%s activity_id=%s external_message_id=%s gmail_status_code=%s",
                to_address,
                subject,
                logged_activity_id,
                external_message_id,
                gmail_status_code,
            )
            return self._to_int_or_none(logged_activity_id)
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
        """Log email opened/read activity with opened_at timestamp."""
        if not self.token or not self.company_id or not _COMPANY_URL:
            return None

        connected_account_fid = self._to_int_or_none(
            self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
        )
        project_fid = self._to_int_or_none(
            self.app_config.get("project_fid")
            or self.app_config.get("projectFid")
            or self.agent_id
        )
        if connected_account_fid is None or project_fid is None:
            logger.warning(
                "Skipping email activity opened log: missing project_fid/connected_account_fid (project_fid=%s connected_account_fid=%s)",
                project_fid,
                connected_account_fid,
            )
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
            "opened_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "metadata": {
                **(metadata or {}),
                "open_result": {
                    "source": "gmail_tool",
                    "opened_via": "read",
                },
            },
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
                logger.warning("Email activity opened log failed: status=%s body=%s", resp.status_code, resp.text[:500])
                return None
            data = resp.json()
            logged_activity_id = ((data.get("data") or {}).get("agent_activity_id")) if isinstance(data, dict) else None
            logger.info(
                "Email activity logged as opened: message_id=%s subject=%s activity_id=%s",
                external_message_id,
                subject,
                logged_activity_id,
            )
            return self._to_int_or_none(logged_activity_id)
        except Exception as e:
            logger.warning("Email activity opened log error: %s", e)
            return None

    async def _create_draft(self, raw: str) -> Optional[Dict]:
        return await self._gmail_request("POST", "/drafts", json_body={"message": {"raw": raw}})

    async def _list_drafts(
        self,
        max_results: int = 20,
        page_token: Optional[str] = None,
        q: Optional[str] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        """Returns (list of draft items with id/message), next_page_token."""
        params = {"maxResults": min(max_results, MAX_RESULTS_CAP)}
        if page_token:
            params["pageToken"] = page_token
        if q:
            params["q"] = q
        data = await self._gmail_request("GET", "/drafts", params=params)
        if not data or "drafts" not in data:
            return [], data.get("nextPageToken") if data else None
        return data.get("drafts", []), data.get("nextPageToken")

    async def _get_draft(self, draft_id: str) -> Optional[Dict]:
        return await self._gmail_request("GET", f"/drafts/{draft_id}")

    async def _update_draft(self, draft_id: str, raw: str, thread_id: Optional[str] = None) -> Optional[Dict]:
        body = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        return await self._gmail_request("PUT", f"/drafts/{draft_id}", json_body=body)

    async def _delete_draft(self, draft_id: str) -> bool:
        r = await self._gmail_request("DELETE", f"/drafts/{draft_id}")
        return r is not None  # 204 returns {}

    async def _list_labels(self) -> List[Dict]:
        data = await self._gmail_request("GET", "/labels")
        if not data or "labels" not in data:
            return []
        return data.get("labels", [])

    async def _modify_message_labels(
        self,
        message_id: str,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Optional[Dict]:
        body = {}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        if not body:
            return await self._get_message(message_id, format="metadata")
        return await self._gmail_request("POST", f"/messages/{message_id}/modify", json_body=body)

    async def _get_profile(self) -> Optional[Dict]:
        return await self._gmail_request("GET", "/profile")

    async def _list_filters(self) -> List[Dict]:
        """List Gmail settings filters (forwarding rules)."""
        data = await self._gmail_request("GET", "/settings/filters")
        if not data or "filter" not in data:
            return []
        return data.get("filter", [])

    async def _delete_message(self, message_id: str) -> bool:
        """Permanently delete a message. Gmail API: messages.delete."""
        r = await self._gmail_request("DELETE", f"/messages/{message_id}")
        return r is not None

    async def _batch_modify_messages(
        self,
        message_ids: List[str],
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> bool:
        """Batch add/remove labels. Gmail API: messages.batchModify."""
        body = {"ids": message_ids}
        if add_label_ids:
            body["addLabelIds"] = add_label_ids
        if remove_label_ids:
            body["removeLabelIds"] = remove_label_ids
        r = await self._gmail_request("POST", "/messages/batchModify", json_body=body)
        return r is not None

    async def _batch_delete_messages(self, message_ids: List[str]) -> bool:
        """Permanently delete multiple messages. Gmail API: messages.batchDelete."""
        if not message_ids:
            return True
        r = await self._gmail_request("POST", "/messages/batchDelete", json_body={"ids": message_ids})
        return r is not None

    async def _create_label(
        self,
        name: str,
        message_list_visibility: str = "show",
        label_list_visibility: str = "labelShow",
    ) -> Optional[Dict]:
        """Create a user label. Gmail API: labels.create."""
        body = {"name": name, "messageListVisibility": message_list_visibility, "labelListVisibility": label_list_visibility}
        return await self._gmail_request("POST", "/labels", json_body=body)

    async def _update_label(
        self,
        label_id: str,
        name: Optional[str] = None,
        message_list_visibility: Optional[str] = None,
        label_list_visibility: Optional[str] = None,
    ) -> Optional[Dict]:
        """Update a label. Gmail API: labels.update."""
        body = {}
        if name is not None:
            body["name"] = name
        if message_list_visibility is not None:
            body["messageListVisibility"] = message_list_visibility
        if label_list_visibility is not None:
            body["labelListVisibility"] = label_list_visibility
        if not body:
            return await self._gmail_request("GET", f"/labels/{label_id}")
        return await self._gmail_request("PUT", f"/labels/{label_id}", json_body=body)

    async def _delete_label(self, label_id: str) -> bool:
        """Delete a user label (system labels cannot be deleted). Gmail API: labels.delete."""
        r = await self._gmail_request("DELETE", f"/labels/{label_id}")
        return r is not None

    async def _get_filter(self, filter_id: str) -> Optional[Dict]:
        """Get a single filter. Gmail API: settings.filters.get."""
        return await self._gmail_request("GET", f"/settings/filters/{filter_id}")

    async def _delete_filter(self, filter_id: str) -> bool:
        """Delete a filter. Gmail API: settings.filters.delete."""
        r = await self._gmail_request("DELETE", f"/settings/filters/{filter_id}")
        return r is not None

    async def _create_filter(self, criteria: Dict, action: Dict) -> Optional[Dict]:
        """Create a filter. Gmail API: settings.filters.create. criteria: from, to, subject, query, etc.; action: addLabelIds, removeLabelIds, forward."""
        body = {"criteria": criteria, "action": action}
        return await self._gmail_request("POST", "/settings/filters", json_body=body)

    async def _get_attachment(self, message_id: str, attachment_id: str) -> Optional[Dict]:
        """Get attachment metadata and data. Gmail API: messages.attachments.get. Returns { data (base64url), size }."""
        return await self._gmail_request("GET", f"/messages/{message_id}/attachments/{attachment_id}")

    def _extract_attachment_list(self, msg: Dict) -> List[Dict]:
        """From a full message payload, extract list of attachments: filename, mimeType, size, attachmentId (for download)."""
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
        thread_id: Optional[str] = None,
        references: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
        mime_type: str = "text/plain",
        html_body: Optional[str] = None,
    ) -> str:
        to = _normalize_address_list(to)
        cc = _normalize_address_list(cc) if cc else None
        bcc = _normalize_address_list(bcc) if bcc else None
        use_attachments = bool(attachments and isinstance(attachments, list))
        use_multipart_alt = (mime_type or "").lower() == "multipart/alternative" and bool(html_body and html_body.strip())
        use_html_only = (mime_type or "").lower() == "text/html"

        if use_attachments:
            msg = MIMEMultipart("mixed")
        elif use_multipart_alt:
            msg = MIMEMultipart("alternative")
        else:
            msg = MIMEMultipart("alternative")

        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        if use_html_only:
            msg.attach(MIMEText(body_text or "", "html", "utf-8"))
        elif use_multipart_alt:
            msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
            msg.attach(MIMEText(html_body.strip(), "html", "utf-8"))
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
                mime_type = (att.get("mimeType") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
                main_type, sub_type = mime_type.split("/", 1) if "/" in mime_type else ("application", "octet-stream")
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

    # ----- Intent: single pipeline -----
    async def _build_gmail_search_query(self, user_query: str) -> str:
        if not (user_query or "").strip():
            return ""
        prompt = """You are a Gmail search expert. Convert the user request into exactly one Gmail search query.

Output rules:
- Use ONLY valid Gmail operators: in: (in:sent, in:inbox), to:, from:, subject:, cc:, bcc:, newer_than:Xd, older_than:Xd, has:attachment, is:read, is:unread, after:, before:.
- Emails I sent to someone → in:sent to:name (e.g. in:sent to:kavita).
- Time: "last week" → newer_than:7d; "last month" → newer_than:30d.
- Output ONLY the search string, one line, no explanation, no quotes. Do not return empty; use in:inbox when vague.

User request: """
        prompt += (user_query or "").strip()[:500]
        out = await self._llm_generate_text(prompt, max_tokens=150)
        if not out:
            return _normalize_gmail_search_fallback(user_query)
        out = out.strip().strip('"').strip("'").strip("`").replace("\n", " ").strip()
        if not out or not _validate_gmail_search(out):
            return _normalize_gmail_search_fallback(user_query)
        return _sanitize_gmail_q(out)

    async def _extract_send_params_with_llm(self, user_query: str, step_action: Optional[str] = None) -> Optional[Dict]:
        context = ("Step/context: " + (step_action or "")[:300] + "\n\n") if step_action else ""
        prompt = f"""Extract recipient email, subject, and body from this request into JSON only.
Output exactly one JSON object with keys: "to", "subject", "body". Use empty string for missing. No markdown.
{context}Request:
{(user_query or "").strip()[:600]}

JSON:"""
        out = await self._llm_generate_text(prompt, max_tokens=350)
        if not out:
            return None
        out = out.strip().strip("`").strip()
        for prefix in ("json", "```"):
            if out.lower().startswith(prefix):
                out = out[len(prefix) :].lstrip()
        if out.endswith("```"):
            out = out[:-3].strip()
        try:
            data = json.loads(out)
            to = (data.get("to") or "").strip()
            subject = (data.get("subject") or "").strip()
            body = (data.get("body") or data.get("content") or "").strip()
            if to and "@" in to and (subject or body):
                return {"to": to, "subject": subject, "body": body}
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _draft_summary_from_provided_data(self, provided_data: Any) -> str:
        """Build a short 'draft from previous step' summary for the LLM so it can output send with subject/body."""
        if not provided_data or not isinstance(provided_data, list):
            return ""
        for item in provided_data[:3]:
            if not isinstance(item, dict):
                continue
            res = item.get("result") if isinstance(item.get("result"), dict) else item
            subj = (res.get("subject") or item.get("subject") or "").strip()
            body = (res.get("body") or res.get("content") or item.get("body") or item.get("content") or "").strip()
            if subj or body:
                body_preview = (body or "")[:300].replace("\n", " ")
                return f"Draft from previous step: subject={subj or '(none)'}, body={body_preview}..."
        return ""

    async def _decide_action_with_llm(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        step_action = None
        if tool_args and isinstance(tool_args, dict) and tool_args.get("step_action"):
            step_action = str(tool_args.get("step_action"))[:300]
        context_parts = []
        draft_summary = self._draft_summary_from_provided_data(provided_data)
        if draft_summary:
            context_parts.append(draft_summary)
        if provided_data and isinstance(provided_data, list):
            for item in provided_data[:3]:
                if isinstance(item, dict):
                    context_parts.append(
                        json.dumps({k: v for k, v in item.items() if k in ("message_id", "thread_id", "to", "subject", "body", "content")})
                    )
                    # Also include result.subject, result.body if present
                    res = item.get("result")
                    if isinstance(res, dict) and (res.get("subject") or res.get("body")):
                        context_parts.append(json.dumps({"subject": res.get("subject"), "body": (res.get("body") or res.get("content") or "")[:500]}))
        if step_action:
            context_parts.append("Step/context: " + step_action)
        context = " | ".join(context_parts) if context_parts else ""

        prompt = f"""You are a Gmail assistant. Output exactly one JSON object. No markdown, no explanation.

ACTION DESCRIPTIONS (pick the one that matches the user request):
{self.ACTION_DESCRIPTIONS}

STRUCTURED OUTPUT RULES:
1. LABELS vs EMAILS: Use "list_labels" when the user wants to list Gmail LABELS (categories/folders). Use "list" only when listing or searching EMAILS/messages (e.g. "list emails", "search inbox", "from:mercury"). Never use "list" with a query like "List labels" or "list existing labels" — use "list_labels" instead.
2. GET_OR_CREATE vs CREATE: Use "get_or_create_label" when the user says "get or create label X", "create label X if it does not exist", or "create label X if not exist". Use "create_label" only when explicitly creating a new label without checking existence.
3. SEND: If the step context or user request says to SEND the email/draft to someone, set "action" to "send". Extract "to" from the text. Use "subject" and "body" from context if present.
4. Do NOT set action to "list" when the user wants to send an email or list labels. Do NOT use "List labels" or "list existing labels" as a gmail_search_query — use action "list_labels" with no query.
5. For list/read EMAILS use "gmail_search_query" or "query" (e.g. in:inbox, from:john). For send or list_labels leave gmail_search_query empty.
6. Use "compose_draft" for a new email draft (not a reply). Use "draft" when replying to an existing message.

JSON keys (use exactly):
- "action": one of send, list, read, summarize, summarize_thread, draft, compose_draft, schedule, list_drafts, get_draft, update_draft, delete_draft, list_labels, modify_labels, get_profile, list_filters, delete_email, batch_modify_emails, batch_delete_emails, create_label, get_or_create_label, update_label, delete_label, get_filter, delete_filter, create_filter, download_attachment
- "to": recipient email (for send)
- "subject": string (for send; use from draft in context if step says send the draft)
- "body": string (for send; use from draft in context if step says send the draft)
- "gmail_search_query": only for list/read; empty for send
- "max_results": 5-50 for list
- "message_id", "thread_id", "draft_id", "to" (string or array), "cc", "bcc", "query" or "gmail_search_query" (for search), "maxResults", "add_label_ids", "remove_label_ids", "message_ids", "filter_id", "label_id", "attachment_id", "criteria", "filter_action", "name", "mimeType" (text/plain|text/html|multipart/alternative), "htmlBody" (for multipart) as needed

Context:
{context}

User request:
{(user_query or "").strip()[:800]}

JSON:"""
        out = await self._llm_generate_text(prompt, max_tokens=450)
        if not out:
            return None
        out = out.strip()
        for prefix in ("```json", "```"):
            if out.startswith(prefix):
                out = out[len(prefix) :].strip()
            if out.endswith("```"):
                out = out[:-3].strip()
        try:
            data = json.loads(out)
            action = (data.get("action") or "list").lower()
            valid_actions = (
                "list", "read", "summarize", "summarize_thread", "draft", "compose_draft", "send", "schedule",
                "list_drafts", "get_draft", "update_draft", "delete_draft",
                "list_labels", "modify_labels", "get_profile", "list_filters",
                "delete_email", "batch_modify_emails", "batch_delete_emails",
                "create_label", "get_or_create_label", "update_label", "delete_label",
                "get_filter", "delete_filter", "create_filter", "download_attachment",
            )
            if action not in valid_actions:
                action = "list"
            params = {}
            if action == "send" and (data.get("to") or data.get("subject") or data.get("body")):
                to = _normalize_address_list(data.get("to"))
                subject = (data.get("subject") or data.get("email_subject") or "").strip()
                body = (data.get("body") or data.get("content") or data.get("email_body") or "").strip()
                if self._is_placeholder_content(subject):
                    subject = ""
                if self._is_placeholder_content(body):
                    body = ""
                if self._step_action_requires_previous_draft(step_action or "", user_query):
                    draft_subj, draft_body = self._draft_from_provided_data(provided_data)
                    if draft_subj or draft_body:
                        subject = draft_subj or subject or "(No subject)"
                        body = draft_body or body
                if (to and "@" in to) and (not subject or not body) and provided_data:
                    draft_subj, draft_body = self._draft_from_provided_data(provided_data)
                    subject = subject or draft_subj or "(No subject)"
                    body = body or draft_body
                if to and "@" in to and (subject or body):
                    params = {"to": to, "subject": subject or "(No subject)", "body": body or ""}
                    if data.get("cc"):
                        params["cc"] = _normalize_address_list(data.get("cc"))
                    if data.get("bcc"):
                        params["bcc"] = _normalize_address_list(data.get("bcc"))
            if action == "compose_draft" and not params and data.get("to"):
                to = _normalize_address_list(data.get("to"))
                if to and "@" in to:
                    params = {
                        "to": to,
                        "subject": (data.get("subject") or "").strip() or "(No subject)",
                        "body": (data.get("body") or data.get("content") or "").strip(),
                    }
                    if data.get("cc"):
                        params["cc"] = _normalize_address_list(data.get("cc"))
                    if data.get("bcc"):
                        params["bcc"] = _normalize_address_list(data.get("bcc"))
            if not params:
                send_params = await self._extract_send_params_with_llm(user_query, step_action=step_action)
                if send_params:
                    action = "send"
                    params = send_params
            if not params:
                q = (data.get("gmail_search_query") or data.get("query") or "").strip()  # MCP uses "query"
                if q and not _validate_gmail_search(q):
                    q = ""
                try:
                    max_results = max(5, min(MAX_RESULTS_CAP, int(data.get("max_results") or data.get("maxResults") or DEFAULT_MAX_RESULTS)))
                except (TypeError, ValueError):
                    max_results = DEFAULT_MAX_RESULTS
                params = {"max_results": max_results}
                if q:
                    params["q"] = params["query"] = q
            if data.get("message_id"):
                params["message_id"] = str(data["message_id"])
            if data.get("thread_id"):
                params["thread_id"] = str(data["thread_id"])
            if data.get("label_ids") and isinstance(data["label_ids"], list):
                params["label_ids"] = [str(x) for x in data["label_ids"][:10]]
            if data.get("draft_id"):
                params["draft_id"] = str(data["draft_id"])
            if data.get("add_label_ids") and isinstance(data["add_label_ids"], list):
                params["add_label_ids"] = [str(x) for x in data["add_label_ids"][:20]]
            if data.get("remove_label_ids") and isinstance(data["remove_label_ids"], list):
                params["remove_label_ids"] = [str(x) for x in data["remove_label_ids"][:20]]
            if data.get("message_ids") and isinstance(data["message_ids"], list):
                params["message_ids"] = [str(x) for x in data["message_ids"][:100]]
            if data.get("filter_id"):
                params["filter_id"] = str(data["filter_id"])
            if data.get("label_id"):
                params["label_id"] = str(data["label_id"])
            if data.get("attachment_id"):
                params["attachment_id"] = str(data["attachment_id"])
            if data.get("criteria") and isinstance(data["criteria"], dict):
                params["criteria"] = data["criteria"]
            if data.get("filter_action") and isinstance(data["filter_action"], dict):
                params["action"] = data["filter_action"]
            if data.get("name") is not None:
                params["name"] = str(data["name"]).strip()
            if data.get("message_list_visibility") is not None:
                params["message_list_visibility"] = str(data["message_list_visibility"])
            if data.get("label_list_visibility") is not None:
                params["label_list_visibility"] = str(data["label_list_visibility"])
            if data.get("mimeType") is not None:
                params["mimeType"] = str(data["mimeType"]).strip()
            if data.get("htmlBody") is not None:
                params["htmlBody"] = str(data["htmlBody"])
            return {"action": action, "params": params}
        except (json.JSONDecodeError, TypeError):
            send_params = await self._extract_send_params_with_llm(user_query, step_action=step_action)
            if send_params:
                return {"action": "send", "params": send_params}
        return None

    def _parse_response_as_subject_body(self, text: str) -> Tuple[str, str]:
        """Parse 'Subject: X\\n\\nBody' or similar formats. Returns (subject, body). Schema-agnostic."""
        if not text or not isinstance(text, str):
            return ("", "")
        t = text.strip()
        subj, body = "", ""
        # Common pattern: "Subject: ...\n\nBody content"
        m = re.match(r"^(?:subject\s*:\s*)(.+?)(?:\n{2,}|\n\n)(.+)$", t, re.DOTALL | re.IGNORECASE)
        if m:
            subj = (m.group(1) or "").strip()
            body = (m.group(2) or "").strip()
        else:
            # Fallback: first line as subject, rest as body
            lines = t.split("\n")
            if lines:
                first = lines[0].strip()
                if first.lower().startswith("subject"):
                    subj = first.split(":", 1)[-1].strip() if ":" in first else first
                    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
                else:
                    body = t
        return (subj or "", body or "")

    def _looks_like_email_content(self, val: str) -> bool:
        """
        True if val exhibits structural traits of email/body content (not a short status line).
        Dynamic: uses length and structure only — no hardcoded phrases. Any step can return any message.
        """
        if not val or not isinstance(val, str):
            return False
        v = val.strip()
        if len(v) < 20:
            return False
        # Multi-paragraph or multi-line suggests substantive content
        if "\n\n" in v or v.count("\n") >= 2:
            return True
        # Substantial single block
        if len(v) > 120:
            return True
        # Medium length with internal structure (e.g. colons, commas)
        if len(v) > 60 and (":" in v or "," in v):
            return True
        return False

    def _extract_draft_from_dict(self, d: Dict[str, Any], depth: int = 0, max_depth: int = 4) -> Tuple[str, str]:
        """Recursively extract (subject, body) from a dict. Schema-agnostic; no hardcoded keys."""
        if depth >= max_depth or not isinstance(d, dict):
            return ("", "")
        subj = ""
        body = ""
        subj_keys = ("subject", "email_subject", "title", "header")
        body_keys = ("body", "email_body", "content", "text", "html_body")
        for k, v in d.items():
            if not isinstance(k, str) or v is None:
                continue
            k_lower = k.lower()
            if isinstance(v, str):
                v = v.strip()
                if k_lower in subj_keys and v and len(v) < 500:
                    subj = subj or v
                elif k_lower == "response" and v:
                    if "subject" in v.lower() and "\n" in v:
                        s, b = self._parse_response_as_subject_body(v)
                        subj = subj or s
                        body = body or b
                    elif len(v) > 30:
                        body = body or v
                elif k_lower in body_keys and v and len(v) > 10:
                    body = body or v
                elif k_lower == "message" and v and self._looks_like_email_content(v):
                    body = body or v
            elif isinstance(v, dict):
                s, b = self._extract_draft_from_dict(v, depth + 1, max_depth)
                subj = subj or s
                body = body or b
        return (subj or "", body or "")

    def _score_email_candidate(self, subj: str, body: str) -> int:
        """Higher = better email candidate. Prefer explicit subject+body structure."""
        if not body or len(body) < 20:
            return 0
        score = 10
        if subj and len(subj) > 2:
            score += 50
        if "subject" in body.lower()[:80] and "\n" in body:
            score += 30
        if any(x in body.lower() for x in ("dear ", "hello ", "hi ", "regards", "best regards")):
            score += 20
        if len(body) > 100:
            score += 10
        return score

    def _draft_from_provided_data(self, provided_data: Any) -> tuple:
        """Get (subject, body) from provided_data. Schema-agnostic; prefers email-like content from later steps."""
        if not provided_data or not isinstance(provided_data, list):
            return ("", "")
        best = ("", "", 0)
        for item in reversed(provided_data):
            if not isinstance(item, dict):
                continue
            subj, body = self._extract_draft_from_dict(item)
            if not subj and not body:
                continue
            score = self._score_email_candidate(subj or "", body or "")
            if score > best[2]:
                best = (subj or "", body or "", score)
        if best[2] > 0:
            return (best[0] or "(No subject)", best[1] or "")
        return ("", "")

    def _is_placeholder_content(self, val: str) -> bool:
        """True if val looks like a placeholder (e.g. '[Content from step 4]'), not real content."""
        if not val or not isinstance(val, str):
            return True
        v = val.strip().lower()
        if len(v) < 20 and any(x in v for x in ("[content", "from step", "step ", "placeholder", "see step")):
            return True
        if re.match(r"^\[.*step\s*\d+\s*\]$", v) or re.match(r"^\[content\s+from\s+step", v):
            return True
        return False

    def _validate_email_for_send(
        self, to: str, subject: str, body: str, is_reply: bool = False
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Validate email before send. Returns (valid, issue_dict).
        If invalid, issue_dict is the execution_issue payload for the runner.
        """
        to_str = (to or "").strip()
        if not to_str or "@" not in to_str:
            return False, {
                "success": False,
                "execution_issue": True,
                "issue": {
                    "code": "EMAIL_MISSING_RECIPIENT",
                    "message": "Email has no valid recipient (to).",
                    "suggested_fix": "discovery",
                    "fix_hint": "Obtain recipient email from context or prior step.",
                },
                "response": "Email has no valid recipient.",
            }
        subj = (subject or "").strip()
        if not subj or self._is_placeholder_content(subj):
            return False, {
                "success": False,
                "execution_issue": True,
                "issue": {
                    "code": "EMAIL_SUBJECT_INVALID",
                    "message": "Email subject is empty or placeholder.",
                    "suggested_fix": "resolve_from_step",
                    "tool_name": "AI Writer",
                    "fix_hint": "Subject should come from the AI Writer (or content-generating) step. Re-run that step or ensure its output is passed to Gmail.",
                },
                "response": "Email subject is invalid or placeholder.",
            }
        body_str = (body or "").strip()
        min_body_len = 15 if is_reply else 30
        if not body_str or self._is_placeholder_content(body_str):
            return False, {
                "success": False,
                "execution_issue": True,
                "issue": {
                    "code": "EMAIL_BODY_PLACEHOLDER",
                    "message": "Email body is empty or placeholder.",
                    "suggested_fix": "resolve_from_step",
                    "tool_name": "AI Writer",
                    "fix_hint": "Body should come from the AI Writer step. Re-run AI Writer to generate full email content, then retry send.",
                },
                "response": "Email body is placeholder or empty.",
            }
        if len(body_str) < min_body_len:
            return False, {
                "success": False,
                "execution_issue": True,
                "issue": {
                    "code": "EMAIL_BODY_TRUNCATED",
                    "message": f"Email body is too short ({len(body_str)} chars); likely truncated.",
                    "suggested_fix": "resolve_from_step",
                    "tool_name": "AI Writer",
                    "fix_hint": "Body appears truncated. Re-run AI Writer to generate complete email content.",
                },
                "response": "Email body is truncated.",
            }
        return True, None

    def _step_action_requires_previous_draft(self, step_action: str, user_query: str) -> bool:
        """
        True when instruction implies sending content from a previous step (e.g. AI Writer).
        Preserve subject/body from provided_data; do not use placeholder or LLM-invented text.
        """
        combined = f"{(step_action or '').lower()} {(user_query or '').lower()}"
        signals = (
            "from step",
            "from previous step",
            "generated in step",
            "generated from step",
            "as generated",
            "body as generated",
            "use the generated",
            "send the generated",
            "send the draft",
            "send the composed",
            "body as composed",
            "composed in step",
            "retrieved from",
            "from outreach",
            "from the outreach",
        )
        return any(s in combined for s in signals)

    def _step_action_implies_list_or_search(self, step_action: str) -> bool:
        """True if step_action describes listing/searching/finding (not sending). Used to avoid picking send from provided_data."""
        if not (step_action and isinstance(step_action, str)):
            return False
        s = step_action.lower()
        if "send" in s and "don't send" not in s and "do not send" not in s:
            return False
        list_verbs = ("search", "list", "find", "get", "retrieve", "identify", "ensure", "look up", "fetch", "show")
        return any(v in s for v in list_verbs)

    def _step_action_implies_modify_labels(self, step_action: str) -> bool:
        """True if step_action describes adding/removing labels (not sending)."""
        if not (step_action and isinstance(step_action, str)):
            return False
        s = step_action.lower()
        return any(
            x in s for x in ("add label", "apply label", "modify label", "remove label", "change label", "label the")
        ) and "send" not in s

    def _implies_list_labels(self, step_action: str, user_query: str) -> bool:
        """True if step_action or user_query asks to list Gmail labels (not emails)."""
        combined = f" {(step_action or '').lower()} {(user_query or '').lower()} "
        return any(
            x in combined for x in (
                "list label", "list existing label", "list all label", "get label", "show label",
                "list labels", "list existing labels", "list all labels", "get labels", "show labels",
                "verify if ", "label exist", "labels exist", "check label", "see label",
            )
        ) and "list email" not in combined and "list message" not in combined

    async def _decide_action(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        step_action = (tool_args or {}).get("step_action") if isinstance(tool_args, dict) else None
        step_action_str = (step_action or "") if isinstance(step_action, str) else ""

        if tool_args and isinstance(tool_args, dict):
            to = _normalize_address_list(tool_args.get("to") or tool_args.get("recipient_email"))
            subject = (tool_args.get("subject") or tool_args.get("email_subject") or "").strip()
            body = (tool_args.get("body") or tool_args.get("content") or tool_args.get("email_body") or "").strip()
            # Strip placeholder content (e.g. "[Content from step 4]") — use provided_data instead
            if self._is_placeholder_content(subject):
                subject = ""
            if self._is_placeholder_content(body):
                body = ""
            # Unambiguous send: plan supplied to + (subject or body)
            if to and "@" in to and (subject or body):
                if self._step_action_requires_previous_draft(step_action_str, user_query):
                    draft_subj, draft_body = self._draft_from_provided_data(provided_data)
                    if draft_subj or draft_body:
                        subject = draft_subj or subject or "(No subject)"
                        body = draft_body or body
                if not (subject and body):
                    draft_subj, draft_body = self._draft_from_provided_data(provided_data)
                    subject = subject or draft_subj or "(No subject)"
                    body = body or draft_body
                params = {"to": to, "subject": subject or "(No subject)", "body": body or ""}
                if tool_args.get("cc"):
                    params["cc"] = (tool_args.get("cc") or "").strip()
                if tool_args.get("bcc"):
                    params["bcc"] = (tool_args.get("bcc") or "").strip()
                return {"action": "send", "params": params}

            # Unambiguous create/get label: tool_args has name, no "to"
            name = (tool_args.get("name") or "").strip()
            if name and not to:
                params = {"name": name}
                if tool_args.get("message_list_visibility") is not None:
                    params["message_list_visibility"] = str(tool_args["message_list_visibility"]).strip()
                if tool_args.get("label_list_visibility") is not None:
                    params["label_list_visibility"] = str(tool_args["label_list_visibility"]).strip()
                sa_lower = (step_action_str or "").lower()
                if any(x in sa_lower for x in ("if it does not exist", "if not exist", "get or create", "or create")):
                    return {"action": "get_or_create_label", "params": params}
                return {"action": "create_label", "params": params}

            # Unambiguous modify_labels: message_id (resolved) + add/remove label ids, no "to"
            msg_id = tool_args.get("message_id") or tool_args.get("messageId")
            add_ids = tool_args.get("add_label_ids") or tool_args.get("addLabelIds")
            remove_ids = tool_args.get("remove_label_ids") or tool_args.get("removeLabelIds")
            if not to and (add_ids or remove_ids):
                msg_id_str = (str(msg_id).strip() if msg_id else "") or None
                if (not msg_id_str or msg_id_str.startswith("{")) and provided_data and isinstance(provided_data, list):
                    for item in provided_data:
                        if isinstance(item, dict) and item.get("id") and "threadId" in item:
                            cand = str(item["id"]).strip()
                            if cand and not cand.startswith("{") and "@" not in cand:
                                msg_id_str = cand
                                break
                if msg_id_str and not msg_id_str.startswith("{") and "@" not in msg_id_str:
                    params = {"message_id": msg_id_str}
                    if add_ids:
                        params["add_label_ids"] = add_ids if isinstance(add_ids, list) else [add_ids]
                    if remove_ids:
                        params["remove_label_ids"] = remove_ids if isinstance(remove_ids, list) else [remove_ids]
                    return {"action": "modify_labels", "params": params}

            # List Gmail labels (not emails): "list labels", "list existing labels", "verify if label exists"
            if not to and self._implies_list_labels(step_action_str, user_query or ""):
                return {"action": "list_labels", "params": {}}

        # Use LLM for action decision when we have step_action (plan context) or when heuristics didn't match
        if step_action_str or not (tool_args and isinstance(tool_args, dict)):
            result = await self._decide_action_with_llm(user_query, provided_data, tool_args=tool_args)
            if result:
                return result

        # Fallback: step_action implies list labels → list_labels (not list messages)
        if self._implies_list_labels(step_action_str, user_query or ""):
            return {"action": "list_labels", "params": {}}
        # Fallback: step_action implies list/search emails → list (avoid provided_data send)
        if self._step_action_implies_list_or_search(step_action_str):
            list_params = {"max_results": DEFAULT_MAX_RESULTS}
            if (user_query or "").strip():
                q = await self._build_gmail_search_query((user_query or "").strip()) if self.llm_provider else _normalize_gmail_search_fallback((user_query or "").strip())
                list_params["q"] = list_params["query"] = q
            return {"action": "list", "params": list_params}
        # Only consider provided_data for SEND when step_action does NOT indicate list/search
        if provided_data and isinstance(provided_data, list) and not self._step_action_implies_list_or_search(step_action_str):
            for item in provided_data:
                if not isinstance(item, dict):
                    continue
                params = {}
                if item.get("message_id"):
                    params["message_id"] = item["message_id"]
                if item.get("thread_id"):
                    params["thread_id"] = item["thread_id"]
                if item.get("to"):
                    params["to"] = item["to"]
                if item.get("subject"):
                    params["subject"] = item["subject"]
                if item.get("body") or item.get("content"):
                    params["body"] = item.get("body") or item.get("content")
                if item.get("tone"):
                    params["tone"] = item["tone"]
                if item.get("send_at") or item.get("scheduled_time"):
                    params["send_at"] = item.get("send_at") or item.get("scheduled_time")
                if params.get("to") and (params.get("subject") or params.get("body")):
                    return {"action": "send", "params": params}
        result = await self._decide_action_with_llm(user_query, provided_data, tool_args=tool_args)
        if result:
            return result
        list_params = {"max_results": DEFAULT_MAX_RESULTS}
        if (user_query or "").strip():
            list_params["q"] = await self._build_gmail_search_query((user_query or "").strip()) if self.llm_provider else _normalize_gmail_search_fallback((user_query or "").strip())
        return {"action": "list", "params": list_params}

    async def _semantic_filter_messages(self, user_query: str, items: List[Dict]) -> List[Dict]:
        if not (user_query or "").strip() or not items or not self.llm_provider or len(items) <= 5:
            return items
        valid_ids = {m.get("id") for m in items if m.get("id")}
        preview = "\n".join(
            f"id={m.get('id')} from={m.get('from','')} to={m.get('to','')} subject={m.get('subject','')} snippet={(m.get('snippet') or '')[:150]}"
            for m in items[:25]
        )
        prompt = f"""User asked: "{user_query[:300]}"

Email previews (id, from, to, subject, snippet):
{preview}

Which message IDs are relevant? Reply with a JSON array of id strings only, e.g. ["id1","id2"]. If all are relevant or unclear, reply ["all"].
JSON array:"""
        out = await self._llm_generate_text(prompt, max_tokens=200)
        if not out:
            return items
        out = out.strip().strip("`").strip()
        try:
            ids = json.loads(out)
            if not isinstance(ids, list) or not ids or ids[0] == "all":
                return items
            id_set = {str(x) for x in ids if x}
            id_set = id_set & valid_ids
            if not id_set:
                return items
            filtered = [m for m in items if m.get("id") in id_set]
            return filtered if filtered else items
        except json.JSONDecodeError:
            return items

    # ----- Handlers -----
    async def _do_list_or_read(self, user_query: str, params: Dict) -> Dict:
        # Read single message by ID (Gmail-MCP-style): return full content + attachment list
        message_id = params.get("message_id")
        if message_id and not (params.get("q") or params.get("label_ids")):
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
            # Only log "opened" activity for inbound emails — never for messages sent by this
            # account (labelIds contains "SENT") or messages whose from-address matches the
            # connected account's own email address.  Without this guard, reading back a just-sent
            # message (which the runner does to build its final response) would immediately mark
            # the outbound email as "opened", polluting the activity log.
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
                    metadata={
                        "thread_id": full.get("threadId"),
                        "message_id": full.get("id") or message_id,
                    },
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

        max_results = int(params.get("max_results") or params.get("maxResults") or DEFAULT_MAX_RESULTS)
        max_results = min(max_results, MAX_RESULTS_CAP)
        q = params.get("q") or params.get("query") or ""  # MCP uses "query", we use "q"
        label_ids = params.get("label_ids")
        if label_ids and not isinstance(label_ids, list):
            label_ids = [label_ids] if label_ids else None
        page_token = params.get("page_token") or None
        messages, next_page_token = await self._list_messages(
            max_results=max_results,
            q=q if q else None,
            label_ids=label_ids,
            page_token=page_token,
        )
        if not messages:
            return {
                "success": True,
                "response": "No emails found for the given criteria.",
                "messages": [],
                "count": 0,
            }
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
        if (user_query or "").strip() and self.llm_provider and len(items) > 5:
            items = await self._semantic_filter_messages(user_query, items)
        response = f"Found {len(items)} email(s). " + "; ".join(
            f"[{m.get('from', '')}] {m.get('subject', '')}" for m in items[:5]
        )
        result = {
            "success": True,
            "response": response,
            "messages": items,
            "count": len(items),
        }
        if next_page_token:
            result["next_page_token"] = next_page_token
        return result

    async def _do_summarize(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        thread_id = params.get("thread_id")
        if not message_id and thread_id:
            thread = await self._get_thread(thread_id)
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
            if self.llm_provider:
                summary = await self._llm_generate_text(
                    f"Summarize this email thread in 3-6 sentences. Preserve key points, decisions, and any open questions.\n\nThread:\n{combined[:12000]}",
                    max_tokens=400,
                )
                summary = summary or combined[:1500]
            else:
                summary = combined[:1500]
            return {
                "success": True,
                "response": f"Thread summary: {summary}",
                "summary": summary,
                "thread_id": thread_id,
            }
        if not message_id:
            q = params.get("q") or ""
            if (user_query or "").strip() and self.llm_provider:
                q = await self._build_gmail_search_query((user_query or "").strip()) if not q else q
            messages, _ = await self._list_messages(max_results=10, q=q if q else None)
            if not messages:
                return {"success": False, "response": "No emails to summarize.", "summary": None}
            if self.llm_provider and (user_query or "").strip():
                preview = "\n".join(
                    f"id={m.get('id')} from={_parse_email_headers(m).get('from','')} subject={_parse_email_headers(m).get('subject','')} snippet={(m.get('snippet') or '')[:120]}"
                    for m in messages
                )
                picked = await self._llm_generate_text(
                    f'User asked: "{user_query[:300]}"\n\nEmail list:\n{preview}\n\nWhich single message id is most relevant? Reply with only that id.',
                    max_tokens=50,
                )
                if picked:
                    picked = picked.strip().strip('"').strip()
                    for m in messages:
                        if str(m.get("id")) == picked:
                            message_id = picked
                            break
            if not message_id:
                message_id = messages[0].get("id")
        if message_id:
            msg = await self._get_message(message_id)
            if not msg:
                return {"success": False, "response": "Could not fetch that email.", "summary": None}
            text = _get_snippet_or_body(msg)
            headers = _parse_email_headers(msg)
            if not text:
                text = msg.get("snippet") or ""
            if len(text) > 500 and self.llm_provider:
                summary = await self._llm_generate_text(
                    f"Summarize this email in 2-4 sentences. Preserve key details (dates, names, links, tasks). Note urgency or sentiment and any questions.\n\nEmail:\n{text[:8000]}",
                    max_tokens=300,
                )
                summary = summary or text[:1500]
            else:
                summary = text[:1500] if len(text) > 1500 else text
            return {
                "success": True,
                "response": f"Summary: {summary}",
                "summary": summary,
                "from": headers.get("from"),
                "subject": headers.get("subject"),
                "message_id": message_id,
            }
        return {"success": False, "response": "No message to summarize.", "summary": None}

    async def _do_draft_reply(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        if not message_id:
            messages, _ = await self._list_messages(max_results=1)
            if messages:
                message_id = messages[0].get("id")
        if not message_id:
            return {"success": False, "response": "No email selected to reply to. Specify which email or list inbox first."}
        orig = await self._get_message(message_id)
        if not orig:
            return {"success": False, "response": "Could not load that email."}
        headers = _parse_email_headers(orig)
        body_text = _get_snippet_or_body(orig)
        from_addr = headers.get("from", "")
        to_addr = _extract_email_from_header(from_addr)
        subject = headers.get("subject", "")
        reply_subject = subject if (subject or "").startswith("Re:") else f"Re: {subject}"
        tone = (params.get("tone") or "friendly").lower()
        body = params.get("body")
        if not body and self.llm_provider:
            body = await self._llm_generate_text(
                f"Write a short email reply (2-5 sentences). Tone: {tone}. Original email:\n{body_text[:3000]}\n\nUser request: {user_query}\n\nReply body only, no subject or greetings if minimal.",
                max_tokens=400,
            )
        if not body:
            body = "Thank you for your email. I will get back to you shortly."
        thread_id = orig.get("threadId")
        refs = (orig.get("payload") or {}).get("headers") or []
        ref_header = next((h.get("value") for h in refs if (h.get("name") or "").lower() == "message-id"), None)
        atts = params.get("attachments")
        atts = atts if isinstance(atts, list) else None
        raw = self._build_raw_message(
            to=to_addr,
            subject=reply_subject,
            body_text=(body or "").strip(),
            reply_to_message_id=message_id,
            thread_id=thread_id,
            references=ref_header,
            in_reply_to=ref_header,
            attachments=atts,
        )
        draft = await self._create_draft(raw)
        if not draft or "id" not in draft:
            return {"success": False, "response": "Failed to create draft in Gmail."}
        return {
            "success": True,
            "response": f"Draft created. Reply to '{subject}' with tone '{tone}'. You can edit and send it in Gmail.",
            "draft_id": draft.get("id"),
            "message": draft.get("message"),
        }

    async def _do_send(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        to = _normalize_address_list(params.get("to"))
        subject = (params.get("subject") or "").strip()
        body = (params.get("body") or params.get("content") or "").strip()
        cc = _normalize_address_list(params.get("cc"))
        bcc = _normalize_address_list(params.get("bcc"))
        if message_id and (body or subject):
            orig = await self._get_message(message_id)
            if orig:
                headers = _parse_email_headers(orig)
                to = to or headers.get("from", "")
                to = _extract_email_from_header(to)
                subject = subject or headers.get("subject", "")
                if subject and not subject.startswith("Re:"):
                    subject = f"Re: {subject}"
                body = body or "Thank you for your email."
                to_val = (to or "").strip() if isinstance(to, str) else ""
                valid, issue = self._validate_email_for_send(to_val, subject, body, is_reply=True)
                if not valid and issue:
                    return issue
                thread_id = orig.get("threadId")
                headers_list = (orig.get("payload") or {}).get("headers") or []
                ref_header = next(
                    (h.get("value") for h in headers_list if (h.get("name") or "").lower() == "message-id"),
                    None,
                )
                atts = params.get("attachments")
                atts = atts if isinstance(atts, list) else None
                mime_type = (params.get("mimeType") or params.get("mime_type") or "text/plain").strip()
                html_body = params.get("htmlBody") or params.get("html_body")
                connected_account_fid = self._to_int_or_none(
                    self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
                )
                project_fid = self._to_int_or_none(
                    self.app_config.get("project_fid")
                    or self.app_config.get("projectFid")
                    or self.agent_id
                )
                pre_activity_id = await self._log_email_activity_before_send(
                    to_address=to,
                    subject=subject,
                    content=(html_body or body or "").strip(),
                    metadata={"source": "gmail_tool", "mode": "reply"},
                )
                tracking_url = self._build_open_tracking_url(
                    activity_id=pre_activity_id,
                    project_fid=project_fid,
                    connected_account_fid=connected_account_fid,
                )
                mime_type, body, html_body = self._inject_open_tracking_pixel(
                    body_text=body,
                    mime_type=mime_type,
                    html_body=html_body,
                    tracking_url=tracking_url,
                )
                raw = self._build_raw_message(
                    to=to,
                    subject=subject,
                    body_text=body,
                    cc=cc,
                    bcc=bcc,
                    reply_to_message_id=message_id,
                    thread_id=thread_id,
                    in_reply_to=ref_header,
                    references=ref_header,
                    attachments=atts,
                    mime_type=mime_type,
                    html_body=html_body,
                )
                sent, send_status_code = await self._send_message_with_status(raw, thread_id=thread_id)
                if sent and sent.get("id"):
                    tracking_url_sent = self._build_open_tracking_url(
                        activity_id=pre_activity_id,
                        project_fid=project_fid,
                        connected_account_fid=connected_account_fid,
                        external_message_id=sent.get("id"),
                    )
                    await self._log_email_activity_after_send(
                        activity_id=pre_activity_id,
                        to_address=to,
                        subject=subject,
                        content=(html_body or body or "").strip(),
                        external_message_id=sent.get("id"),
                        gmail_status_code=send_status_code,
                        metadata={
                            "source": "gmail_tool",
                            "mode": "reply",
                            "open_tracking": {
                                "enabled": bool(tracking_url_sent or tracking_url),
                                "tracking_url": tracking_url_sent or tracking_url,
                            },
                        },
                    )
                    return {"success": True, "response": "Reply sent successfully.", "message_id": sent.get("id")}
        if to and (subject or body):
            subject = subject or "(No subject)"
            to_val = (to or "").strip() if isinstance(to, str) else (", ".join(to) if isinstance(to, list) else "")
            valid, issue = self._validate_email_for_send(to_val, subject, body, is_reply=False)
            if not valid and issue:
                return issue
            atts = params.get("attachments")
            atts = atts if isinstance(atts, list) else None
            mime_type = (params.get("mimeType") or params.get("mime_type") or "text/plain").strip()
            html_body = (params.get("htmlBody") or params.get("html_body")) or None
            connected_account_fid = self._to_int_or_none(
                self.app_config.get("connected_account_fid") or self.app_config.get("connected_service_id")
            )
            project_fid = self._to_int_or_none(
                self.app_config.get("project_fid")
                or self.app_config.get("projectFid")
                or self.agent_id
            )
            pre_activity_id = await self._log_email_activity_before_send(
                to_address=to,
                subject=subject,
                content=(html_body or body or "").strip(),
                metadata={"source": "gmail_tool", "mode": "new_email"},
            )
            tracking_url = self._build_open_tracking_url(
                activity_id=pre_activity_id,
                project_fid=project_fid,
                connected_account_fid=connected_account_fid,
            )
            mime_type, body, html_body = self._inject_open_tracking_pixel(
                body_text=body,
                mime_type=mime_type,
                html_body=html_body,
                tracking_url=tracking_url,
            )
            raw = self._build_raw_message(
                to=to,
                subject=subject,
                body_text=body,
                cc=cc,
                bcc=bcc,
                attachments=atts,
                mime_type=mime_type,
                html_body=html_body,
            )
            sent, send_status_code = await self._send_message_with_status(raw)
            if sent and sent.get("id"):
                tracking_url_sent = self._build_open_tracking_url(
                    activity_id=pre_activity_id,
                    project_fid=project_fid,
                    connected_account_fid=connected_account_fid,
                    external_message_id=sent.get("id"),
                )
                await self._log_email_activity_after_send(
                    activity_id=pre_activity_id,
                    to_address=to,
                    subject=subject,
                    content=(html_body or body or "").strip(),
                    external_message_id=sent.get("id"),
                    gmail_status_code=send_status_code,
                    metadata={
                        "source": "gmail_tool",
                        "mode": "new_email",
                        "open_tracking": {
                            "enabled": bool(tracking_url_sent or tracking_url),
                            "tracking_url": tracking_url_sent or tracking_url,
                        },
                    },
                )
                return {"success": True, "response": "Email sent successfully.", "message_id": sent.get("id")}
        return {"success": False, "response": "Could not send: missing message to reply to or missing to/subject/body for a new email."}

    async def _do_schedule(self, user_query: str, params: Dict) -> Dict:
        send_at = params.get("send_at") or params.get("scheduled_time")
        result = await self._do_draft_reply(user_query, {**params})
        if result.get("success") and send_at:
            result["scheduled_send_at"] = send_at
            result["response"] = (result.get("response") or "") + f" Scheduled to send at: {send_at}."
        return result

    async def _do_compose_draft(self, user_query: str, params: Dict) -> Dict:
        """Create a new draft (no reply). Gmail-MCP draft_email style: to, subject, body, cc, bcc, mimeType, htmlBody, attachments."""
        to = _normalize_address_list(params.get("to"))
        subject = (params.get("subject") or "").strip() or "(No subject)"
        body = (params.get("body") or params.get("content") or "").strip()
        cc = _normalize_address_list(params.get("cc"))
        bcc = _normalize_address_list(params.get("bcc"))
        if not to:
            return {"success": False, "response": "Recipient (to) is required to create a draft."}
        atts = params.get("attachments")
        atts = atts if isinstance(atts, list) else None
        mime_type = (params.get("mimeType") or params.get("mime_type") or "text/plain").strip()
        html_body = params.get("htmlBody") or params.get("html_body")
        raw = self._build_raw_message(
            to=to,
            subject=subject,
            body_text=body,
            cc=cc,
            bcc=bcc,
            attachments=atts,
            mime_type=mime_type,
            html_body=html_body,
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

    async def _do_list_drafts(self, user_query: str, params: Dict) -> Dict:
        max_results = min(int(params.get("max_results") or params.get("maxResults") or 20), MAX_RESULTS_CAP)
        page_token = params.get("page_token")
        q = params.get("q") or params.get("query") or ""
        drafts, next_token = await self._list_drafts(max_results=max_results, page_token=page_token, q=q if q else None)
        if not drafts:
            return {"success": True, "response": "No drafts found.", "drafts": [], "count": 0}
        # Enrich with message metadata (draft has id and message with id/threadId)
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

    async def _do_get_draft(self, user_query: str, params: Dict) -> Dict:
        draft_id = params.get("draft_id")
        if not draft_id:
            return {"success": False, "response": "No draft ID provided."}
        draft = await self._get_draft(draft_id)
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

    async def _do_update_draft(self, user_query: str, params: Dict) -> Dict:
        draft_id = params.get("draft_id")
        to = (params.get("to") or "").strip()
        subject = (params.get("subject") or "").strip()
        body = (params.get("body") or params.get("content") or "").strip()
        if not draft_id:
            return {"success": False, "response": "No draft ID provided."}
        if not to and not subject and not body:
            return {"success": False, "response": "Provide to, subject, or body to update the draft."}
        existing = await self._get_draft(draft_id)
        if not existing:
            return {"success": False, "response": "Could not load that draft."}
        msg = existing.get("message") or {}
        headers = _parse_email_headers(msg)
        to = to or headers.get("to", "")
        subject = subject or headers.get("subject", "(No subject)")
        body = body or _get_snippet_or_body(msg)
        atts = params.get("attachments")
        atts = atts if isinstance(atts, list) else None
        raw = self._build_raw_message(to=to, subject=subject, body_text=body, attachments=atts)
        updated = await self._update_draft(draft_id, raw, thread_id=msg.get("threadId"))
        if not updated:
            return {"success": False, "response": "Failed to update draft."}
        return {"success": True, "response": "Draft updated.", "draft_id": draft_id}

    async def _do_delete_draft(self, user_query: str, params: Dict) -> Dict:
        draft_id = params.get("draft_id")
        if not draft_id:
            return {"success": False, "response": "No draft ID provided."}
        ok = await self._delete_draft(draft_id)
        if not ok:
            return {"success": False, "response": "Could not delete that draft."}
        return {"success": True, "response": "Draft deleted.", "draft_id": draft_id}

    async def _do_list_labels(self, user_query: str, params: Dict) -> Dict:
        labels = await self._list_labels()
        if not labels:
            return {"success": True, "response": "No labels found.", "labels": []}
        items = [{"id": lb.get("id"), "name": lb.get("name"), "type": lb.get("type")} for lb in labels]
        return {"success": True, "response": f"Found {len(items)} label(s).", "labels": items, "count": len(items)}

    def _looks_like_gmail_label_id(self, s: str) -> bool:
        """True if s looks like a Gmail label ID (Label_xxx or system label), not a name or placeholder."""
        if not s or not isinstance(s, str):
            return False
        s = s.strip()
        if s.startswith("{") or "@" in s:
            return False
        if s.startswith("Label_"):
            return True
        system = ("INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "UNREAD", "IMPORTANT", "CATEGORY_")
        return any(s.upper() == x or s.upper().startswith(x) for x in system)

    async def _resolve_label_names_to_ids(self, id_or_name_list: List[str]) -> List[str]:
        """Resolve list of label IDs or names to Gmail API label IDs. Drops placeholders (start with {)."""
        out = []
        for x in (id_or_name_list or []):
            s = (str(x).strip() if x is not None else "") or ""
            if not s or s.startswith("{"):
                continue
            if self._looks_like_gmail_label_id(s):
                out.append(s)
                continue
            labels = await self._list_labels()
            found = None
            for lb in (labels or []):
                if (lb.get("name") or "").strip().lower() == s.lower():
                    found = lb.get("id")
                    break
            if found:
                out.append(found)
            else:
                created = await self._create_label(name=s, message_list_visibility="show", label_list_visibility="labelShow")
                if created and created.get("id"):
                    out.append(created["id"])
        return out

    async def _do_modify_labels(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        add_label_ids = params.get("add_label_ids") or []
        remove_label_ids = params.get("remove_label_ids") or []
        if not message_id:
            return {"success": False, "response": "No message ID provided."}
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "Provide add_label_ids and/or remove_label_ids."}
        if not isinstance(add_label_ids, list):
            add_label_ids = [add_label_ids] if add_label_ids else []
        if not isinstance(remove_label_ids, list):
            remove_label_ids = [remove_label_ids] if remove_label_ids else []
        add_label_ids = await self._resolve_label_names_to_ids(add_label_ids)
        remove_label_ids = await self._resolve_label_names_to_ids(remove_label_ids)
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "No valid label IDs after resolving names and dropping placeholders."}
        updated = await self._modify_message_labels(message_id, add_label_ids=add_label_ids or None, remove_label_ids=remove_label_ids or None)
        if not updated:
            return {"success": False, "response": "Could not update message labels."}
        return {
            "success": True,
            "response": "Message labels updated.",
            "message_id": message_id,
            "label_ids": updated.get("labelIds", []),
        }

    async def _do_get_profile(self, user_query: str, params: Dict) -> Dict:
        profile = await self._get_profile()
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

    async def _do_list_filters(self, user_query: str, params: Dict) -> Dict:
        filters = await self._list_filters()
        if not filters:
            return {"success": True, "response": "No filters found (or filters API not available).", "filters": []}
        items = []
        for f in filters[:50]:
            crit = f.get("criteria") or {}
            action = f.get("action") or {}
            items.append({
                "id": f.get("id"),
                "criteria": crit,
                "action": action,
            })
        return {"success": True, "response": f"Found {len(items)} filter(s).", "filters": items, "count": len(items)}

    async def _do_delete_email(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        if not message_id:
            return {"success": False, "response": "No message ID provided."}
        ok = await self._delete_message(str(message_id))
        if not ok:
            return {"success": False, "response": "Could not delete that email."}
        return {"success": True, "response": "Email permanently deleted.", "message_id": message_id}

    async def _do_batch_modify_emails(self, user_query: str, params: Dict) -> Dict:
        message_ids = params.get("message_ids") or []
        if isinstance(message_ids, str):
            message_ids = [message_ids]
        add_label_ids = params.get("add_label_ids") or []
        remove_label_ids = params.get("remove_label_ids") or []
        if not message_ids:
            return {"success": False, "response": "No message IDs provided."}
        if not add_label_ids and not remove_label_ids:
            return {"success": False, "response": "Provide add_label_ids and/or remove_label_ids."}
        if not isinstance(add_label_ids, list):
            add_label_ids = [add_label_ids] if add_label_ids else []
        if not isinstance(remove_label_ids, list):
            remove_label_ids = [remove_label_ids] if remove_label_ids else []
        batch_size = min(50, max(1, int(params.get("batch_size") or 50)))
        for i in range(0, len(message_ids), batch_size):
            chunk = message_ids[i : i + batch_size]
            ok = await self._batch_modify_messages(chunk, add_label_ids=add_label_ids or None, remove_label_ids=remove_label_ids or None)
            if not ok:
                return {"success": False, "response": f"Batch modify failed for chunk at index {i}.", "processed": i}
        return {"success": True, "response": f"Labels updated for {len(message_ids)} email(s).", "count": len(message_ids)}

    async def _do_batch_delete_emails(self, user_query: str, params: Dict) -> Dict:
        message_ids = params.get("message_ids") or []
        if isinstance(message_ids, str):
            message_ids = [message_ids]
        if not message_ids:
            return {"success": False, "response": "No message IDs provided."}
        batch_size = min(50, max(1, int(params.get("batch_size") or 50)))
        for i in range(0, len(message_ids), batch_size):
            chunk = message_ids[i : i + batch_size]
            ok = await self._batch_delete_messages(chunk)
            if not ok:
                return {"success": False, "response": f"Batch delete failed for chunk at index {i}.", "processed": i}
        return {"success": True, "response": f"Permanently deleted {len(message_ids)} email(s).", "count": len(message_ids)}

    async def _do_create_label(self, user_query: str, params: Dict) -> Dict:
        name = (params.get("name") or "").strip()
        if not name:
            return {"success": False, "response": "Label name is required."}
        msg_vis = (params.get("message_list_visibility") or "show").strip().lower()
        label_vis = (params.get("label_list_visibility") or "labelShow").strip()
        created = await self._create_label(name=name, message_list_visibility=msg_vis, label_list_visibility=label_vis)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that label."}
        return {"success": True, "response": f"Label '{name}' created.", "label": {"id": created.get("id"), "name": created.get("name")}}

    async def _do_get_or_create_label(self, user_query: str, params: Dict) -> Dict:
        """Get existing label by name or create it if it does not exist (Gmail-MCP get_or_create_label)."""
        name = (params.get("name") or "").strip()
        if not name:
            return {"success": False, "response": "Label name is required."}
        labels = await self._list_labels()
        for lb in labels or []:
            if (lb.get("name") or "").strip().lower() == name.lower():
                return {
                    "success": True,
                    "response": f"Label '{name}' already exists.",
                    "label": {"id": lb.get("id"), "name": lb.get("name")},
                    "created": False,
                }
        msg_vis = (params.get("message_list_visibility") or "show").strip().lower()
        label_vis = (params.get("label_list_visibility") or "labelShow").strip()
        created = await self._create_label(name=name, message_list_visibility=msg_vis, label_list_visibility=label_vis)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that label."}
        return {"success": True, "response": f"Label '{name}' created.", "label": {"id": created.get("id"), "name": created.get("name")}, "created": True}

    async def _do_update_label(self, user_query: str, params: Dict) -> Dict:
        label_id = params.get("label_id") or params.get("id")
        if not label_id:
            return {"success": False, "response": "Label ID is required."}
        name = params.get("name")
        if name is not None:
            name = str(name).strip()
        msg_vis = params.get("message_list_visibility")
        label_vis = params.get("label_list_visibility")
        updated = await self._update_label(str(label_id), name=name, message_list_visibility=msg_vis, label_list_visibility=label_vis)
        if not updated:
            return {"success": False, "response": "Could not update that label."}
        return {"success": True, "response": "Label updated.", "label": {"id": updated.get("id"), "name": updated.get("name")}}

    async def _do_delete_label(self, user_query: str, params: Dict) -> Dict:
        label_id = params.get("label_id") or params.get("id")
        if not label_id:
            return {"success": False, "response": "Label ID is required."}
        ok = await self._delete_label(str(label_id))
        if not ok:
            return {"success": False, "response": "Could not delete that label (system labels cannot be deleted)."}
        return {"success": True, "response": "Label deleted.", "label_id": label_id}

    async def _do_get_filter(self, user_query: str, params: Dict) -> Dict:
        filter_id = params.get("filter_id") or params.get("id")
        if not filter_id:
            return {"success": False, "response": "Filter ID is required."}
        f = await self._get_filter(str(filter_id))
        if not f:
            return {"success": False, "response": "Could not fetch that filter."}
        return {"success": True, "response": f"Filter: {f.get('criteria')} -> {f.get('action')}.", "filter": f}

    async def _do_delete_filter(self, user_query: str, params: Dict) -> Dict:
        filter_id = params.get("filter_id") or params.get("id")
        if not filter_id:
            return {"success": False, "response": "Filter ID is required."}
        ok = await self._delete_filter(str(filter_id))
        if not ok:
            return {"success": False, "response": "Could not delete that filter."}
        return {"success": True, "response": "Filter deleted.", "filter_id": filter_id}

    async def _do_create_filter(self, user_query: str, params: Dict) -> Dict:
        criteria = params.get("criteria")
        action = params.get("filter_action") or params.get("action")
        if not isinstance(criteria, dict) or not isinstance(action, dict):
            return {"success": False, "response": "criteria and action (both objects) are required."}
        created = await self._create_filter(criteria=criteria, action=action)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create that filter."}
        return {"success": True, "response": f"Filter created (id: {created.get('id')}).", "filter": created}

    async def _do_download_attachment(self, user_query: str, params: Dict) -> Dict:
        message_id = params.get("message_id")
        attachment_id = params.get("attachment_id")
        if not message_id or not attachment_id:
            return {"success": False, "response": "message_id and attachment_id are required."}
        att = await self._get_attachment(str(message_id), str(attachment_id))
        if not att:
            return {"success": False, "response": "Could not fetch that attachment."}
        # Return base64 data and size so caller can save or use; no filesystem write in agent
        data_b64 = att.get("data")  # Gmail returns base64url
        size = att.get("size", 0)
        return {"success": True, "response": f"Attachment retrieved ({size} bytes).", "data": data_b64, "size": size}

    async def initialize(self) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Gmail agent init: connected_service_id=%s user_id=%s company_id=%s permissions=%s",
                self.connected_service_id,
                self.user_id,
                self.company_id,
                getattr(self, "_permissions", "full"),
            )
        if not self.access_token:
            logger.warning("Gmail Agent: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("Gmail Agent Tool initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        logger.debug("Gmail Agent Tool cleaned up")

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        try:
            try:
                # Log *everything* the Gmail tool receives as context (redacted).
                logger.info(
                    "GmailTool.run: session_id=%s user_id=%s company_id=%s tool_args=%s provided_data=%s user_query=%s",
                    session_id,
                    getattr(self, "user_id", None),
                    getattr(self, "company_id", None),
                    json.dumps(_redact_secrets_for_log(tool_args or {}), default=str),
                    json.dumps(_redact_secrets_for_log(provided_data), default=str),
                    json.dumps(_redact_secrets_for_log(user_query or ""), default=str),
                )
            except Exception:
                pass

            if not self.access_token:
                yield event(AgentEvent.RESULT, {
                    "success": False,
                    "response": "Gmail is not connected. Please connect your Gmail account in the app's connected service.",
                    "query": user_query,
                })
                yield event(AgentEvent.FINAL, {"success": False, "response": json.dumps({"success": False, "error": "Gmail not connected."}), "result": {"success": False}})
                return

            yield event(AgentEvent.THOUGHT, "Understanding your Gmail request and choosing an action...")
            action_result = await self._decide_action(user_query, provided_data, tool_args=tool_args)
            action_type = action_result.get("action") or "list"
            params = action_result.get("params") or {}
            try:
                # Gmail-local "intent": the chosen action + params.
                logger.info(
                    "GmailTool.intent: action=%s params=%s",
                    action_type,
                    json.dumps(_redact_secrets_for_log(params), default=str),
                )
            except Exception:
                pass

            write_actions = (
                "send", "draft", "reply_draft", "compose_draft", "schedule", "update_draft", "delete_draft", "modify_labels",
                "delete_email", "batch_modify_emails", "batch_delete_emails",
                "create_label", "update_label", "delete_label", "create_filter", "delete_filter",
            )
            if self._permissions == "read-only" and action_type in write_actions:
                yield event(AgentEvent.RESULT, {
                    "success": False,
                    "response": "Gmail app is set to read-only. Sending, drafting, updating/deleting drafts, changing labels, deleting emails, batch operations, and creating/updating/deleting labels or filters are not allowed.",
                    "query": user_query,
                })
                yield event(AgentEvent.FINAL, {"success": False, "response": json.dumps({"success": False, "error": "Gmail is read-only."}), "result": {"success": False}})
                return

            if action_type in ("list", "read"):
                yield event(AgentEvent.PLAN, "Fetching and summarizing emails from Gmail.")
                result = await self._do_list_or_read(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type in ("summarize", "summarize_thread"):
                yield event(AgentEvent.PLAN, "Summarizing the selected email or thread.")
                result = await self._do_summarize(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type in ("draft", "reply_draft"):
                yield event(AgentEvent.PLAN, "Creating a reply draft.")
                result = await self._do_draft_reply(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "compose_draft":
                yield event(AgentEvent.PLAN, "Creating a new draft (compose).")
                result = await self._do_compose_draft(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "send":
                # If body or subject is missing and intent is purely to send, insert an AI Writer
                # step before this one so content is generated before the send executes.
                _send_body = (params.get("body") or "").strip()
                _send_subject = (params.get("subject") or "").strip()
                _needs_writer = (
                    not _send_body
                    or self._is_placeholder_content(_send_body)
                    or not _send_subject
                    or self._is_placeholder_content(_send_subject)
                )
                if _needs_writer:
                    _self_name = (
                        self.app_config.get("custom_name")
                        or self.app_config.get("app_name")
                        or "Gmail"
                    )
                    _to = (params.get("to") or "").strip()
                    _writer_hint = _send_subject or _to or "the recipient"
                    _writer_query = (
                        f"Write a complete professional email"
                        + (f" to {_to}" if _to else "")
                        + (f" with subject '{_send_subject}'" if _send_subject else "")
                        + f". Original request: {(user_query or '').strip()}"
                        + ". Include a clear subject line and full body. Output subject and body."
                    )
                    _seed: List[Any] = []
                    if isinstance(provided_data, list):
                        _seed.extend(x for x in provided_data[:30] if isinstance(x, (dict, list, str, int, float, bool)) or x is None)
                    elif provided_data is not None:
                        _seed.append(provided_data)
                    yield event(AgentEvent.THOUGHT, f"Email content missing — inserting AI Writer step before send.")
                    yield event(AgentEvent.FINAL, {
                        "_expand_plan": True,
                        "router_steps": [
                            {
                                "top_level": True,
                                "tool_name": "AI Writer",
                                "action": f"Write email: {_writer_hint[:60]}",
                                "query": _writer_query,
                                "arguments": {"_provided_data_seed": _seed} if _seed else {},
                            },
                            {
                                "top_level": True,
                                "tool_name": _self_name,
                                "action": f"Send email" + (f" to {_to}" if _to else ""),
                                "query": user_query,
                                "arguments": {},
                            },
                        ],
                        "success": True,
                        "response": "Email content missing — AI Writer step inserted to generate subject and body before sending.",
                    })
                    return
                yield event(AgentEvent.PLAN, "Sending the email.")
                result = await self._do_send(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "schedule":
                yield event(AgentEvent.PLAN, "Creating a draft to send later (schedule).")
                result = await self._do_schedule(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "list_drafts":
                yield event(AgentEvent.PLAN, "Listing your drafts.")
                result = await self._do_list_drafts(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "get_draft":
                yield event(AgentEvent.PLAN, "Fetching the draft.")
                result = await self._do_get_draft(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "update_draft":
                yield event(AgentEvent.PLAN, "Updating the draft.")
                result = await self._do_update_draft(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "delete_draft":
                yield event(AgentEvent.PLAN, "Deleting the draft.")
                result = await self._do_delete_draft(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "list_labels":
                yield event(AgentEvent.PLAN, "Listing your Gmail labels.")
                result = await self._do_list_labels(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "modify_labels":
                yield event(AgentEvent.PLAN, "Updating message labels.")
                result = await self._do_modify_labels(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "get_profile":
                yield event(AgentEvent.PLAN, "Fetching your Gmail profile and settings.")
                result = await self._do_get_profile(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "list_filters":
                yield event(AgentEvent.PLAN, "Listing your Gmail filters.")
                result = await self._do_list_filters(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "delete_email":
                yield event(AgentEvent.PLAN, "Deleting the email.")
                result = await self._do_delete_email(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "batch_modify_emails":
                yield event(AgentEvent.PLAN, "Updating labels for multiple emails.")
                result = await self._do_batch_modify_emails(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "batch_delete_emails":
                yield event(AgentEvent.PLAN, "Permanently deleting multiple emails.")
                result = await self._do_batch_delete_emails(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "create_label":
                yield event(AgentEvent.PLAN, "Creating a new label.")
                result = await self._do_create_label(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "get_or_create_label":
                yield event(AgentEvent.PLAN, "Getting or creating the label.")
                result = await self._do_get_or_create_label(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "update_label":
                yield event(AgentEvent.PLAN, "Updating the label.")
                result = await self._do_update_label(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "delete_label":
                yield event(AgentEvent.PLAN, "Deleting the label.")
                result = await self._do_delete_label(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "get_filter":
                yield event(AgentEvent.PLAN, "Fetching the filter.")
                result = await self._do_get_filter(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "delete_filter":
                yield event(AgentEvent.PLAN, "Deleting the filter.")
                result = await self._do_delete_filter(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "create_filter":
                yield event(AgentEvent.PLAN, "Creating a new filter.")
                result = await self._do_create_filter(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            if action_type == "download_attachment":
                yield event(AgentEvent.PLAN, "Downloading the attachment.")
                result = await self._do_download_attachment(user_query, params)
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, {"success": result.get("success", False), "response": json.dumps(result, default=str), "result": result})
                return

            yield event(AgentEvent.PLAN, "Listing recent emails.")
            result = await self._do_list_or_read(user_query, {"max_results": 15})
            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, {"success": result.get("success", True), "response": json.dumps(result, default=str), "result": result})
        except Exception as e:
            logger.exception("Gmail agent failed")
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "response": json.dumps({"success": False, "error": str(e)})})
