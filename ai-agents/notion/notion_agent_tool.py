"""
Notion Agent Tool for PlumoAI.

Manage pages, databases, blocks, users, comments, and search across a Notion workspace.
Uses Notion API v2022-06-28: https://api.notion.com/v1/

Design:
- Single intent pipeline: tool_args -> provided_data -> one LLM call -> action + params.
- API layer: pages, databases, blocks, users, comments, search.
- Handlers: search, list_pages, create_page, get_page, update_page, archive_page,
  list_databases, create_database, query_database, get_database, update_database,
  list_block_children, append_block_children, get_block, update_block, delete_block,
  list_users, get_current_user, list_comments, create_comment.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

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
            return ss[:6] + "..." + ss[-4:]
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
            return value[:600] + "...<truncated>..." + value[-120:]
        if len(value.strip()) >= 48:
            return _mask_str(value)
        return value
    return value


def _format_page(page: Dict) -> Dict:
    """Extract key fields from a Notion page object."""
    props = page.get("properties", {})
    title = ""
    for prop_name, prop_val in props.items():
        if prop_val.get("type") == "title":
            title_parts = prop_val.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts)
            break
    return {
        "id": page.get("id", ""),
        "title": title,
        "url": page.get("url", ""),
        "created_time": page.get("created_time", ""),
        "last_edited_time": page.get("last_edited_time", ""),
        "archived": page.get("archived", False),
        "parent_type": (page.get("parent") or {}).get("type", ""),
    }


def _format_database(db: Dict) -> Dict:
    """Extract key fields from a Notion database object."""
    title_parts = db.get("title", [])
    title = "".join(t.get("plain_text", "") for t in title_parts)
    return {
        "id": db.get("id", ""),
        "title": title,
        "url": db.get("url", ""),
        "created_time": db.get("created_time", ""),
        "last_edited_time": db.get("last_edited_time", ""),
        "archived": db.get("archived", False),
        "properties": list((db.get("properties") or {}).keys()),
    }


def _format_block(block: Dict) -> Dict:
    """Extract key fields from a Notion block object."""
    block_type = block.get("type", "")
    content = {}
    if block_type and block_type in block:
        type_data = block[block_type]
        if isinstance(type_data, dict):
            rich_text = type_data.get("rich_text", [])
            if rich_text:
                content["text"] = "".join(t.get("plain_text", "") for t in rich_text)
            if "url" in type_data:
                content["url"] = type_data["url"]
            if "checked" in type_data:
                content["checked"] = type_data["checked"]
    return {
        "id": block.get("id", ""),
        "type": block_type,
        "has_children": block.get("has_children", False),
        "archived": block.get("archived", False),
        "created_time": block.get("created_time", ""),
        "content": content,
    }


def _format_user(user: Dict) -> Dict:
    """Extract key fields from a Notion user object."""
    return {
        "id": user.get("id", ""),
        "name": user.get("name", ""),
        "type": user.get("type", ""),
        "avatar_url": user.get("avatar_url", ""),
        "email": (user.get("person") or {}).get("email", ""),
    }


def _format_comment(comment: Dict) -> Dict:
    """Extract key fields from a Notion comment object."""
    rich_text = comment.get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich_text)
    return {
        "id": comment.get("id", ""),
        "text": text,
        "created_time": comment.get("created_time", ""),
        "created_by": (comment.get("created_by") or {}).get("id", ""),
        "parent_type": (comment.get("parent") or {}).get("type", ""),
    }


class NotionAgentTool(ConnectedServiceToolAgent):
    """
    Notion workspace agent. Capabilities:
    - Pages: list, create, get, update, archive, search.
    - Databases: list, create, query, get, update.
    - Blocks: list children, append children, get, update, delete.
    - Users: list, get current user.
    - Comments: list, create.
    - Search: search across workspace.
    """

    TOOL_NAME = "Notion"
    TOOL_DESCRIPTION = """Notion AI Agent: manage pages, databases, blocks, users, and comments in a Notion workspace.

USE WHEN: user mentions Notion, page, database, block, workspace, wiki, document, knowledge base, or content management.

