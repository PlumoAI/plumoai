"""
Slack Agent Tool for PlumoAI.

Read channels, send messages, reply to threads, search, manage channels, and react.
Uses Slack Web API: https://api.slack.com/methods

Design:
- Single intent pipeline: tool_args → provided_data → one LLM call → action + params.
- API layer: channels, messages, users, reactions, pins, files, search.
- Handlers: list_channels, read_messages, send_message, reply_thread, search, list_users, etc.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

SLACK_API_BASE = "https://slack.com/api"

DEFAULT_MESSAGE_LIMIT = 20
MAX_MESSAGE_LIMIT = 100
_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
_COMPANY_URL = (os.getenv("COMPANY_URL") or _AUTH_URL).rstrip("/")


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


def _redact_secrets_for_log(value: Any) -> Any:
    SENSITIVE_KEY_TOKENS = (
        "token", "secret", "password", "api_key", "access_token",
        "refresh_token", "authorization", "bearer", "private", "key",
        "client_secret", "credentials",
    )

    def _looks_sensitive_key(k: str) -> bool:
        return any(t in (k or "").lower() for t in SENSITIVE_KEY_TOKENS)

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
        if len(value) > 1200:
            return value[:600] + "…<truncated>…" + value[-120:]
        if len(value.strip()) >= 48:
            return _mask_str(value)
        return value
    return value


def _format_slack_message(msg: Dict) -> Dict:
    return {
        "ts": msg.get("ts", ""),
        "user": msg.get("user", ""),
        "text": msg.get("text", ""),
        "thread_ts": msg.get("thread_ts"),
        "reply_count": msg.get("reply_count", 0),
        "reactions": [
            {"name": r.get("name", ""), "count": r.get("count", 0)}
            for r in (msg.get("reactions") or [])
        ],
    }


def _resolve_channel_identifier(channel: str) -> str:
    if not channel or not isinstance(channel, str):
        return ""
    c = channel.strip().lstrip("#")
    return c


class SlackAgentTool(ConnectedServiceToolAgent):
    """
    Slack app agent. Capabilities:
    - Channels: list, create, archive, invite users, set topic/purpose.
    - Messages: read history, send, reply to thread, update, delete, schedule.
    - Search: search messages across workspace.
    - Users: list, get info, lookup by email.
    - Reactions: add, remove, get.
    - Pins: add, remove, list.
    - Files: upload, list, delete.
    """

    TOOL_NAME = "Slack"
    TOOL_DESCRIPTION = """Slack AI Agent: read, send, search, and manage Slack workspace communication.

USE WHEN: user mentions Slack, channel, message, DM, thread, workspace, send message, post, notify, or team communication.

