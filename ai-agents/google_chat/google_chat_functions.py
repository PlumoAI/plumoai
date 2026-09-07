"""
Google Chat functions class for functions_wrapper plugin.

Each public @tool method maps to one Google Chat REST API operation
(https://developers.google.com/workspace/chat/api/reference/rest).
Private helpers handle HTTP, token refresh, and response formatting.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].

Resource names follow the Chat API convention, e.g.:
  spaces/{space}
  spaces/{space}/members/{member}
  spaces/{space}/messages/{message}
  spaces/{space}/messages/{message}/attachments/{attachment}
  spaces/{space}/messages/{message}/reactions/{reaction}
  spaces/{space}/messagePins/{messagePin}
  spaces/{space}/spaceEvents/{spaceEvent}
  customEmojis/{customEmoji}

These full resource names (as returned by list_*/get_* tools) should be
passed back verbatim to other tools that take a "name" argument.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

CHAT_API_BASE = "https://chat.googleapis.com/v1"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 1000
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


class GoogleChatFunctions(ConnectedServiceToolAgent):
    """
    Google Chat tool functions. Each @tool method is a Google Chat capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Google Chat: list/get/create/update/delete spaces, manage memberships, "
        "send/list/get/update/delete messages, react to messages, pin messages, "
        "read space events, and manage custom emojis."
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
        raise NotImplementedError("GoogleChatFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GoogleChatFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GoogleChatFunctions initialized")

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

    async def _chat_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        files: Optional[Dict] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{CHAT_API_BASE}{path}" if path.startswith("/") else f"{CHAT_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if files is not None:
            # Multipart upload (custom emojis / attachments); strip JSON content-type.
            headers = {"Authorization": f"Bearer {self.access_token}"}
            r = await self._httpx_client.request(method, url, params=params, files=files, headers=headers)
        elif json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._chat_request(
                method, path, json_body=json_body, params=params, files=files, retry_401=False
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "Chat API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._chat_request(
                method, path, json_body=json_body, params=params, files=files,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("Chat API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("Chat API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            return {"_error": True, "status_code": r.status_code, "message": (r.text or "")[:500]}
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Chat API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    @staticmethod
    def _error_result(data: Optional[Dict], default_msg: str) -> Optional[Dict]:
        if data is None:
            return {"success": False, "response": default_msg}
        if isinstance(data, dict) and data.get("_error"):
            return {
                "success": False,
                "response": f"{default_msg}: {data.get('status_code')} {data.get('message')}",
            }
        return None

    @staticmethod
    def _normalize_name(value: str, prefix: str) -> str:
        """Allow callers to pass either a bare ID or a full resource name."""
        value = (value or "").strip()
        if not value:
            return value
        return value if value.startswith(prefix) else f"{prefix}{value}"

    # ==================================================================
    # SPACES
    # ==================================================================

    @tool(
        description=(
            "List Google Chat spaces (rooms, group chats, and direct messages) that the "
            "authenticated user is a member of. Returns each space's resource name "
            "(e.g. 'spaces/AAAA...'), display name, space type, and whether it is a direct message. "
            "Use this to discover valid space names before calling other space/message tools."
        ),
        params={
            "filter": (
                "Optional filter, e.g. \"space_type = \\\"SPACE\\\"\" or "
                "\"space_type = \\\"GROUP_CHAT\\\" OR space_type = \\\"DIRECT_MESSAGE\\\"\". "
                "Leave empty to list all spaces the user belongs to."
            ),
            "page_size": "Maximum number of spaces to return (default 25, max 1000).",
            "page_token": "Token from a previous list_spaces call to fetch the next page.",
        },
    )
    async def list_spaces(
        self, filter: Optional[str] = None, page_size: int = DEFAULT_PAGE_SIZE, page_token: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if filter:
            params["filter"] = filter
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", "/spaces", params=params)
        err = self._error_result(data, "Failed to list spaces")
        if err:
            return err
        spaces = (data or {}).get("spaces") or []
        out = [
            {
                "name": s.get("name"),
                "displayName": s.get("displayName"),
                "spaceType": s.get("spaceType"),
                "type": s.get("type"),
                "spaceThreadingState": s.get("spaceThreadingState"),
            }
            for s in spaces
        ]
        return {
            "success": True,
            "response": f"Found {len(out)} space(s).",
            "spaces": out,
            "count": len(out),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    @tool(
        description=(
            "Get details about a single Google Chat space, including its display name, "
            "space type, threading state, and access settings."
        ),
        params={"name": "Resource name of the space, e.g. 'spaces/AAAAAAAAAAA' (from list_spaces)."},
    )
    async def get_space(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get space")
        if err:
            return err
        return {"success": True, "response": f"Space '{data.get('displayName') or name}'.", "space": data}

    @tool(
        description=(
            "Create a new Google Chat space (a named room). "
            "Only 'SPACE' type spaces can be created directly; for group chats / direct "
            "messages with specific people use setup_space instead."
        ),
        params={
            "display_name": "Display name for the new space, e.g. 'Project Falcon'.",
            "space_details": "Optional free-text description of the space's purpose, shown in space details.",
            "external_user_allowed": (
                "Whether users outside the workspace organization can be added to the space. Default false."
            ),
            "importMode": (
                "Set to true only when creating a space in import mode for data migration. Default false."
            ),
        },
    )
    async def create_space(
        self,
        display_name: str,
        space_details: Optional[str] = None,
        external_user_allowed: bool = False,
        importMode: bool = False,
    ) -> Dict:
        body: Dict[str, Any] = {
            "spaceType": "SPACE",
            "displayName": display_name,
            "externalUserAllowed": bool(external_user_allowed),
        }
        if importMode:
            body["importMode"] = True
        if space_details:
            body["spaceDetails"] = {"description": space_details}
        data = await self._chat_request("POST", "/spaces", json_body=body)
        err = self._error_result(data, "Failed to create space")
        if err:
            return err
        return {"success": True, "response": f"Created space '{display_name}'.", "space": data}

    @tool(
        description=(
            "Create a Google Chat space and add the given users to it in one call. "
            "Use this for group chats or direct messages (omit display_name and set "
            "space_type to 'DIRECT_MESSAGE' with exactly one member for a 1:1 DM)."
        ),
        params={
            "display_name": "Display name for a new named space. Omit for direct messages / unnamed group chats.",
            "space_type": "One of 'SPACE', 'GROUP_CHAT', or 'DIRECT_MESSAGE'. Default 'SPACE' if display_name set, else 'GROUP_CHAT'.",
            "member_emails": "List of email addresses of users to add as members of the new space.",
        },
    )
    async def setup_space(
        self,
        display_name: Optional[str] = None,
        space_type: Optional[str] = None,
        member_emails: Optional[List[str]] = None,
    ) -> Dict:
        resolved_type = space_type or ("SPACE" if display_name else "GROUP_CHAT")
        space_body: Dict[str, Any] = {"spaceType": resolved_type}
        if display_name:
            space_body["displayName"] = display_name
        body: Dict[str, Any] = {"space": space_body}
        memberships = []
        for email in member_emails or []:
            email = (email or "").strip()
            if email:
                memberships.append({"member": {"name": f"users/{email}", "type": "HUMAN"}})
        if memberships:
            body["memberships"] = memberships
        data = await self._chat_request("POST", "/spaces:setup", json_body=body)
        err = self._error_result(data, "Failed to set up space")
        if err:
            return err
        return {"success": True, "response": "Space set up successfully.", "space": data}

    @tool(
        description="Update a Google Chat space's display name, description, or guidelines.",
        params={
            "name": "Resource name of the space to update, e.g. 'spaces/AAAAAAAAAAA'.",
            "display_name": "New display name for the space.",
            "space_details": "New description text for the space.",
            "guidelines": "New guidelines/rules text for the space.",
        },
    )
    async def update_space(
        self,
        name: str,
        display_name: Optional[str] = None,
        space_details: Optional[str] = None,
        guidelines: Optional[str] = None,
    ) -> Dict:
        name = self._normalize_name(name, "spaces/")
        body: Dict[str, Any] = {}
        update_mask: List[str] = []
        if display_name is not None:
            body["displayName"] = display_name
            update_mask.append("displayName")
        if space_details is not None or guidelines is not None:
            details: Dict[str, Any] = {}
            if space_details is not None:
                details["description"] = space_details
                update_mask.append("spaceDetails.description")
            if guidelines is not None:
                details["guidelines"] = guidelines
                update_mask.append("spaceDetails.guidelines")
            body["spaceDetails"] = details
        if not update_mask:
            return {"success": False, "response": "No fields provided to update."}
        params = {"updateMask": ",".join(update_mask)}
        data = await self._chat_request("PATCH", f"/{name}", json_body=body, params=params)
        err = self._error_result(data, "Failed to update space")
        if err:
            return err
        return {"success": True, "response": "Space updated.", "space": data}

    @tool(
        description="Delete a Google Chat space. This permanently removes the space and all its messages.",
        params={"name": "Resource name of the space to delete, e.g. 'spaces/AAAAAAAAAAA'."},
    )
    async def delete_space(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to delete space")
        if err:
            return err
        return {"success": True, "response": f"Deleted space '{name}'."}

    @tool(
        description=(
            "Find an existing direct message (1:1) space with a given user, if one exists. "
            "Returns the space resource name if found."
        ),
        params={"user_email": "Email address (or 'users/{id}') of the other person in the direct message."},
    )
    async def find_direct_message(self, user_email: str) -> Dict:
        name = user_email if user_email.startswith("users/") else f"users/{user_email}"
        data = await self._chat_request("GET", "/spaces:findDirectMessage", params={"name": name})
        err = self._error_result(data, "Failed to find direct message")
        if err:
            return err
        if not data:
            return {"success": True, "response": "No direct message space found with this user.", "space": None}
        return {"success": True, "response": f"Found direct message space '{data.get('name')}'.", "space": data}

    # ==================================================================
    # MEMBERS
    # ==================================================================

    @tool(
        description=(
            "List memberships (members) of a Google Chat space, including human users, "
            "Google Groups, and the Chat app itself. Returns each membership's resource "
            "name, member type, and role."
        ),
        params={
            "space_name": "Resource name of the space, e.g. 'spaces/AAAAAAAAAAA'.",
            "filter": (
                "Optional filter, e.g. \"member.type = \\\"HUMAN\\\"\" to list only human members, "
                "or \"role = \\\"ROLE_MANAGER\\\"\" to list space managers."
            ),
            "page_size": "Maximum number of memberships to return (default 25, max 1000).",
            "page_token": "Token from a previous list_members call to fetch the next page.",
        },
    )
    async def list_members(
        self,
        space_name: str,
        filter: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: Optional[str] = None,
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if filter:
            params["filter"] = filter
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", f"/{space_name}/members", params=params)
        err = self._error_result(data, "Failed to list members")
        if err:
            return err
        memberships = (data or {}).get("memberships") or []
        out = [
            {
                "name": m.get("name"),
                "role": m.get("role"),
                "state": m.get("state"),
                "member": m.get("member"),
            }
            for m in memberships
        ]
        return {
            "success": True,
            "response": f"Found {len(out)} member(s).",
            "members": out,
            "count": len(out),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    @tool(
        description="Get details about a single membership in a Google Chat space.",
        params={"name": "Resource name of the membership, e.g. 'spaces/AAAAAAAAAAA/members/BBBBBBBB'."},
    )
    async def get_member(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get member")
        if err:
            return err
        return {"success": True, "response": "Membership details retrieved.", "member": data}

    @tool(
        description=(
            "Add a user or Google Group to a Google Chat space as a member. "
            "The authenticated user must have permission to invite members to the space."
        ),
        params={
            "space_name": "Resource name of the space to add the member to, e.g. 'spaces/AAAAAAAAAAA'.",
            "user_email": "Email address of the human user to add.",
            "role": "Role to assign: 'ROLE_MEMBER' (default) or 'ROLE_MANAGER'.",
        },
    )
    async def create_member(self, space_name: str, user_email: str, role: str = "ROLE_MEMBER") -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        body = {
            "member": {"name": f"users/{user_email}", "type": "HUMAN"},
            "role": role,
        }
        data = await self._chat_request("POST", f"/{space_name}/members", json_body=body)
        err = self._error_result(data, "Failed to add member")
        if err:
            return err
        return {"success": True, "response": f"Added '{user_email}' to space.", "member": data}

    @tool(
        description="Update a membership's role within a Google Chat space (e.g. promote a member to manager).",
        params={
            "name": "Resource name of the membership to update, e.g. 'spaces/AAAAAAAAAAA/members/BBBBBBBB'.",
            "role": "New role: 'ROLE_MEMBER' or 'ROLE_MANAGER'.",
        },
    )
    async def update_member(self, name: str, role: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request(
            "PATCH", f"/{name}", json_body={"role": role}, params={"updateMask": "role"}
        )
        err = self._error_result(data, "Failed to update member")
        if err:
            return err
        return {"success": True, "response": "Membership updated.", "member": data}

    @tool(
        description="Remove a member from a Google Chat space (revokes their access to the space).",
        params={"name": "Resource name of the membership to remove, e.g. 'spaces/AAAAAAAAAAA/members/BBBBBBBB'."},
    )
    async def delete_member(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to remove member")
        if err:
            return err
        return {"success": True, "response": "Member removed from space."}

    # ==================================================================
    # MESSAGES
    # ==================================================================

    @tool(
        description=(
            "List messages in a Google Chat space, ordered by creation time. "
            "Returns each message's resource name, sender, text/cards, thread, and create time. "
            "Message resource names returned here can be used with get_message, update_message, "
            "delete_message, create_reaction, and create_message_pin."
        ),
        params={
            "space_name": "Resource name of the space to list messages from, e.g. 'spaces/AAAAAAAAAAA'.",
            "page_size": "Maximum number of messages to return (default 25, max 1000).",
            "filter": (
                "Optional filter on createTime and/or thread, e.g. "
                "\"createTime > \\\"2026-01-01T00:00:00+00:00\\\"\"."
            ),
            "order_by": "Sort order, e.g. 'createTime DESC' for newest first. Default ascending by createTime.",
            "show_deleted": "Whether to include deleted messages (with content stripped). Default false.",
            "page_token": "Token from a previous list_messages call to fetch the next page.",
        },
    )
    async def list_messages(
        self,
        space_name: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        filter: Optional[str] = None,
        order_by: Optional[str] = None,
        show_deleted: bool = False,
        page_token: Optional[str] = None,
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if filter:
            params["filter"] = filter
        if order_by:
            params["orderBy"] = order_by
        if show_deleted:
            params["showDeleted"] = True
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", f"/{space_name}/messages", params=params)
        err = self._error_result(data, "Failed to list messages")
        if err:
            return err
        messages = (data or {}).get("messages") or []
        out = [
            {
                "name": m.get("name"),
                "text": m.get("text"),
                "sender": m.get("sender"),
                "createTime": m.get("createTime"),
                "thread": m.get("thread"),
                "cardsV2": m.get("cardsV2"),
            }
            for m in messages
        ]
        return {
            "success": True,
            "response": f"Found {len(out)} message(s).",
            "messages": out,
            "count": len(out),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    @tool(
        description="Get details about a single Google Chat message, including its text, sender, thread, and attachments.",
        params={"name": "Resource name of the message, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'."},
    )
    async def get_message(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get message")
        if err:
            return err
        return {"success": True, "response": "Message details retrieved.", "message": data}

    @tool(
        description=(
            "Send a text message to a Google Chat space. "
            "Optionally reply within an existing thread by providing thread_name or thread_key."
        ),
        params={
            "space_name": "Resource name of the space to post the message in, e.g. 'spaces/AAAAAAAAAAA'.",
            "text": "Plain text content of the message. Supports basic Chat formatting (e.g. *bold*, _italic_).",
            "thread_name": (
                "Optional resource name of an existing thread to reply to, e.g. "
                "'spaces/AAAAAAAAAAA/threads/CCCCCCCC' (from a message's 'thread' field)."
            ),
            "thread_key": (
                "Optional client-defined key identifying a thread; messages with the same "
                "thread_key in the same space are grouped into the same thread."
            ),
        },
    )
    async def send_message(
        self,
        space_name: str,
        text: str,
        thread_name: Optional[str] = None,
        thread_key: Optional[str] = None,
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        body: Dict[str, Any] = {"text": text}
        params: Dict[str, Any] = {}
        if thread_name or thread_key:
            thread: Dict[str, Any] = {}
            if thread_name:
                thread["name"] = thread_name
            if thread_key:
                thread["threadKey"] = thread_key
            body["thread"] = thread
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        data = await self._chat_request("POST", f"/{space_name}/messages", json_body=body, params=params or None)
        err = self._error_result(data, "Failed to send message")
        if err:
            return err
        return {"success": True, "response": "Message sent.", "message": data}

    @tool(
        description="Update the text of an existing Google Chat message that was sent by the calling user/app.",
        params={
            "name": "Resource name of the message to update, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'.",
            "text": "New plain text content for the message.",
        },
    )
    async def update_message(self, name: str, text: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request(
            "PATCH", f"/{name}", json_body={"text": text}, params={"updateMask": "text"}
        )
        err = self._error_result(data, "Failed to update message")
        if err:
            return err
        return {"success": True, "response": "Message updated.", "message": data}

    @tool(
        description="Delete a Google Chat message that was sent by the calling user/app.",
        params={"name": "Resource name of the message to delete, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'."},
    )
    async def delete_message(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to delete message")
        if err:
            return err
        return {"success": True, "response": "Message deleted."}

    # ==================================================================
    # ATTACHMENTS
    # ==================================================================

    @tool(
        description="Get metadata about an attachment on a Google Chat message (filename, content type, source).",
        params={
            "name": (
                "Resource name of the attachment, e.g. "
                "'spaces/AAAAAAAAAAA/messages/BBBBBBBB/attachments/CCCCCCCC'."
            )
        },
    )
    async def get_attachment(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get attachment")
        if err:
            return err
        return {"success": True, "response": "Attachment metadata retrieved.", "attachment": data}

    @tool(
        description=(
            "Upload a file as an attachment to a Google Chat space so it can be referenced "
            "when sending a message. Provide either a publicly accessible file_url to fetch, "
            "or raw base64-encoded file_content."
        ),
        params={
            "space_name": "Resource name of the space to upload the attachment to, e.g. 'spaces/AAAAAAAAAAA'.",
            "filename": "Filename to give the uploaded attachment, e.g. 'report.pdf'.",
            "file_url": "Optional URL to download the file content from before uploading.",
            "file_content_base64": "Optional base64-encoded file content (used if file_url is not provided).",
            "content_type": "MIME type of the file, e.g. 'application/pdf' or 'image/png'.",
        },
    )
    async def upload_attachment(
        self,
        space_name: str,
        filename: str,
        file_url: Optional[str] = None,
        file_content_base64: Optional[str] = None,
        content_type: str = "application/octet-stream",
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        file_bytes: Optional[bytes] = None
        if file_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    resp = await hc.get(file_url)
                resp.raise_for_status()
                file_bytes = resp.content
            except Exception as e:
                return {"success": False, "response": f"Failed to download file_url: {e}"}
        elif file_content_base64:
            try:
                file_bytes = base64.b64decode(file_content_base64)
            except Exception as e:
                return {"success": False, "response": f"Invalid file_content_base64: {e}"}
        else:
            return {"success": False, "response": "Provide either file_url or file_content_base64."}

        files = {"file": (filename, file_bytes, content_type)}
        data = await self._chat_request(
            "POST", f"/{space_name}/attachments:upload", files=files
        )
        err = self._error_result(data, "Failed to upload attachment")
        if err:
            return err
        return {"success": True, "response": f"Uploaded '{filename}'.", "attachment": data}

    # ==================================================================
    # REACTIONS
    # ==================================================================

    @tool(
        description="Add an emoji reaction to a Google Chat message.",
        params={
            "message_name": "Resource name of the message to react to, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'.",
            "emoji": "Unicode emoji character to react with, e.g. '👍' or '🎉'.",
        },
    )
    async def create_reaction(self, message_name: str, emoji: str) -> Dict:
        message_name = self._normalize_name(message_name, "spaces/")
        body = {"emoji": {"unicode": emoji}}
        data = await self._chat_request("POST", f"/{message_name}/reactions", json_body=body)
        err = self._error_result(data, "Failed to add reaction")
        if err:
            return err
        return {"success": True, "response": f"Added reaction '{emoji}'.", "reaction": data}

    @tool(
        description="Remove an emoji reaction from a Google Chat message.",
        params={
            "name": (
                "Resource name of the reaction to remove, e.g. "
                "'spaces/AAAAAAAAAAA/messages/BBBBBBBB/reactions/CCCCCCCC' (from list_reactions)."
            )
        },
    )
    async def delete_reaction(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to remove reaction")
        if err:
            return err
        return {"success": True, "response": "Reaction removed."}

    @tool(
        description="List the emoji reactions on a Google Chat message.",
        params={
            "message_name": "Resource name of the message, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'.",
            "filter": "Optional filter, e.g. \"emoji.unicode = \\\"👍\\\"\" to list only that reaction.",
            "page_size": "Maximum number of reactions to return (default 25, max 1000).",
            "page_token": "Token from a previous list_reactions call to fetch the next page.",
        },
    )
    async def list_reactions(
        self,
        message_name: str,
        filter: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: Optional[str] = None,
    ) -> Dict:
        message_name = self._normalize_name(message_name, "spaces/")
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if filter:
            params["filter"] = filter
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", f"/{message_name}/reactions", params=params)
        err = self._error_result(data, "Failed to list reactions")
        if err:
            return err
        reactions = (data or {}).get("reactions") or []
        return {
            "success": True,
            "response": f"Found {len(reactions)} reaction(s).",
            "reactions": reactions,
            "count": len(reactions),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    # ==================================================================
    # MESSAGE PINS
    # ==================================================================

    @tool(
        description="Pin a message in a Google Chat space so it stays accessible at the top of the space.",
        params={"message_name": "Resource name of the message to pin, e.g. 'spaces/AAAAAAAAAAA/messages/BBBBBBBB'."},
    )
    async def create_message_pin(self, message_name: str) -> Dict:
        message_name = self._normalize_name(message_name, "spaces/")
        if "/messages/" not in message_name:
            return {"success": False, "response": "message_name must be a full message resource name."}
        space_name = message_name.split("/messages/")[0]
        body = {"message": {"name": message_name}}
        data = await self._chat_request("POST", f"/{space_name}/messagePins", json_body=body)
        err = self._error_result(data, "Failed to pin message")
        if err:
            return err
        return {"success": True, "response": "Message pinned.", "messagePin": data}

    @tool(
        description="Remove a pin from a Google Chat message.",
        params={
            "name": (
                "Resource name of the message pin to remove, e.g. "
                "'spaces/AAAAAAAAAAA/messagePins/BBBBBBBB' (from list_message_pins)."
            )
        },
    )
    async def delete_message_pin(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to remove message pin")
        if err:
            return err
        return {"success": True, "response": "Message pin removed."}

    @tool(
        description="List pinned messages in a Google Chat space.",
        params={
            "space_name": "Resource name of the space, e.g. 'spaces/AAAAAAAAAAA'.",
            "page_size": "Maximum number of message pins to return (default 25, max 1000).",
            "page_token": "Token from a previous list_message_pins call to fetch the next page.",
        },
    )
    async def list_message_pins(
        self, space_name: str, page_size: int = DEFAULT_PAGE_SIZE, page_token: Optional[str] = None
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", f"/{space_name}/messagePins", params=params)
        err = self._error_result(data, "Failed to list message pins")
        if err:
            return err
        pins = (data or {}).get("messagePins") or []
        return {
            "success": True,
            "response": f"Found {len(pins)} pinned message(s).",
            "messagePins": pins,
            "count": len(pins),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    # ==================================================================
    # SPACE EVENTS
    # ==================================================================

    @tool(
        description=(
            "Get details about a single Google Chat space event "
            "(e.g. a message, membership, or reaction change event)."
        ),
        params={"name": "Resource name of the space event, e.g. 'spaces/AAAAAAAAAAA/spaceEvents/CCCCCCCC'."},
    )
    async def get_space_event(self, name: str) -> Dict:
        name = self._normalize_name(name, "spaces/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get space event")
        if err:
            return err
        return {"success": True, "response": "Space event retrieved.", "spaceEvent": data}

    @tool(
        description=(
            "List events from a Google Chat space within a time range, such as new messages, "
            "membership changes, or reactions. Useful for detecting recent activity in a space."
        ),
        params={
            "space_name": "Resource name of the space, e.g. 'spaces/AAAAAAAAAAA'.",
            "filter": (
                "Required filter specifying event types and time range, e.g. "
                "\"event_types:\\\"google.workspace.chat.message.v1.created\\\" AND "
                "start_time=\\\"2026-01-01T00:00:00Z\\\" AND end_time=\\\"2026-01-02T00:00:00Z\\\"\". "
                "See the Chat API SpaceEvents.list reference for supported event types."
            ),
            "page_size": "Maximum number of events to return (default 25, max 1000).",
            "page_token": "Token from a previous list_space_events call to fetch the next page.",
        },
    )
    async def list_space_events(
        self, space_name: str, filter: str, page_size: int = DEFAULT_PAGE_SIZE, page_token: Optional[str] = None
    ) -> Dict:
        space_name = self._normalize_name(space_name, "spaces/")
        params: Dict[str, Any] = {
            "filter": filter,
            "pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await self._chat_request("GET", f"/{space_name}/spaceEvents", params=params)
        err = self._error_result(data, "Failed to list space events")
        if err:
            return err
        events = (data or {}).get("spaceEvents") or []
        return {
            "success": True,
            "response": f"Found {len(events)} space event(s).",
            "spaceEvents": events,
            "count": len(events),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    # ==================================================================
    # CUSTOM EMOJIS
    # ==================================================================

    @tool(
        description="List custom emojis visible to the authenticated user in Google Chat.",
        params={
            "page_size": "Maximum number of custom emojis to return (default 25, max 1000).",
            "page_token": "Token from a previous list_custom_emojis call to fetch the next page.",
            "filter": "Optional filter, e.g. \"creator.name = \\\"users/{user}\\\"\" to list emojis created by a user.",
        },
    )
    async def list_custom_emojis(
        self, page_size: int = DEFAULT_PAGE_SIZE, page_token: Optional[str] = None, filter: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"pageSize": min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)}
        if page_token:
            params["pageToken"] = page_token
        if filter:
            params["filter"] = filter
        data = await self._chat_request("GET", "/customEmojis", params=params)
        err = self._error_result(data, "Failed to list custom emojis")
        if err:
            return err
        emojis = (data or {}).get("customEmojis") or []
        return {
            "success": True,
            "response": f"Found {len(emojis)} custom emoji(s).",
            "customEmojis": emojis,
            "count": len(emojis),
            "nextPageToken": (data or {}).get("nextPageToken"),
        }

    @tool(
        description="Get details about a single custom emoji in Google Chat.",
        params={"name": "Resource name of the custom emoji, e.g. 'customEmojis/AAAAAAAAAAA'."},
    )
    async def get_custom_emoji(self, name: str) -> Dict:
        name = self._normalize_name(name, "customEmojis/")
        data = await self._chat_request("GET", f"/{name}")
        err = self._error_result(data, "Failed to get custom emoji")
        if err:
            return err
        return {"success": True, "response": "Custom emoji retrieved.", "customEmoji": data}

    @tool(
        description=(
            "Create a custom emoji in Google Chat from an image. Provide either a publicly "
            "accessible image_url to fetch, or raw base64-encoded image_content."
        ),
        params={
            "emoji_name": (
                "Shortcode-style name for the emoji, lowercase letters/numbers/underscores/hyphens "
                "between colons, e.g. ':my_emoji:'."
            ),
            "image_url": "Optional URL to download the emoji image from (PNG, JPEG, or GIF, max 256KB).",
            "image_content_base64": "Optional base64-encoded image content (used if image_url is not provided).",
            "content_type": "MIME type of the image, e.g. 'image/png'.",
        },
    )
    async def create_custom_emoji(
        self,
        emoji_name: str,
        image_url: Optional[str] = None,
        image_content_base64: Optional[str] = None,
        content_type: str = "image/png",
    ) -> Dict:
        image_bytes: Optional[bytes] = None
        if image_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    resp = await hc.get(image_url)
                resp.raise_for_status()
                image_bytes = resp.content
            except Exception as e:
                return {"success": False, "response": f"Failed to download image_url: {e}"}
        elif image_content_base64:
            try:
                image_bytes = base64.b64decode(image_content_base64)
            except Exception as e:
                return {"success": False, "response": f"Invalid image_content_base64: {e}"}
        else:
            return {"success": False, "response": "Provide either image_url or image_content_base64."}

        files = {
            "metadata": (None, json.dumps({"emojiName": emoji_name}), "application/json"),
            "file": ("emoji", image_bytes, content_type),
        }
        data = await self._chat_request("POST", "/customEmojis", files=files)
        err = self._error_result(data, "Failed to create custom emoji")
        if err:
            return err
        return {"success": True, "response": f"Created custom emoji '{emoji_name}'.", "customEmoji": data}

    @tool(
        description="Delete a custom emoji from Google Chat. Only the creator can delete their own custom emoji.",
        params={"name": "Resource name of the custom emoji to delete, e.g. 'customEmojis/AAAAAAAAAAA'."},
    )
    async def delete_custom_emoji(self, name: str) -> Dict:
        name = self._normalize_name(name, "customEmojis/")
        data = await self._chat_request("DELETE", f"/{name}")
        err = self._error_result(data, "Failed to delete custom emoji")
        if err:
            return err
        return {"success": True, "response": "Custom emoji deleted."}