ACTIONS: search, list_pages, create_page, get_page, update_page, archive_page, list_databases, create_database, query_database, get_database, update_database, list_block_children, append_block_children, get_block, update_block, delete_block, list_users, get_current_user, list_comments, create_comment."""

    ACTION_DESCRIPTIONS = (
        "search=search across workspace pages and databases; "
        "list_pages=list pages accessible to the integration; "
        "create_page=create a new page in a parent page or database; "
        "get_page=retrieve a page by ID; "
        "update_page=update page properties; "
        "archive_page=archive (soft-delete) a page; "
        "list_databases=list databases accessible to the integration; "
        "create_database=create a new database in a parent page; "
        "query_database=query a database with optional filter and sort; "
        "get_database=retrieve a database by ID; "
        "update_database=update database title or properties; "
        "list_block_children=list child blocks of a block or page; "
        "append_block_children=append new blocks to a page or block; "
        "get_block=retrieve a block by ID; "
        "update_block=update a block; "
        "delete_block=delete (archive) a block; "
        "list_users=list all users in the workspace; "
        "get_current_user=get the bot user (current integration); "
        "list_comments=list comments on a block or page; "
        "create_comment=add a comment to a page or discussion"
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

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
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
            logger.debug("Notion LLM generate failed: %s", e)
        return None

    # ----- Notion API layer -----
    async def _notion_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Optional[Dict]:
        url = f"{NOTION_API_BASE}/{path.lstrip('/')}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0)
        headers = self._headers()

        if method.upper() == "GET":
            r = await self._httpx_client.request(method, url, params=params, headers=headers)
        else:
            r = await self._httpx_client.request(method, url, json=json_body or {}, headers=headers)

        if r.status_code == 401 and retry_401 and await self._refresh_access_token_if_needed():
            return await self._notion_request(method, path, json_body=json_body, params=params, retry_401=False)
        if r.status_code >= 400:
            logger.warning("Notion API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            try:
                err_data = r.json()
                return {"success": False, "error": err_data.get("message", r.text[:200]), "status": r.status_code}
            except Exception:
                return {"success": False, "error": r.text[:200], "status": r.status_code}
        try:
            return r.json()
        except Exception:
            return None

    # ----- Search -----
    async def _search(self, query: str = "", filter_type: Optional[str] = None, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict]:
        body: Dict[str, Any] = {"page_size": min(page_size, MAX_PAGE_SIZE)}
        if query:
            body["query"] = query
        if filter_type in ("page", "database"):
            body["filter"] = {"value": filter_type, "property": "object"}
        body["sort"] = {"direction": "descending", "timestamp": "last_edited_time"}
        data = await self._notion_request("POST", "search", json_body=body)
        if not data or "results" not in data:
            return []
        results = []
        for item in data.get("results", []):
            obj_type = item.get("object", "")
            if obj_type == "page":
                results.append(_format_page(item))
            elif obj_type == "database":
                results.append(_format_database(item))
            else:
                results.append({"id": item.get("id", ""), "object": obj_type})
        return results

    # ----- Page operations -----
    async def _list_pages(self, query: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict]:
        return await self._search(query=query, filter_type="page", page_size=page_size)

    async def _create_page(self, parent: Dict, properties: Dict, children: Optional[List[Dict]] = None) -> Optional[Dict]:
        body: Dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        data = await self._notion_request("POST", "pages", json_body=body)
        if not data or data.get("success") is False:
            return data
        return _format_page(data)

    async def _get_page(self, page_id: str) -> Optional[Dict]:
        data = await self._notion_request("GET", f"pages/{page_id}")
        if not data or data.get("success") is False:
            return data
        return _format_page(data)

    async def _update_page(self, page_id: str, properties: Dict) -> Optional[Dict]:
        data = await self._notion_request("PATCH", f"pages/{page_id}", json_body={"properties": properties})
        if not data or data.get("success") is False:
            return data
        return _format_page(data)

    async def _archive_page(self, page_id: str) -> Optional[Dict]:
        data = await self._notion_request("PATCH", f"pages/{page_id}", json_body={"archived": True})
        if not data or data.get("success") is False:
            return data
        return _format_page(data)

    # ----- Database operations -----
    async def _list_databases(self, query: str = "", page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict]:
        return await self._search(query=query, filter_type="database", page_size=page_size)

    async def _create_database(self, parent: Dict, title: List[Dict], properties: Dict) -> Optional[Dict]:
        body: Dict[str, Any] = {"parent": parent, "title": title, "properties": properties}
        data = await self._notion_request("POST", "databases", json_body=body)
        if not data or data.get("success") is False:
            return data
        return _format_database(data)

    async def _query_database(
        self,
        database_id: str,
        filter_obj: Optional[Dict] = None,
        sorts: Optional[List[Dict]] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        start_cursor: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"page_size": min(page_size, MAX_PAGE_SIZE)}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = await self._notion_request("POST", f"databases/{database_id}/query", json_body=body)
        if not data or data.get("success") is False:
            return data or {"success": False, "error": "Query failed"}
        results = [_format_page(item) for item in data.get("results", [])]
        return {
            "results": results,
            "has_more": data.get("has_more", False),
            "next_cursor": data.get("next_cursor"),
            "count": len(results),
        }

    async def _get_database(self, database_id: str) -> Optional[Dict]:
        data = await self._notion_request("GET", f"databases/{database_id}")
        if not data or data.get("success") is False:
            return data
        return _format_database(data)

    async def _update_database(self, database_id: str, title: Optional[List[Dict]] = None, properties: Optional[Dict] = None) -> Optional[Dict]:
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if properties is not None:
            body["properties"] = properties
        data = await self._notion_request("PATCH", f"databases/{database_id}", json_body=body)
        if not data or data.get("success") is False:
            return data
        return _format_database(data)

    # ----- Block operations -----
    async def _list_block_children(self, block_id: str, page_size: int = DEFAULT_PAGE_SIZE, start_cursor: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"page_size": min(page_size, MAX_PAGE_SIZE)}
        if start_cursor:
            params["start_cursor"] = start_cursor
        data = await self._notion_request("GET", f"blocks/{block_id}/children", params=params)
        if not data or data.get("success") is False:
            return data or {"success": False, "error": "Failed to list blocks"}
        results = [_format_block(b) for b in data.get("results", [])]
        return {
            "results": results,
            "has_more": data.get("has_more", False),
            "next_cursor": data.get("next_cursor"),
            "count": len(results),
        }

    async def _append_block_children(self, block_id: str, children: List[Dict]) -> Optional[Dict]:
        data = await self._notion_request("PATCH", f"blocks/{block_id}/children", json_body={"children": children})
        if not data or data.get("success") is False:
            return data
        results = [_format_block(b) for b in data.get("results", [])]
        return {"results": results, "count": len(results)}

    async def _get_block(self, block_id: str) -> Optional[Dict]:
        data = await self._notion_request("GET", f"blocks/{block_id}")
        if not data or data.get("success") is False:
            return data
        return _format_block(data)

    async def _update_block(self, block_id: str, block_data: Dict) -> Optional[Dict]:
        data = await self._notion_request("PATCH", f"blocks/{block_id}", json_body=block_data)
        if not data or data.get("success") is False:
            return data
        return _format_block(data)

    async def _delete_block(self, block_id: str) -> Optional[Dict]:
        data = await self._notion_request("DELETE", f"blocks/{block_id}")
        if not data or data.get("success") is False:
            return data
        return _format_block(data)

    # ----- User operations -----
    async def _list_users(self, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict]:
        params: Dict[str, Any] = {"page_size": min(page_size, MAX_PAGE_SIZE)}
        data = await self._notion_request("GET", "users", params=params)
        if not data or "results" not in data:
            return []
        return [_format_user(u) for u in data.get("results", [])]

    async def _get_current_user(self) -> Optional[Dict]:
        data = await self._notion_request("GET", "users/me")
        if not data or data.get("success") is False:
            return data
        return _format_user(data)

    # ----- Comment operations -----
    async def _list_comments(self, block_id: str, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict]:
        params: Dict[str, Any] = {"block_id": block_id, "page_size": min(page_size, MAX_PAGE_SIZE)}
        data = await self._notion_request("GET", "comments", params=params)
        if not data or "results" not in data:
            return []
        return [_format_comment(c) for c in data.get("results", [])]

    async def _create_comment(self, parent: Dict, rich_text: List[Dict], discussion_id: Optional[str] = None) -> Optional[Dict]:
        body: Dict[str, Any] = {"parent": parent, "rich_text": rich_text}
        if discussion_id:
            body["discussion_id"] = discussion_id
        data = await self._notion_request("POST", "comments", json_body=body)
        if not data or data.get("success") is False:
            return data
        return _format_comment(data)

    # ----- Intent pipeline -----
    async def _decide_action(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if tool_args and isinstance(tool_args, dict):
            if tool_args.get("action"):
                action = str(tool_args["action"]).strip().lower()
                params = dict(tool_args)
                params.pop("action", None)
                params.pop("step_action", None)
                return {"action": action, "params": params}

        result = await self._decide_action_with_llm(user_query, provided_data, tool_args=tool_args)
        if result:
            return result

        return {"action": "search", "params": {"query": (user_query or "")[:200]}}

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
                        {k: v for k, v in item.items() if k in ("page_id", "database_id", "block_id", "title", "query", "text")}
                    ))
        if step_action:
            context_parts.append("Step/context: " + step_action)
        context = " | ".join(context_parts) if context_parts else ""

        prompt = f"""You are a Notion assistant. Output exactly one JSON object. No markdown, no explanation.

