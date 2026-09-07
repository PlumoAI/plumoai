from __future__ import annotations

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

"""
Web Search Agent Tool (app_code: websearch)

Runs a full research loop:
  1. Expand the user query into 3-5 focused sub-queries (1 LLM call)
  2. Search each sub-query via DuckDuckGo JSON API (parallel, no key required)
  3. Fetch the top pages (parallel, deduplicated, httpx)
  4. Synthesize a cited answer from the page excerpts (1 LLM call)

Falls back to the LLM provider's native web search when DuckDuckGo returns no results.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

_MAX_SUB_QUERIES = 5
_MAX_TOTAL_URLS = 10
_PAGE_CHAR_LIMIT = 2000
_FETCH_TIMEOUT = 8.0
_DDG_URL = "https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1"

_EXPAND_SYSTEM = """You are a search query strategist. Given a user question, produce 3-5 focused
search sub-queries that together will retrieve all the information needed to answer it fully.

Return ONLY a JSON array of strings, no markdown, no explanation.
Example: ["query one", "query two", "query three"]"""

_SYNTHESIS_SYSTEM = """You are a research assistant. Answer the user's question using ONLY the
provided web page excerpts below. For every claim, add an inline citation like [1] or [2].
At the end, include a "Sources:" section listing each cited number and its URL.
If the excerpts do not contain enough information, say so honestly — do not hallucinate."""

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


def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


class WebSearchAgentTool(BaseToolAgent):
    """
    Web Search / Research agent.

    Short queries (single intent) → native LLM web search (fast path).
    Research queries (multi-faceted) → full research loop: expand → search → fetch → synthesize.
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
- In-depth research requiring multiple sources