ACTIONS: list_channels, read_messages, send_message, reply_thread, update_message, delete_message, schedule_message, search_messages, list_users, get_user_info, lookup_user_by_email, add_reaction, remove_reaction, get_reactions, create_channel, archive_channel, invite_to_channel, set_channel_topic, set_channel_purpose, pin_message, unpin_message, list_pins, upload_file, list_files, delete_file."""

    ACTION_DESCRIPTIONS = (
        "list_channels=list workspace channels; read_messages=read channel history; "
        "send_message=post message to channel; reply_thread=reply in thread; "
        "update_message=edit existing message; delete_message=delete a message; "
        "schedule_message=schedule a future message; search_messages=search across workspace; "
        "list_users=list workspace members; get_user_info=get user details by ID; "
        "lookup_user_by_email=find user by email; add_reaction=add emoji reaction; "
        "remove_reaction=remove reaction; get_reactions=get reactions on message; "
        "create_channel=create new channel; archive_channel=archive channel; "
        "invite_to_channel=invite user to channel; set_channel_topic=set channel topic; "
        "set_channel_purpose=set channel purpose; pin_message=pin a message; "
        "unpin_message=unpin a message; list_pins=list pinned items; "
        "upload_file=upload file to channel; list_files=list files; delete_file=delete a file"
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
        self._channel_cache: Dict[str, str] = {}
        self._user_cache: Dict[str, Dict] = {}

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _refresh_access_token_if_needed(self) -> bool:
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
            logger.debug("Slack LLM generate failed: %s", e)
        return None

    # ----- Slack API layer -----
    async def _slack_request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Optional[Dict]:
        url = f"{SLACK_API_BASE}/{endpoint}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0)
        headers = self._headers()
        if method.upper() == "GET":
            r = await self._httpx_client.request(method, url, params=params, headers=headers)
        else:
            r = await self._httpx_client.request(method, url, json=json_body, params=params, headers=headers)

        if r.status_code == 401 and retry_401 and await self._refresh_access_token_if_needed():
            return await self._slack_request(method, endpoint, json_body=json_body, params=params, retry_401=False)
        if r.status_code >= 400:
            logger.warning("Slack API %s %s -> %s %s", method, endpoint, r.status_code, (r.text or "")[:500])
            return None
        try:
            data = r.json()
        except Exception:
            return None

        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error == "token_expired" and retry_401 and await self._refresh_access_token_if_needed():
                return await self._slack_request(method, endpoint, json_body=json_body, params=params, retry_401=False)
            logger.warning("Slack API error: %s %s -> %s", method, endpoint, error)
            return data
        return data

    async def _resolve_channel_id(self, channel: str) -> Optional[str]:
        channel = _resolve_channel_identifier(channel)
        if not channel:
            return None
        if channel.startswith("C") and len(channel) >= 9 and channel[1:].isalnum():
            return channel
        if channel in self._channel_cache:
            return self._channel_cache[channel]
        cursor = None
        for _ in range(10):
            params = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await self._slack_request("GET", "conversations.list", params=params)
            if not data or not data.get("ok"):
                break
            for ch in data.get("channels", []):
                name = (ch.get("name") or "").lower()
                cid = ch.get("id", "")
                self._channel_cache[name] = cid
                if name == channel.lower():
                    return cid
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return None

    # ----- Channel operations -----
    async def _list_channels(self, limit: int = 100, types: str = "public_channel,private_channel") -> List[Dict]:
        channels = []
        cursor = None
        for _ in range(5):
            params = {"types": types, "limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor
            data = await self._slack_request("GET", "conversations.list", params=params)
            if not data or not data.get("ok"):
                break
            for ch in data.get("channels", []):
                channels.append({
                    "id": ch.get("id", ""),
                    "name": ch.get("name", ""),
                    "is_private": ch.get("is_private", False),
                    "num_members": ch.get("num_members", 0),
                    "topic": (ch.get("topic") or {}).get("value", ""),
                    "purpose": (ch.get("purpose") or {}).get("value", ""),
                })
                if len(channels) >= limit:
                    return channels
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return channels

    async def _read_messages(
        self,
        channel: str,
        limit: int = DEFAULT_MESSAGE_LIMIT,
        oldest: Optional[str] = None,
        latest: Optional[str] = None,
    ) -> List[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return []
        params: Dict[str, Any] = {"channel": channel_id, "limit": min(limit, MAX_MESSAGE_LIMIT)}
        if oldest:
            params["oldest"] = oldest
        if latest:
            params["latest"] = latest
        data = await self._slack_request("GET", "conversations.history", params=params)
        if not data or not data.get("ok"):
            return []
        return [_format_slack_message(m) for m in data.get("messages", [])]

    async def _read_thread(self, channel: str, thread_ts: str, limit: int = DEFAULT_MESSAGE_LIMIT) -> List[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return []
        params: Dict[str, Any] = {"channel": channel_id, "ts": thread_ts, "limit": min(limit, MAX_MESSAGE_LIMIT)}
        data = await self._slack_request("GET", "conversations.replies", params=params)
        if not data or not data.get("ok"):
            return []
        return [_format_slack_message(m) for m in data.get("messages", [])]

    async def _send_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: Optional[str] = None,
        unfurl_links: bool = True,
    ) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        body: Dict[str, Any] = {"channel": channel_id, "text": text, "unfurl_links": unfurl_links}
        if thread_ts:
            body["thread_ts"] = thread_ts
        data = await self._slack_request("POST", "chat.postMessage", json_body=body)
        if not data or not data.get("ok"):
            return data
        msg = data.get("message", {})
        return {"ok": True, "channel": data.get("channel", channel_id), "ts": msg.get("ts", ""), "text": msg.get("text", "")}

    async def _update_message(self, channel: str, ts: str, text: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "chat.update", json_body={"channel": channel_id, "ts": ts, "text": text})

    async def _delete_message(self, channel: str, ts: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "chat.delete", json_body={"channel": channel_id, "ts": ts})

    async def _schedule_message(self, channel: str, text: str, post_at: int, thread_ts: Optional[str] = None) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        body: Dict[str, Any] = {"channel": channel_id, "text": text, "post_at": post_at}
        if thread_ts:
            body["thread_ts"] = thread_ts
        return await self._slack_request("POST", "chat.scheduleMessage", json_body=body)

    async def _search_messages(self, query: str, count: int = 20) -> List[Dict]:
        params = {"query": query, "count": min(count, 100), "sort": "timestamp", "sort_dir": "desc"}
        data = await self._slack_request("GET", "search.messages", params=params)
        if not data or not data.get("ok"):
            return []
        matches = (data.get("messages") or {}).get("matches", [])
        return [
            {
                "ts": m.get("ts", ""),
                "text": m.get("text", ""),
                "user": (m.get("username") or m.get("user", "")),
                "channel": (m.get("channel") or {}).get("name", ""),
                "permalink": m.get("permalink", ""),
            }
            for m in matches
        ]

    # ----- User operations -----
    async def _list_users(self, limit: int = 100) -> List[Dict]:
        users = []
        cursor = None
        for _ in range(5):
            params: Dict[str, Any] = {"limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor
            data = await self._slack_request("GET", "users.list", params=params)
            if not data or not data.get("ok"):
                break
            for u in data.get("members", []):
                if u.get("deleted") or u.get("is_bot"):
                    continue
                profile = u.get("profile") or {}
                users.append({
                    "id": u.get("id", ""),
                    "name": u.get("name", ""),
                    "real_name": u.get("real_name", profile.get("real_name", "")),
                    "email": profile.get("email", ""),
                    "title": profile.get("title", ""),
                    "is_admin": u.get("is_admin", False),
                })
                if len(users) >= limit:
                    return users
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return users

    async def _get_user_info(self, user_id: str) -> Optional[Dict]:
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        data = await self._slack_request("GET", "users.info", params={"user": user_id})
        if not data or not data.get("ok"):
            return None
        u = data.get("user", {})
        profile = u.get("profile") or {}
        info = {
            "id": u.get("id", ""),
            "name": u.get("name", ""),
            "real_name": u.get("real_name", profile.get("real_name", "")),
            "email": profile.get("email", ""),
            "title": profile.get("title", ""),
            "is_admin": u.get("is_admin", False),
            "tz": u.get("tz", ""),
        }
        self._user_cache[user_id] = info
        return info

    async def _lookup_user_by_email(self, email: str) -> Optional[Dict]:
        data = await self._slack_request("GET", "users.lookupByEmail", params={"email": email})
        if not data or not data.get("ok"):
            return None
        u = data.get("user", {})
        profile = u.get("profile") or {}
        return {
            "id": u.get("id", ""),
            "name": u.get("name", ""),
            "real_name": u.get("real_name", profile.get("real_name", "")),
            "email": profile.get("email", ""),
        }

    # ----- Reactions -----
    async def _add_reaction(self, channel: str, ts: str, emoji: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "reactions.add", json_body={
            "channel": channel_id, "timestamp": ts, "name": emoji.strip(":")
        })

    async def _remove_reaction(self, channel: str, ts: str, emoji: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "reactions.remove", json_body={
            "channel": channel_id, "timestamp": ts, "name": emoji.strip(":")
        })

    async def _get_reactions(self, channel: str, ts: str) -> List[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return []
        data = await self._slack_request("GET", "reactions.get", params={"channel": channel_id, "timestamp": ts, "full": "true"})
        if not data or not data.get("ok"):
            return []
        msg = data.get("message", {})
        return [{"name": r.get("name", ""), "count": r.get("count", 0), "users": r.get("users", [])} for r in (msg.get("reactions") or [])]

    # ----- Channel management -----
    async def _create_channel(self, name: str, is_private: bool = False) -> Optional[Dict]:
        return await self._slack_request("POST", "conversations.create", json_body={"name": name, "is_private": is_private})

    async def _archive_channel(self, channel: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "conversations.archive", json_body={"channel": channel_id})

    async def _invite_to_channel(self, channel: str, user_ids: List[str]) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "conversations.invite", json_body={
            "channel": channel_id, "users": ",".join(user_ids)
        })

    async def _set_channel_topic(self, channel: str, topic: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "conversations.setTopic", json_body={"channel": channel_id, "topic": topic})

    async def _set_channel_purpose(self, channel: str, purpose: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "conversations.setPurpose", json_body={"channel": channel_id, "purpose": purpose})

    # ----- Pins -----
    async def _pin_message(self, channel: str, ts: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "pins.add", json_body={"channel": channel_id, "timestamp": ts})

    async def _unpin_message(self, channel: str, ts: str) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        return await self._slack_request("POST", "pins.remove", json_body={"channel": channel_id, "timestamp": ts})

    async def _list_pins(self, channel: str) -> List[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return []
        data = await self._slack_request("GET", "pins.list", params={"channel": channel_id})
        if not data or not data.get("ok"):
            return []
        return [
            {
                "type": item.get("type", ""),
                "created": item.get("created", 0),
                "message": _format_slack_message(item.get("message", {})) if item.get("message") else {},
            }
            for item in data.get("items", [])
        ]

    # ----- Files -----
    async def _upload_file(self, channel: str, content: str, filename: str, title: Optional[str] = None) -> Optional[Dict]:
        channel_id = await self._resolve_channel_id(channel)
        if not channel_id:
            return None
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0)
        r = await self._httpx_client.post(
            f"{SLACK_API_BASE}/files.upload",
            headers={"Authorization": f"Bearer {self.access_token}"},
            data={"channels": channel_id, "filename": filename, "title": title or filename},
            files={"file": (filename, content.encode("utf-8"))},
        )
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except Exception:
            return None

    async def _list_files(self, channel: Optional[str] = None, count: int = 20) -> List[Dict]:
        params: Dict[str, Any] = {"count": min(count, 100)}
        if channel:
            channel_id = await self._resolve_channel_id(channel)
            if channel_id:
                params["channel"] = channel_id
        data = await self._slack_request("GET", "files.list", params=params)
        if not data or not data.get("ok"):
            return []
        return [
            {"id": f.get("id", ""), "name": f.get("name", ""), "title": f.get("title", ""),
             "filetype": f.get("filetype", ""), "size": f.get("size", 0), "created": f.get("created", 0)}
            for f in data.get("files", [])
        ]

    async def _delete_file(self, file_id: str) -> Optional[Dict]:
        return await self._slack_request("POST", "files.delete", json_body={"file": file_id})

    # ----- Intent pipeline -----
    async def _decide_action(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if tool_args and isinstance(tool_args, dict):
            channel = (tool_args.get("channel") or tool_args.get("channel_name") or "").strip()
            text = (tool_args.get("text") or tool_args.get("message") or tool_args.get("body") or "").strip()
            thread_ts = (tool_args.get("thread_ts") or "").strip()

            if tool_args.get("action"):
                action = str(tool_args["action"]).strip().lower()
                params = dict(tool_args)
                params.pop("action", None)
                params.pop("step_action", None)
                return {"action": action, "params": params}

            if channel and text:
                if thread_ts:
                    return {"action": "reply_thread", "params": {"channel": channel, "text": text, "thread_ts": thread_ts}}
                return {"action": "send_message", "params": {"channel": channel, "text": text}}

        result = await self._decide_action_with_llm(user_query, provided_data, tool_args=tool_args)
        if result:
            return result

        return {"action": "list_channels", "params": {"limit": 20}}

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
        if provided_data and isinstance(provided_data, list):
            for item in provided_data[:3]:
                if isinstance(item, dict):
                    context_parts.append(json.dumps(
                        {k: v for k, v in item.items() if k in ("channel", "text", "message", "user", "ts", "thread_ts")}
                    ))
        if step_action:
            context_parts.append("Step/context: " + step_action)
        context = " | ".join(context_parts) if context_parts else ""

        prompt = f"""You are a Slack assistant. Output exactly one JSON object. No markdown, no explanation.

