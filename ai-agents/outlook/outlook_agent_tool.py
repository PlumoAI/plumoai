from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "content": content,
    }


class OutlookAgentTool(ConnectedServiceToolAgent):
    TOOL_DESCRIPTION = """Outlook Email Agent: read, search, send, and reply to Microsoft Outlook emails.
Use when the user wants to check inbox messages, search mail, open a message, send a new email, reply to a message, or inspect Outlook folders.

ACTIONS:
- list_messages: list recent emails from a folder
- search_messages: search mailbox by text
- read_message: fetch one message by id
- send_message: send a new email
- reply_message: reply to an existing message by id
- list_folders: list available mail folders

Preferred tool_args:
- action
- message_id
- folder_id
- folder_name
- query
- to
- cc
- bcc
- subject
- body
- top
"""

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

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def initialize(self) -> None:
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    async def _graph_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        retry_401: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        url = path if path.startswith("http") else f"{GRAPH_API_BASE}{path}"
        request_headers = dict(extra_headers or {})
        response = await self._httpx_client.request(method, url, params=params, json=json_body, headers=request_headers or None)
        if response.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._graph_request(
                method,
                path,
                params=params,
                json_body=json_body,
                extra_headers=extra_headers,
                retry_401=False,
            )
        if response.status_code >= 400:
            logger.warning(
                "Outlook Graph %s %s -> %s %s",
                method,
                path,
                response.status_code,
                (response.text or "")[:500],
            )
            return None
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return None

    async def _list_folders(self) -> List[Dict[str, Any]]:
        data = await self._graph_request(
            "GET",
            "/me/mailFolders",
            params={"$top": 100, "$select": "id,displayName,parentFolderId,childFolderCount,totalItemCount,unreadItemCount"},
        )
        return data.get("value", []) if data else []

    async def _resolve_folder_id(self, folder_id: Optional[str], folder_name: Optional[str]) -> str:
        if folder_id:
            return str(folder_id)
        wanted = (folder_name or "").strip().lower()
        if not wanted:
            return "inbox"
        for folder in await self._list_folders():
            if (folder.get("displayName") or "").strip().lower() == wanted:
                return str(folder.get("id") or "inbox")
        return "inbox"

    async def _list_messages(
        self,
        *,
        folder_id: str,
        top: int = DEFAULT_PAGE_SIZE,
        search_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(top or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        params: Dict[str, Any] = {
            "$top": limit,
            "$orderby": "receivedDateTime DESC",
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,webLink,parentFolderId",
        }
        if search_query:
            params["$search"] = f'"{search_query}"'
        endpoint = f"/me/mailFolders/{folder_id}/messages"
        data = await self._graph_request("GET", endpoint, params=params)
        return data or {}

    async def _search_messages(self, *, search_query: str, top: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
        limit = max(1, min(int(top or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        data = await self._graph_request(
            "GET",
            "/me/messages",
            params={
                "$top": limit,
                "$search": f'"{search_query}"',
                "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,isRead,webLink,parentFolderId",
            },
            extra_headers={"ConsistencyLevel": "eventual"},
        )
        return data or {}

    async def _get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        return await self._graph_request(
            "GET",
            f"/me/messages/{message_id}",
            params={
                "$select": "id,subject,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime,sentDateTime,bodyPreview,body,isRead,webLink,parentFolderId,conversationId,replyTo",
            },
        )

    async def _send_message(self, payload: Dict[str, Any]) -> bool:
        result = await self._graph_request("POST", "/me/sendMail", json_body={"message": payload, "saveToSentItems": True})
        return result is not None

    async def _reply_message(self, message_id: str, comment: str) -> bool:
        result = await self._graph_request("POST", f"/me/messages/{message_id}/reply", json_body={"comment": comment})
        return result is not None

    async def _llm_generate(self, prompt: str, max_tokens: int = 400) -> Optional[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return None
        try:
            generated = self.llm_provider.generate(prompt, max_tokens=max_tokens)
            if generated is None:
                return None
            if hasattr(generated, "__aiter__"):
                output = ""
                async for chunk in generated:
                    if isinstance(chunk, dict) and "text" in chunk:
                        output += chunk.get("text", "")
                    elif isinstance(chunk, str):
                        output += chunk
                return output.strip() or None
            if isinstance(generated, str):
                return generated.strip() or None
        except Exception as exc:
            logger.debug("Outlook LLM parsing failed: %s", exc)
        return None

    async def _infer_action_with_llm(self, user_query: str) -> Optional[Dict[str, Any]]:
        prompt = f"""You convert Outlook email requests into JSON.
Supported actions: list_messages, search_messages, read_message, send_message, reply_message, list_folders.

Return one JSON object only. Examples:
{{"action":"list_messages","params":{{"folder_name":"Inbox","top":10}}}}
{{"action":"search_messages","params":{{"query":"invoice from John","top":10}}}}
{{"action":"send_message","params":{{"to":["user@example.com"],"subject":"Status update","body":"Hello ..."}}}}
{{"action":"reply_message","params":{{"message_id":"abc123","body":"Thanks, I will handle it."}}}}

User request: {user_query}
JSON:
"""
        raw = await self._llm_generate(prompt, max_tokens=300)
        if not raw:
            return None
        raw = raw.strip().strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("action"):
            return parsed
        return None

    def _normalize_email_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [part.strip() for part in value.replace(";", ",").split(",")]
            return [part for part in parts if "@" in part]
        if isinstance(value, list):
            emails: List[str] = []
            for item in value:
                emails.extend(self._normalize_email_list(item))
            return emails
        return []

    def _recipient_objects(self, value: Any) -> List[Dict[str, Dict[str, str]]]:
        return [{"emailAddress": {"address": email}} for email in self._normalize_email_list(value)]

    def _message_summary(self, message: Dict[str, Any]) -> Dict[str, Any]:
        sender = (((message.get("from") or {}).get("emailAddress") or {}).get("address"))
        to_addresses = [
            ((recipient.get("emailAddress") or {}).get("address"))
            for recipient in (message.get("toRecipients") or [])
            if isinstance(recipient, dict)
        ]
        cc_addresses = [
            ((recipient.get("emailAddress") or {}).get("address"))
            for recipient in (message.get("ccRecipients") or [])
            if isinstance(recipient, dict)
        ]
        return {
            "id": message.get("id"),
            "subject": message.get("subject") or "(No subject)",
            "from": sender,
            "to": [address for address in to_addresses if address],
            "cc": [address for address in cc_addresses if address],
            "received_at": message.get("receivedDateTime"),
            "is_read": bool(message.get("isRead")),
            "preview": message.get("bodyPreview"),
            "web_link": message.get("webLink"),
            "folder_id": message.get("parentFolderId"),
            "conversation_id": message.get("conversationId"),
        }

    def _need_discovery(self, parameter: str, reason: str, original_query: str) -> Dict[str, Any]:
        return {
            "success": False,
            "need_discovery": True,
            "missing_info": [
                {
                    "parameter": parameter,
                    "reason": reason,
                    "original_query": original_query,
                }
            ],
        }

    async def _decide_action(
        self,
        user_query: str,
        tool_args: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        tool_args = tool_args or {}
        action = (tool_args.get("action") or tool_args.get("step_action") or "").strip().lower()
        if action:
            return {"action": action, "params": dict(tool_args)}
        if tool_args.get("message_id"):
            return {"action": "read_message", "params": dict(tool_args)}
        if tool_args.get("to") and (tool_args.get("subject") or tool_args.get("body")):
            return {"action": "send_message", "params": dict(tool_args)}
        if tool_args.get("query"):
            return {"action": "search_messages", "params": dict(tool_args)}

        q = (user_query or "").strip().lower()
        if not q:
            return {"action": "list_messages", "params": {"folder_name": "Inbox", "top": DEFAULT_PAGE_SIZE}}
        if "folder" in q:
            return {"action": "list_folders", "params": {}}
        if any(word in q for word in ("send", "email to", "mail to", "compose")):
            parsed = await self._infer_action_with_llm(user_query)
            if parsed:
                return parsed
        if any(word in q for word in ("reply", "respond")):
            parsed = await self._infer_action_with_llm(user_query)
            if parsed:
                return parsed
            return {"action": "reply_message", "params": {}}
        if any(word in q for word in ("read", "open", "show message", "message details")):
            return {"action": "read_message", "params": dict(tool_args)}
        if any(word in q for word in ("search", "find", "look for")):
            return {"action": "search_messages", "params": {"query": user_query, "top": DEFAULT_PAGE_SIZE}}
        return {"action": "list_messages", "params": {"folder_name": "Inbox", "top": DEFAULT_PAGE_SIZE}}

    async def _handle_list_folders(self) -> Dict[str, Any]:
        folders = await self._list_folders()
        items = [
            {
                "id": folder.get("id"),
                "display_name": folder.get("displayName"),
                "unread_count": folder.get("unreadItemCount"),
                "total_count": folder.get("totalItemCount"),
            }
            for folder in folders
        ]
        return {
            "success": True,
            "response": f"Found {len(items)} Outlook folder(s).",
            "result": {"items": items, "count": len(items)},
        }

    async def _handle_list_messages(self, params: Dict[str, Any]) -> Dict[str, Any]:
        folder_id = await self._resolve_folder_id(params.get("folder_id"), params.get("folder_name"))
        data = await self._list_messages(folder_id=folder_id, top=params.get("top") or DEFAULT_PAGE_SIZE)
        messages = [self._message_summary(message) for message in data.get("value", [])]
        return {
            "success": True,
            "response": f"Found {len(messages)} message(s).",
            "result": {
                "items": messages,
                "count": len(messages),
                "next_page_token": data.get("@odata.nextLink"),
                "folder_id": folder_id,
            },
        }

    async def _handle_search_messages(self, params: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        query = (params.get("query") or params.get("search_query") or user_query or "").strip()
        if not query:
            result = self._need_discovery("query", "No mailbox search text was provided.", user_query)
            result["response"] = "I need a search phrase to find Outlook messages."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        data = await self._search_messages(search_query=query, top=params.get("top") or DEFAULT_PAGE_SIZE)
        messages = [self._message_summary(message) for message in data.get("value", [])]
        return {
            "success": True,
            "response": f"Found {len(messages)} matching message(s).",
            "result": {
                "items": messages,
                "count": len(messages),
                "next_page_token": data.get("@odata.nextLink"),
                "query": query,
            },
        }

    async def _handle_read_message(self, params: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        message_id = params.get("message_id") or params.get("id")
        if not message_id:
            result = self._need_discovery("message_id", "A specific Outlook message id is required.", user_query)
            result["response"] = "I need a message id to open that Outlook email."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        message = await self._get_message(str(message_id))
        if not message:
            return {"success": False, "response": "Could not retrieve that Outlook message.", "result": {"message_id": message_id}}
        detail = self._message_summary(message)
        detail["body"] = ((message.get("body") or {}).get("content"))
        detail["body_content_type"] = ((message.get("body") or {}).get("contentType"))
        detail["reply_to"] = [
            ((recipient.get("emailAddress") or {}).get("address"))
            for recipient in (message.get("replyTo") or [])
            if isinstance(recipient, dict)
        ]
        detail["sent_at"] = message.get("sentDateTime")
        return {"success": True, "response": f"Loaded message: {detail['subject']}.", "result": detail}

    async def _handle_send_message(self, params: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        to = self._recipient_objects(params.get("to"))
        subject = str(params.get("subject") or "").strip()
        body = str(params.get("body") or "").strip()
        if not to:
            result = self._need_discovery("to", "A recipient email address is required to send mail.", user_query)
            result["response"] = "I need at least one recipient to send the Outlook email."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        if not subject or not body:
            result = self._need_discovery("subject/body", "Both subject and body are required to send mail.", user_query)
            result["response"] = "I need both a subject and body to send the Outlook email."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        payload = {
            "subject": subject,
            "body": {
                "contentType": "HTML" if str(params.get("body_content_type") or "").upper() == "HTML" else "Text",
                "content": body,
            },
            "toRecipients": to,
        }
        cc = self._recipient_objects(params.get("cc"))
        bcc = self._recipient_objects(params.get("bcc"))
        if cc:
            payload["ccRecipients"] = cc
        if bcc:
            payload["bccRecipients"] = bcc
        success = await self._send_message(payload)
        if not success:
            return {"success": False, "response": "Could not send the Outlook email.", "result": {"subject": subject}}
        return {
            "success": True,
            "response": f"Email sent: {subject}.",
            "result": {
                "subject": subject,
                "to": [item["emailAddress"]["address"] for item in to],
                "cc": [item["emailAddress"]["address"] for item in cc],
                "bcc": [item["emailAddress"]["address"] for item in bcc],
            },
        }

    async def _handle_reply_message(self, params: Dict[str, Any], user_query: str) -> Dict[str, Any]:
        message_id = params.get("message_id") or params.get("id")
        body = str(params.get("body") or params.get("comment") or "").strip()
        if not message_id:
            result = self._need_discovery("message_id", "Replying requires an Outlook message id.", user_query)
            result["response"] = "I need the target message id before I can reply."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        if not body:
            result = self._need_discovery("body", "Replying requires the reply text.", user_query)
            result["response"] = "I need the reply body before I can send the Outlook response."
            result["result"] = {
                "success": False,
                "need_discovery": True,
                "missing_info": result["missing_info"],
            }
            return result
        success = await self._reply_message(str(message_id), body)
        if not success:
            return {"success": False, "response": "Could not send the Outlook reply.", "result": {"message_id": message_id}}
        return {
            "success": True,
            "response": "Reply sent.",
            "result": {"message_id": message_id, "body": body},
        }

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            if not self.access_token:
                result = {
                    "success": False,
                    "response": "Microsoft Outlook is not connected. Connect the Microsoft provider in the UI and try again.",
                    "result": {"success": False, "error": "Outlook not connected"},
                }
                yield event(AgentEvent.RESULT, result)
                yield event(AgentEvent.FINAL, result)
                return

            yield event(AgentEvent.THOUGHT, "Understanding your Outlook email request.")
            decision = await self._decide_action(user_query, tool_args)
            action = (decision.get("action") or "list_messages").strip().lower()
            params = decision.get("params") or {}
            yield event(AgentEvent.PLAN, f"Running Outlook action: {action}.")

            if action == "list_folders":
                result = await self._handle_list_folders()
            elif action == "list_messages":
                result = await self._handle_list_messages(params)
            elif action == "search_messages":
                result = await self._handle_search_messages(params, user_query)
            elif action == "read_message":
                result = await self._handle_read_message(params, user_query)
            elif action == "send_message":
                result = await self._handle_send_message(params, user_query)
            elif action == "reply_message":
                result = await self._handle_reply_message(params, user_query)
            else:
                result = {
                    "success": False,
                    "response": f"Unsupported Outlook action: {action}.",
                    "result": {"action": action},
                }

            yield event(AgentEvent.RESULT, result)
            yield event(
                AgentEvent.FINAL,
                {
                    "success": result.get("success", False),
                    "response": json.dumps(result, default=str),
                    "result": result.get("result", result),
                },
            )
        except Exception as exc:
            logger.exception("Outlook agent run error: %s", exc)
            error_result = {"success": False, "response": str(exc), "result": {"success": False}}
            yield event(AgentEvent.ERROR, error_result)
            yield event(AgentEvent.FINAL, error_result)