Input: natural language question or search query. Accepts JSON from planner: {"query":"..."} or query=...
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

    # ------------------------------------------------------------------
    # Query extraction
    # ------------------------------------------------------------------

    def _extract_search_query(self, user_query: str, tool_args: Optional[Dict[str, Any]]) -> str:
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

    # ------------------------------------------------------------------
    # Research loop helpers
    # ------------------------------------------------------------------

    async def _expand_queries(self, query: str) -> List[str]:
        """Ask LLM to generate focused sub-queries. Falls back to [query] on failure."""
        try:
            prompt = f"User question: {query}\n\nGenerate search sub-queries:"
            raw = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt=_EXPAND_SYSTEM,
                max_tokens=300,
                temperature=0.3,
            )
            raw = (raw or "").strip()
            # strip markdown fences if present
            raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                sub_queries = [str(q).strip() for q in parsed if str(q).strip()]
                return sub_queries[:_MAX_SUB_QUERIES] or [query]
        except Exception:
            logger.debug("Query expansion failed, using original query")
        return [query]

    async def _ddg_search(self, client: httpx.AsyncClient, sub_query: str) -> List[Tuple[str, str]]:
        """Search DuckDuckGo and return [(url, snippet), ...]."""
        try:
            url = _DDG_URL.format(q=quote_plus(sub_query))
            resp = await client.get(url, timeout=_FETCH_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            results: List[Tuple[str, str]] = []
            # AbstractURL is often the most relevant single result
            if data.get("AbstractURL") and data.get("AbstractText"):
                results.append((data["AbstractURL"], data["AbstractText"]))
            for topic in data.get("RelatedTopics", []):
                if isinstance(topic, dict) and topic.get("FirstURL") and topic.get("Text"):
                    results.append((topic["FirstURL"], topic["Text"]))
                # some topics are grouped
                for sub in topic.get("Topics", []):
                    if isinstance(sub, dict) and sub.get("FirstURL") and sub.get("Text"):
                        results.append((sub["FirstURL"], sub["Text"]))
            return results[:3]
        except Exception as e:
            logger.debug("DDG search failed for %r: %s", sub_query, e)
            return []

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> str:
        """Fetch a page and return stripped text excerpt."""
        try:
            resp = await client.get(
                url,
                timeout=_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchBot/1.0)"},
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" not in ct and "text" not in ct:
                return ""
            return _strip_html(resp.text)[:_PAGE_CHAR_LIMIT]
        except Exception as e:
            logger.debug("Page fetch failed for %s: %s", url, e)
            return ""

    async def _synthesize(self, query: str, excerpts: List[Tuple[str, str]]) -> Tuple[str, List[Dict]]:
        """Synthesize cited answer from excerpts. Returns (answer_text, citations_list)."""
        numbered = "\n\n".join(
            f"[{i+1}] Source: {url}\n{text}" for i, (url, text) in enumerate(excerpts)
        )
        prompt = f"User question: {query}\n\n--- Web excerpts ---\n{numbered}\n\nAnswer:"
        try:
            answer = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt=_SYNTHESIS_SYSTEM,
                max_tokens=1500,
                temperature=0.3,
            )
            answer = (answer or "").strip()
        except Exception as e:
            logger.warning("Synthesis LLM call failed: %s", e)
            answer = "\n\n".join(f"[{i+1}] {text}" for i, (_, text) in enumerate(excerpts))

        citations = [{"index": i + 1, "url": url} for i, (url, _) in enumerate(excerpts)]
        return answer, citations

    # ------------------------------------------------------------------
    # Full research loop
    # ------------------------------------------------------------------

    async def _run_research_loop(self, query: str) -> AsyncGenerator[Dict, None]:
        yield event("thought", f"Expanding query into sub-queries for: {query[:80]}")

        sub_queries = await self._expand_queries(query)
        yield event("plan", sub_queries)

        async with httpx.AsyncClient() as client:
            # parallel DuckDuckGo searches
            search_tasks = [self._ddg_search(client, q) for q in sub_queries]
            search_results_per_query = await asyncio.gather(*search_tasks)

            # deduplicate URLs, preserve order
            seen_urls: set = set()
            url_snippet_pairs: List[Tuple[str, str]] = []
            for sub_q, results in zip(sub_queries, search_results_per_query):
                count = 0
                for url, snippet in results:
                    if url not in seen_urls and len(url_snippet_pairs) < _MAX_TOTAL_URLS:
                        seen_urls.add(url)
                        url_snippet_pairs.append((url, snippet))
                        count += 1
                yield event("tool_call", {"query": sub_q, "results_count": count})

            if not url_snippet_pairs:
                # no DDG results — fall through to native LLM web search
                return

            # parallel page fetches
            fetch_tasks = [self._fetch_page(client, url) for url, _ in url_snippet_pairs]
            page_texts = await asyncio.gather(*fetch_tasks)

        # build excerpts: prefer fetched page text, fall back to DDG snippet
        excerpts: List[Tuple[str, str]] = []
        for (url, snippet), page_text in zip(url_snippet_pairs, page_texts):
            text = page_text if page_text else snippet
            if text:
                excerpts.append((url, text[:_PAGE_CHAR_LIMIT]))
                yield event("observation", {"url": url, "excerpt": text[:200] + "..."})

        if not excerpts:
            return

        yield event("thought", f"Synthesizing answer from {len(excerpts)} sources...")
        answer, citations = await self._synthesize(query, excerpts)

        yield event("result", {
            "success": True,
            "query": query,
            "result": answer,
            "response": answer,
            "citations": citations,
        })
        yield event("final", {
            "success": True,
            "response": answer,
            "result": {"answer": answer, "citations": citations},
            "citations": citations,
        })

    # ------------------------------------------------------------------
    # Native LLM web search fallback (original behaviour)
    # ------------------------------------------------------------------

    async def _run_native_search(self, query: str) -> AsyncGenerator[Dict, None]:
        yield event("thought", f"Searching the web for: {query[:80]}...")

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

    # ------------------------------------------------------------------
    # Public run()
    # ------------------------------------------------------------------

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict, None]:
        try:
            query = self._extract_search_query(user_query, tool_args)
            if not query:
                query = (user_query or "").strip()[:500]
            if not query:
                out = {"success": False, "error": "No search query provided", "result": None}
                yield event("result", out)
                yield event("final", {"success": False, "error": out["error"], "response": None})
                return

            if not self.llm_provider or not getattr(self.llm_provider, "get_response", None):
                out = {"success": False, "error": "LLM provider not configured", "result": None}
                yield event("result", out)
                yield event("final", {"success": False, "error": out["error"], "response": None})
                return

            # Try the full research loop first
            emitted_result = False
            async for ev in self._run_research_loop(query):
                yield ev
                if ev["type"] in ("result", "final") and ev.get("content", {}).get("success"):
                    emitted_result = True

            # If research loop produced nothing useful, fall back to native LLM web search
            if not emitted_result:
                yield event("thought", "No web results found via search API — trying native web search...")
                async for ev in self._run_native_search(query):
                    yield ev

        except Exception as e:
            logger.exception("Web search agent failed")
            yield event("error", {"error": str(e)})
            yield event("final", {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("Web Search agent initialized")

    async def cleanup(self) -> None:
        logger.debug("Web Search agent cleaned up")