ACTION DESCRIPTIONS:
{self.ACTION_DESCRIPTIONS}

JSON keys (use exactly):
- "action": one of the actions listed above
- "channel": channel name or ID
- "text": message text (for send_message/reply_thread)
- "thread_ts": thread timestamp (for reply_thread/read_thread)
- "ts": message timestamp (for update/delete/reactions)
- "query": search query (for search_messages)
- "emoji": emoji name without colons (for reactions)
- "user_id": user ID (for get_user_info)
- "email": email address (for lookup_user_by_email)
- "name": channel name (for create_channel)
- "is_private": boolean (for create_channel)
- "user_ids": array of user IDs (for invite_to_channel)
- "topic": string (for set_channel_topic)
- "purpose": string (for set_channel_purpose)
- "limit": number of results
- "file_id": file ID (for delete_file)
- "post_at": unix timestamp (for schedule_message)

Context:
{context}

User request:
{(user_query or "").strip()[:800]}

JSON:"""
        out = await self._llm_generate_text(prompt, max_tokens=400)
        if not out:
            return None
        out = out.strip()
        for prefix in ("```json", "```"):
            if out.startswith(prefix):
                out = out[len(prefix):].strip()
            if out.endswith("```"):
                out = out[:-3].strip()
        try:
            data = json.loads(out)
            action = (data.get("action") or "list_channels").lower()
            valid_actions = (
                "list_channels", "read_messages", "send_message", "reply_thread",
                "update_message", "delete_message", "schedule_message", "search_messages",
                "list_users", "get_user_info", "lookup_user_by_email",
                "add_reaction", "remove_reaction", "get_reactions",
                "create_channel", "archive_channel", "invite_to_channel",
                "set_channel_topic", "set_channel_purpose",
                "pin_message", "unpin_message", "list_pins",
                "upload_file", "list_files", "delete_file",
            )
            if action not in valid_actions:
                action = "list_channels"
            params = {k: v for k, v in data.items() if k != "action" and v is not None}
            return {"action": action, "params": params}
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    # ----- Handler dispatch -----
    async def _execute_action(self, action: str, params: Dict) -> Dict:
        try:
            if action == "list_channels":
                channels = await self._list_channels(limit=params.get("limit", 100))
                return {"success": True, "action": action, "channels": channels, "count": len(channels)}

            elif action == "read_messages":
                channel = params.get("channel", "")
                if not channel:
                    return {"success": False, "response": "No channel specified."}
                thread_ts = params.get("thread_ts")
                if thread_ts:
                    messages = await self._read_thread(channel, thread_ts, limit=params.get("limit", DEFAULT_MESSAGE_LIMIT))
                else:
                    messages = await self._read_messages(
                        channel, limit=params.get("limit", DEFAULT_MESSAGE_LIMIT),
                        oldest=params.get("oldest"), latest=params.get("latest"),
                    )
                return {"success": True, "action": action, "channel": channel, "messages": messages, "count": len(messages)}

            elif action == "send_message":
                channel = params.get("channel", "")
                text = params.get("text", "")
                if not channel or not text:
                    return {"success": False, "response": "Channel and text are required.", "execution_issue": True,
                            "need_discovery": True, "missing_fields": ["channel", "text"]}
                result = await self._send_message(channel, text)
                if result and result.get("ok"):
                    return {"success": True, "action": action, "channel": channel, "ts": result.get("ts", ""),
                            "response": f"Message sent to #{channel}."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to send message: {error}"}

            elif action == "reply_thread":
                channel = params.get("channel", "")
                text = params.get("text", "")
                thread_ts = params.get("thread_ts", "")
                if not channel or not text or not thread_ts:
                    return {"success": False, "response": "Channel, text, and thread_ts are required.",
                            "execution_issue": True, "need_discovery": True,
                            "missing_fields": [f for f in ["channel", "text", "thread_ts"] if not params.get(f)]}
                result = await self._send_message(channel, text, thread_ts=thread_ts)
                if result and result.get("ok"):
                    return {"success": True, "action": action, "channel": channel, "thread_ts": thread_ts,
                            "ts": result.get("ts", ""), "response": f"Reply sent in thread."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to reply: {error}"}

            elif action == "update_message":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                text = params.get("text", "")
                if not channel or not ts or not text:
                    return {"success": False, "response": "Channel, ts, and text are required."}
                result = await self._update_message(channel, ts, text)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Message updated." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "delete_message":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                if not channel or not ts:
                    return {"success": False, "response": "Channel and ts are required."}
                result = await self._delete_message(channel, ts)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Message deleted." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "schedule_message":
                channel = params.get("channel", "")
                text = params.get("text", "")
                post_at = params.get("post_at")
                if not channel or not text or not post_at:
                    return {"success": False, "response": "Channel, text, and post_at (unix timestamp) are required."}
                result = await self._schedule_message(channel, text, int(post_at), thread_ts=params.get("thread_ts"))
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Message scheduled." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "search_messages":
                query = params.get("query", "")
                if not query:
                    return {"success": False, "response": "Search query is required."}
                results = await self._search_messages(query, count=params.get("limit", 20))
                return {"success": True, "action": action, "query": query, "results": results, "count": len(results)}

            elif action == "list_users":
                users = await self._list_users(limit=params.get("limit", 100))
                return {"success": True, "action": action, "users": users, "count": len(users)}

            elif action == "get_user_info":
                uid = params.get("user_id", "")
                if not uid:
                    return {"success": False, "response": "user_id is required."}
                info = await self._get_user_info(uid)
                return {"success": bool(info), "action": action, "user": info} if info else {"success": False, "response": "User not found."}

            elif action == "lookup_user_by_email":
                email = params.get("email", "")
                if not email:
                    return {"success": False, "response": "Email is required."}
                info = await self._lookup_user_by_email(email)
                return {"success": bool(info), "action": action, "user": info} if info else {"success": False, "response": "User not found."}

            elif action == "add_reaction":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                emoji = params.get("emoji", "")
                if not channel or not ts or not emoji:
                    return {"success": False, "response": "Channel, ts, and emoji are required."}
                result = await self._add_reaction(channel, ts, emoji)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": f"Reaction :{emoji}: added." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "remove_reaction":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                emoji = params.get("emoji", "")
                if not channel or not ts or not emoji:
                    return {"success": False, "response": "Channel, ts, and emoji are required."}
                result = await self._remove_reaction(channel, ts, emoji)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": f"Reaction :{emoji}: removed." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "get_reactions":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                if not channel or not ts:
                    return {"success": False, "response": "Channel and ts are required."}
                reactions = await self._get_reactions(channel, ts)
                return {"success": True, "action": action, "reactions": reactions}

            elif action == "create_channel":
                name = params.get("name", "")
                if not name:
                    return {"success": False, "response": "Channel name is required."}
                result = await self._create_channel(name, is_private=params.get("is_private", False))
                if result and result.get("ok"):
                    ch = result.get("channel", {})
                    return {"success": True, "action": action, "channel_id": ch.get("id", ""), "name": ch.get("name", ""),
                            "response": f"Channel #{ch.get('name', name)} created."}
                return {"success": False, "response": f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "archive_channel":
                channel = params.get("channel", "")
                if not channel:
                    return {"success": False, "response": "Channel is required."}
                result = await self._archive_channel(channel)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Channel archived." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "invite_to_channel":
                channel = params.get("channel", "")
                user_ids = params.get("user_ids", [])
                if not channel or not user_ids:
                    return {"success": False, "response": "Channel and user_ids are required."}
                result = await self._invite_to_channel(channel, user_ids if isinstance(user_ids, list) else [user_ids])
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Users invited." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "set_channel_topic":
                channel = params.get("channel", "")
                topic = params.get("topic", "")
                if not channel or not topic:
                    return {"success": False, "response": "Channel and topic are required."}
                result = await self._set_channel_topic(channel, topic)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Topic set." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "set_channel_purpose":
                channel = params.get("channel", "")
                purpose = params.get("purpose", "")
                if not channel or not purpose:
                    return {"success": False, "response": "Channel and purpose are required."}
                result = await self._set_channel_purpose(channel, purpose)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Purpose set." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "pin_message":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                if not channel or not ts:
                    return {"success": False, "response": "Channel and ts are required."}
                result = await self._pin_message(channel, ts)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Message pinned." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "unpin_message":
                channel = params.get("channel", "")
                ts = params.get("ts", "")
                if not channel or not ts:
                    return {"success": False, "response": "Channel and ts are required."}
                result = await self._unpin_message(channel, ts)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "Message unpinned." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            elif action == "list_pins":
                channel = params.get("channel", "")
                if not channel:
                    return {"success": False, "response": "Channel is required."}
                pins = await self._list_pins(channel)
                return {"success": True, "action": action, "pins": pins, "count": len(pins)}

            elif action == "upload_file":
                channel = params.get("channel", "")
                content = params.get("content", "")
                filename = params.get("filename", "file.txt")
                if not channel or not content:
                    return {"success": False, "response": "Channel and content are required."}
                result = await self._upload_file(channel, content, filename, title=params.get("title"))
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "File uploaded." if result and result.get("ok") else "Failed to upload file."}

            elif action == "list_files":
                files = await self._list_files(channel=params.get("channel"), count=params.get("limit", 20))
                return {"success": True, "action": action, "files": files, "count": len(files)}

            elif action == "delete_file":
                file_id = params.get("file_id", "")
                if not file_id:
                    return {"success": False, "response": "file_id is required."}
                result = await self._delete_file(file_id)
                return {"success": bool(result and result.get("ok")), "action": action,
                        "response": "File deleted." if result and result.get("ok") else f"Failed: {(result or {}).get('error', 'unknown')}"}

            else:
                return {"success": False, "response": f"Unknown action: {action}"}

        except Exception as e:
            logger.exception("Slack action %s failed: %s", action, e)
            return {"success": False, "response": f"Error executing {action}: {str(e)}"}

    # ----- Main entry point (called by the runner) -----
    async def execute(
        self,
        user_query: str,
        *,
        provided_data: Optional[Any] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict, None]:
        try:
            yield event(AgentEvent.THOUGHT, f"Processing Slack request: {(user_query or '')[:200]}")

            decision = await self._decide_action(user_query, provided_data, tool_args=tool_args)
            action = decision.get("action", "list_channels")
            params = decision.get("params", {})

            yield event(AgentEvent.PLAN, f"Action: {action}, params: {json.dumps(_redact_secrets_for_log(params), default=str)[:500]}")

            result = await self._execute_action(action, params)

            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, result)

        except Exception as e:
            logger.exception("Slack agent execute failed: %s", e)
            error_result = {"success": False, "response": f"Slack agent error: {str(e)}"}
            yield event(AgentEvent.ERROR, error_result)
            yield event(AgentEvent.FINAL, error_result)