ACTION DESCRIPTIONS:
{self.ACTION_DESCRIPTIONS}

JSON keys (use exactly):
- "action": one of the actions listed above
- "query": search query text (for search/list_pages/list_databases)
- "page_id": page ID (for get_page/update_page/archive_page)
- "database_id": database ID (for query_database/get_database/update_database)
- "block_id": block ID (for list_block_children/append_block_children/get_block/update_block/delete_block/list_comments)
- "parent": parent object e.g. {{"type":"page_id","page_id":"..."}} or {{"type":"database_id","database_id":"..."}}
- "properties": properties object (for create_page/update_page/create_database/update_database)
- "title": title text (for create_page/create_database)
- "children": array of block objects (for create_page/append_block_children)
- "filter": filter object (for query_database)
- "sorts": sorts array (for query_database)
- "block_data": block update data (for update_block)
- "rich_text": rich text array (for create_comment)
- "discussion_id": discussion thread ID (for create_comment)
- "page_size": number of results (default 20)
- "filter_type": "page" or "database" (for search)

Context:
{context}

User request:
{(user_query or "").strip()[:800]}

JSON:"""
        out = await self._llm_generate_text(prompt, max_tokens=600)
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
            action = (data.get("action") or "search").lower()
            valid_actions = (
                "search", "list_pages", "create_page", "get_page", "update_page", "archive_page",
                "list_databases", "create_database", "query_database", "get_database", "update_database",
                "list_block_children", "append_block_children", "get_block", "update_block", "delete_block",
                "list_users", "get_current_user",
                "list_comments", "create_comment",
            )
            if action not in valid_actions:
                action = "search"
            params = {k: v for k, v in data.items() if k != "action" and v is not None}
            return {"action": action, "params": params}
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    # ----- Handler dispatch -----
    async def _execute_action(self, action: str, params: Dict) -> Dict:
        try:
            if action == "search":
                query = params.get("query", "")
                results = await self._search(
                    query=query,
                    filter_type=params.get("filter_type"),
                    page_size=params.get("page_size", DEFAULT_PAGE_SIZE),
                )
                return {"success": True, "action": action, "query": query, "results": results, "count": len(results)}

            elif action == "list_pages":
                results = await self._list_pages(
                    query=params.get("query", ""),
                    page_size=params.get("page_size", DEFAULT_PAGE_SIZE),
                )
                return {"success": True, "action": action, "pages": results, "count": len(results)}

            elif action == "create_page":
                parent = params.get("parent")
                properties = params.get("properties", {})
                title = params.get("title", "")
                if not parent:
                    return {"success": False, "response": "Parent (page_id or database_id) is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["parent"]}
                # If a simple title string is provided, build title property
                if title and not properties:
                    properties = {"title": {"title": [{"text": {"content": title}}]}}
                children = params.get("children")
                result = await self._create_page(parent, properties, children=children)
                if result and result.get("id"):
                    return {"success": True, "action": action, "page": result,
                            "response": f"Page created: {result.get('title', result.get('id', ''))}"}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to create page: {error}"}

            elif action == "get_page":
                page_id = params.get("page_id", "")
                if not page_id:
                    return {"success": False, "response": "page_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["page_id"]}
                result = await self._get_page(page_id)
                if result and result.get("id"):
                    return {"success": True, "action": action, "page": result}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to get page: {error}"}

            elif action == "update_page":
                page_id = params.get("page_id", "")
                properties = params.get("properties", {})
                if not page_id:
                    return {"success": False, "response": "page_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["page_id"]}
                if not properties:
                    return {"success": False, "response": "properties to update are required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["properties"]}
                result = await self._update_page(page_id, properties)
                if result and result.get("id"):
                    return {"success": True, "action": action, "page": result, "response": "Page updated."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to update page: {error}"}

            elif action == "archive_page":
                page_id = params.get("page_id", "")
                if not page_id:
                    return {"success": False, "response": "page_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["page_id"]}
                result = await self._archive_page(page_id)
                if result and result.get("id"):
                    return {"success": True, "action": action, "page": result, "response": "Page archived."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to archive page: {error}"}

            elif action == "list_databases":
                results = await self._list_databases(
                    query=params.get("query", ""),
                    page_size=params.get("page_size", DEFAULT_PAGE_SIZE),
                )
                return {"success": True, "action": action, "databases": results, "count": len(results)}

            elif action == "create_database":
                parent = params.get("parent")
                title = params.get("title", "")
                properties = params.get("properties", {})
                if not parent:
                    return {"success": False, "response": "Parent page_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["parent"]}
                title_obj = [{"text": {"content": title}}] if isinstance(title, str) else title
                if not properties:
                    properties = {"Name": {"title": {}}}
                result = await self._create_database(parent, title_obj, properties)
                if result and result.get("id"):
                    return {"success": True, "action": action, "database": result,
                            "response": f"Database created: {result.get('title', result.get('id', ''))}"}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to create database: {error}"}

            elif action == "query_database":
                database_id = params.get("database_id", "")
                if not database_id:
                    return {"success": False, "response": "database_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["database_id"]}
                result = await self._query_database(
                    database_id,
                    filter_obj=params.get("filter"),
                    sorts=params.get("sorts"),
                    page_size=params.get("page_size", DEFAULT_PAGE_SIZE),
                    start_cursor=params.get("start_cursor"),
                )
                if isinstance(result, dict) and "results" in result:
                    return {"success": True, "action": action, "database_id": database_id, **result}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to query database: {error}"}

            elif action == "get_database":
                database_id = params.get("database_id", "")
                if not database_id:
                    return {"success": False, "response": "database_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["database_id"]}
                result = await self._get_database(database_id)
                if result and result.get("id"):
                    return {"success": True, "action": action, "database": result}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to get database: {error}"}

            elif action == "update_database":
                database_id = params.get("database_id", "")
                if not database_id:
                    return {"success": False, "response": "database_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["database_id"]}
                title = params.get("title")
                title_obj = [{"text": {"content": title}}] if isinstance(title, str) else title
                result = await self._update_database(database_id, title=title_obj, properties=params.get("properties"))
                if result and result.get("id"):
                    return {"success": True, "action": action, "database": result, "response": "Database updated."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to update database: {error}"}

            elif action == "list_block_children":
                block_id = params.get("block_id", "")
                if not block_id:
                    return {"success": False, "response": "block_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["block_id"]}
                result = await self._list_block_children(
                    block_id,
                    page_size=params.get("page_size", DEFAULT_PAGE_SIZE),
                    start_cursor=params.get("start_cursor"),
                )
                if isinstance(result, dict) and "results" in result:
                    return {"success": True, "action": action, "block_id": block_id, **result}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to list blocks: {error}"}

            elif action == "append_block_children":
                block_id = params.get("block_id", "")
                children = params.get("children", [])
                if not block_id:
                    return {"success": False, "response": "block_id is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["block_id"]}
                if not children:
                    return {"success": False, "response": "children blocks are required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["children"]}
                result = await self._append_block_children(block_id, children)
                if result and "results" in result:
                    return {"success": True, "action": action, "block_id": block_id, **result,
                            "response": f"Appended {result.get('count', 0)} blocks."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to append blocks: {error}"}

            elif action == "get_block":
                block_id = params.get("block_id", "")
                if not block_id:
                    return {"success": False, "response": "block_id is required."}
                result = await self._get_block(block_id)
                if result and result.get("id"):
                    return {"success": True, "action": action, "block": result}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to get block: {error}"}

            elif action == "update_block":
                block_id = params.get("block_id", "")
                block_data = params.get("block_data", {})
                if not block_id:
                    return {"success": False, "response": "block_id is required."}
                if not block_data:
                    return {"success": False, "response": "block_data is required."}
                result = await self._update_block(block_id, block_data)
                if result and result.get("id"):
                    return {"success": True, "action": action, "block": result, "response": "Block updated."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to update block: {error}"}

            elif action == "delete_block":
                block_id = params.get("block_id", "")
                if not block_id:
                    return {"success": False, "response": "block_id is required."}
                result = await self._delete_block(block_id)
                if result and result.get("id"):
                    return {"success": True, "action": action, "block": result, "response": "Block deleted."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to delete block: {error}"}

            elif action == "list_users":
                users = await self._list_users(page_size=params.get("page_size", DEFAULT_PAGE_SIZE))
                return {"success": True, "action": action, "users": users, "count": len(users)}

            elif action == "get_current_user":
                user = await self._get_current_user()
                if user and user.get("id"):
                    return {"success": True, "action": action, "user": user}
                error = (user or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to get current user: {error}"}

            elif action == "list_comments":
                block_id = params.get("block_id", "")
                if not block_id:
                    return {"success": False, "response": "block_id (or page_id) is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["block_id"]}
                comments = await self._list_comments(block_id, page_size=params.get("page_size", DEFAULT_PAGE_SIZE))
                return {"success": True, "action": action, "comments": comments, "count": len(comments)}

            elif action == "create_comment":
                parent = params.get("parent")
                rich_text = params.get("rich_text", [])
                text = params.get("text", "")
                if not parent:
                    return {"success": False, "response": "parent is required (page_id).",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["parent"]}
                # If simple text provided, build rich_text
                if text and not rich_text:
                    rich_text = [{"text": {"content": text}}]
                if not rich_text:
                    return {"success": False, "response": "Comment text (rich_text or text) is required.",
                            "execution_issue": True, "need_discovery": True, "missing_fields": ["rich_text"]}
                result = await self._create_comment(parent, rich_text, discussion_id=params.get("discussion_id"))
                if result and result.get("id"):
                    return {"success": True, "action": action, "comment": result, "response": "Comment created."}
                error = (result or {}).get("error", "unknown")
                return {"success": False, "response": f"Failed to create comment: {error}"}

            else:
                return {"success": False, "response": f"Unknown action: {action}"}

        except Exception as e:
            logger.exception("Notion action %s failed: %s", action, e)
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
            yield event(AgentEvent.THOUGHT, f"Processing Notion request: {(user_query or '')[:200]}")

            decision = await self._decide_action(user_query, provided_data, tool_args=tool_args)
            action = decision.get("action", "search")
            params = decision.get("params", {})

            yield event(AgentEvent.PLAN, f"Action: {action}, params: {json.dumps(_redact_secrets_for_log(params), default=str)[:500]}")

            result = await self._execute_action(action, params)

            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, result)

        except Exception as e:
            logger.exception("Notion agent execute failed: %s", e)
            error_result = {"success": False, "response": f"Notion agent error: {str(e)}"}
            yield event(AgentEvent.ERROR, error_result)
            yield event(AgentEvent.FINAL, error_result)
