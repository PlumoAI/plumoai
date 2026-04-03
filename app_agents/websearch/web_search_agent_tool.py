from __future__ import annotations

from backend.services.app_agents.base_tool_agent import BaseToolAgent

"""
Web Search Agent Tool (app_code: websearch)

Built-in tool that performs web searches when modelConfig.enableWebSearching is true.
Uses the connected LLM provider (same as the main agent) - no 3rd party search API.
When enable_web_search is true, the provider (e.g. OpenRouter) uses its web plugin
so the model can search the web during generation.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)

WEB_SEARCH_SYSTEM_PROMPT = """You have web search enabled. The user needs real-time information from the internet.

Search the web for the user's question and provide a clear, accurate, concise answer.
- Cite sources when available (URLs or site names)
- If you cannot find relevant information, say so clearly
- Keep the response focused and useful for the user
- Use the same language as the user's question when appropriate"""


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "content": content,
    }


class WebSearchAgentTool(BaseToolAgent):
    """
    Web Search agent: uses the connected LLM provider with web search enabled.
    The provider (e.g. OpenRouter with plugins: [{"id": "web"}]) lets the model
    search the web during generation. No 3rd party search API.
    """

    TOOL_NAME = "Web Search"
    APP_CODE = "websearch"

    TOOL_DESCRIPTION = """Web Search: search the internet for current information, facts, news, and real-time data.

Use when the user asks about:
- Current events, news, or recent happenings
- Facts or information that may have changed
- Real-time data (weather, stock prices, sports scores)
- Topics not in your training data or knowledge base
- Verification of facts or latest updates

Input: natural language question or search query. Accepts JSON from planner: {"query":"..."} or query=...
The tool uses the connected LLM with web search to find and synthesize an answer for the user.
"""

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

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def get_description(self) -> str:
        return self.get_tool_responsibility()

    def _extract_search_query(self, user_query: str, tool_args: Optional[Dict[str, Any]]) -> str:
        """Extract search query from user_query or tool_args."""
        query = ""
        if tool_args and isinstance(tool_args.get("query"), str):
            query = (tool_args.get("query") or "").strip()
        if not query and user_query:
            s = user_query.strip()
            if s.startswith("{") and "}" in s:
                try:
                    obj = json.loads(s[:2000])
                    if isinstance(obj, dict) and obj.get("query"):
                        query = str(obj.get("query", "")).strip()
                except json.JSONDecodeError:
                    pass
            if not query:
                query = s[:500]
        return query or ""

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Run web search using the connected LLM provider.
        The provider must have enable_web_search=True (from modelConfig.enableWebSearching).
        """
        try:
            query = self._extract_search_query(user_query, tool_args)
            if not query:
                query = (user_query or "").strip()[:500]
            if not query:
                out = {"success": False, "error": "No search query provided", "result": None}
                yield event("result", out)
                yield event("final", {"success": False, "error": out["error"], "response": None})
                return

            yield event("thought", f"Searching the web for: {query[:80]}...")

            if not self.llm_provider or not getattr(self.llm_provider, "get_response", None):
                out = {"success": False, "error": "LLM provider not configured for web search", "result": None}
                yield event("result", out)
                yield event("final", {"success": False, "error": out["error"], "response": None})
                return

            citations_list: List[Dict[str, Any]] = []
            response = await self.llm_provider.get_response(
                transcript=query,
                system_prompt=WEB_SEARCH_SYSTEM_PROMPT,
                max_tokens=1500,
                temperature=0.3,
                use_web_search=True,
                citations=citations_list,
            )

            if not response or not str(response).strip():
                out = {"success": False, "error": "No response from web search", "result": None}
                yield event("result", out)
                yield event("final", {"success": False, "error": out["error"], "response": None})
                return

            out = {
                "success": True,
                "query": query,
                "result": response.strip(),
                "response": response.strip(),
                "citations": citations_list,
            }
            yield event("result", out)
            yield event("final", {
                "success": True,
                "response": response.strip(),
                "result": out,
                "citations": citations_list,
            })

        except Exception as e:
            logger.exception("Web search agent failed")
            yield event("error", {"error": str(e)})
            yield event("final", {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("Web Search agent initialized")

    async def cleanup(self) -> None:
        logger.debug("Web Search agent cleaned up")

