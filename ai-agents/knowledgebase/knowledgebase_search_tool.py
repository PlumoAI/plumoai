from __future__ import annotations

from backend.services.app_agents.base_tool_agent import BaseToolAgent

"""
Knowledgebase Search Tool
Enables agents to search company and employee knowledgebase using semantic search.

Autonomy features:
  - Auto-detects query complexity and upgrades search_depth without LLM involvement
  - Document routing: pre-filters irrelevant docs by keyword overlap before searching
  - Self-assesses result quality and auto-escalates normal→deep on poor results
  - Reformulates failed queries using LLM before reporting failure
  - Iterative gap-filling: detects unanswered aspects and searches for them
  - Re-ranks chunks by blending semantic similarity with keyword overlap
  - Emits a confidence score so the orchestrator can act on result quality
  - Session-level cache avoids redundant API calls for repeated queries
"""

import os
import json
import re
import logging
import asyncio
import hashlib
import httpx
import uuid
from typing import Dict, Any, Optional, List, AsyncGenerator, Set, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

COMPANY_URL = os.getenv("COMPANY_URL")

# ── Thresholds ────────────────────────────────────────────────────────────── #
QUALITY_THRESHOLD = 38.0       # min top-chunk relevance for "sufficient" result
MIN_RESULT_COUNT = 2           # min chunks for "sufficient" result
RERANK_SEMANTIC_WEIGHT = 0.70
RERANK_KEYWORD_WEIGHT = 0.30

# Stop words excluded from document routing keyword matching
_STOP_WORDS: Set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "about", "against", "between", "into",
    "during", "before", "after", "above", "below", "up", "down", "out",
    "off", "over", "under", "again", "then", "once", "and", "but", "or",
    "nor", "so", "yet", "both", "either", "neither", "not", "only",
    "own", "same", "than", "too", "very", "just", "what", "which", "who",
    "whom", "how", "when", "where", "why", "all", "each", "every",
    "more", "most", "other", "some", "such", "no", "if", "me", "my",
    "i", "we", "our", "you", "your", "he", "she", "it", "they", "their",
    "this", "that", "these", "those", "tell", "give", "show", "explain",
    "describe", "get", "find", "know", "want", "please",
}


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    RESULT = "result"
    ERROR = "error"
    FINAL = "final"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content,
    }


# ── Query complexity heuristics ───────────────────────────────────────────── #
_DEEP_PATTERNS = re.compile(
    r"\b(everything|all about|explain all|summarize|overview|compare|difference|"
    r"how does|how do|what are all|list all|tell me all|full details|in detail|"
    r"comprehensive|complete guide|walkthrough)\b",
    re.IGNORECASE,
)


def _auto_detect_depth(query: str) -> str:
    if _DEEP_PATTERNS.search(query):
        return "deep"
    if query.count("?") > 1:
        return "deep"
    if len(query.split()) >= 10:
        return "deep"
    return "normal"


# ── Quality assessment ────────────────────────────────────────────────────── #
def _assess_quality(chunks: List[Dict]) -> Tuple[bool, float, float]:
    """Returns (is_sufficient, max_relevance, confidence_0_to_1)."""
    if not chunks:
        return False, 0.0, 0.0
    scores = sorted([c.get("relevance_score", 0.0) for c in chunks], reverse=True)
    max_rel = scores[0]
    top3_avg = sum(scores[:3]) / len(scores[:3])
    confidence = round(top3_avg / 100.0, 3)
    sufficient = max_rel >= QUALITY_THRESHOLD and len(chunks) >= MIN_RESULT_COUNT
    return sufficient, max_rel, confidence


# ── Re-ranking ────────────────────────────────────────────────────────────── #
def _rerank(chunks: List[Dict], query: str) -> List[Dict]:
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    if not query_words:
        return chunks
    scored: List[Tuple[float, Dict]] = []
    for chunk in chunks:
        text = (chunk.get("chunk_text") or "").lower()
        heading = (chunk.get("heading") or "").lower()
        keywords = (chunk.get("keywords") or "").lower()
        combined = set(re.findall(r"\w+", f"{text} {heading} {keywords}"))
        overlap = len(query_words & combined) / max(len(query_words), 1)
        blended = (
            chunk.get("relevance_score", 0.0) * RERANK_SEMANTIC_WEIGHT
            + overlap * 100.0 * RERANK_KEYWORD_WEIGHT
        )
        scored.append((blended, chunk))
    return [c for _, c in sorted(scored, key=lambda x: x[0], reverse=True)]


# ── Document routing ─────────────────────────────────────────────────────── #
def _route_documents(
    query: str,
    all_sources: List[Dict],
    min_docs: int = 5,
) -> List[Any]:
    """
    Score each source document by keyword overlap between the query and the
    document title. Returns document IDs of the most relevant subset.

    Falls back to all document IDs when fewer than min_docs score above zero,
    so we never accidentally exclude the only relevant document.
    """
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    if not query_words:
        return [s["agent_knowledgebase_id"] for s in all_sources if s.get("agent_knowledgebase_id") is not None]

    scored: List[Tuple[float, Any]] = []
    for src in all_sources:
        doc_id = src.get("agent_knowledgebase_id")
        if doc_id is None:
            continue
        title_words = set(re.findall(r"\w+", (src.get("title") or "").lower()))
        score = len(query_words & title_words)
        scored.append((score, doc_id))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Keep all docs that have at least 1 keyword match
    matched = [doc_id for score, doc_id in scored if score > 0]

    if len(matched) < min_docs:
        # Not enough matches — use all docs (safe fallback)
        logger.debug(f"Document routing: only {len(matched)} match(es), falling back to all {len(scored)} docs")
        return [doc_id for _, doc_id in scored]

    logger.info(f"Document routing: narrowed {len(scored)} docs → {len(matched)} relevant for query '{query[:55]}'")
    return matched


class KnowledgebaseSearchTool(BaseToolAgent):
    """
    Self-driven knowledgebase search tool.

    Autonomy loop (fully internal — caller just passes a natural-language query):

      1.  Auto-detect depth from query structure
      2.  Session cache check
      3.  Document routing — narrow candidate docs by keyword overlap
      4.  Run search (normal or deep fan-out)
      5.  Quality check
          ├─ normal + poor  → auto-escalate to deep
          └─ deep  + poor   → reformulate query and retry
      6.  Gap-filling — find unanswered aspects, run targeted follow-up searches
      7.  Re-rank (semantic × keyword blend)
      8.  Emit RESULT with confidence + quality label
    """

    def __init__(
        self,
        token: str,
        company_id: str,
        agent_id: str,
        llm_provider,
        company_sources: Optional[List[Dict]] = None,
        employee_sources: Optional[List[Dict]] = None,
        is_company_enabled: bool = False,
        is_employee_enabled: bool = False,
    ):
        self.token = token
        self.company_id = company_id
        self.agent_id = agent_id
        self.llm_provider = llm_provider
        self.company_sources = company_sources or []
        self.employee_sources = employee_sources or []
        self.is_company_enabled = is_company_enabled
        self.is_employee_enabled = is_employee_enabled

        # Session-level result cache
        self._search_cache: Dict[str, List[Dict]] = {}

        logger.info("📚 Knowledgebase Search Tool initialized")
        logger.info(f"   Company KB: {len(self.company_sources)} sources (enabled: {is_company_enabled})")
        logger.info(f"   Employee KB: {len(self.employee_sources)} sources (enabled: {is_employee_enabled})")

    # ------------------------------------------------------------------ #
    #  LLM tool description                                                #
    # ------------------------------------------------------------------ #

    def get_tool_responsibility(self) -> str:
        doc_list = []
        if self.is_company_enabled and self.company_sources:
            doc_list.append(f"\n📚 Company Knowledge ({len(self.company_sources)} documents):")
            for doc in self.company_sources[:10]:
                doc_list.append(f"  • {doc.get('title', 'Untitled')} ({doc.get('file_type', '').upper()})")
            if len(self.company_sources) > 10:
                doc_list.append(f"  ... and {len(self.company_sources) - 10} more documents")
        if self.is_employee_enabled and self.employee_sources:
            doc_list.append(f"\n📋 Personal Knowledge ({len(self.employee_sources)} documents):")
            for doc in self.employee_sources[:5]:
                doc_list.append(f"  • {doc.get('title', 'Untitled')} ({doc.get('file_type', '').upper()})")
            if len(self.employee_sources) > 5:
                doc_list.append(f"  ... and {len(self.employee_sources) - 5} more documents")

        documents_section = "\n".join(doc_list) if doc_list else "\n(No documents currently available)"

        return f"""🧠 PRIMARY BRAIN MEMORY - Knowledge Base Search

═══════════════════════════════════════════════════════════
⚡ CHECK THIS TOOL FIRST for ANY informational query!
═══════════════════════════════════════════════════════════

This is your PRIMARY MEMORY containing all uploaded documents,
policies, procedures, FAQs, guidelines, and company information.
{documents_section}

🎯 WHEN TO USE (Priority #1):
  ✅ ANY question asking for information, facts, or knowledge
  ✅ Policy questions (HR, break times, working hours, etc.)
  ✅ Procedure and process questions
  ✅ Company guidelines and documentation
  ✅ FAQs and general knowledge queries
  ✅ Historical information stored in documents

⚠️ WHEN NOT TO USE:
  ❌ Mathematical calculations (3+5=?)
  ❌ Creating visualizations/charts
  ❌ Real-time database queries (current sales data)
  ❌ External API calls / file operations

🤖 FULLY AUTONOMOUS — you don't need to manage any of this:
  • Routes your query to the most relevant documents first
  • Auto-detects whether your query needs deep or normal search
  • Escalates to deep search if normal results are insufficient
  • Reformulates the query when nothing relevant is found
  • Detects unanswered aspects and runs follow-up gap searches
  • Re-ranks results by semantic similarity AND keyword relevance
  • Returns a confidence score (0-1) so you know how reliable the answer is

💡 Just pass a natural language query. The tool handles everything else."""

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def initialize(self):
        pass

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def run(
        self,
        query: str,
        search_scope: str = "all",
        max_results: int = 5,
        search_depth: str = "normal",
        project_fid: Optional[int] = None,
    ) -> AsyncGenerator[Dict, None]:
        # ── 1. Auto-detect depth ─────────────────────────────────────── #
        detected_depth = _auto_detect_depth(query)
        if detected_depth == "deep" and search_depth == "normal":
            logger.info(f"🧠 Auto-upgraded depth: normal→deep for '{query[:60]}'")
            yield event(AgentEvent.THOUGHT, "Query complexity detected — automatically using deep search")
            search_depth = "deep"
        else:
            yield event(AgentEvent.THOUGHT, f"Searching knowledgebase: '{query}' (depth={search_depth})")

        if not COMPANY_URL:
            yield event(AgentEvent.ERROR, "Knowledgebase search service not configured")
            return

        if search_scope not in ("all", "company", "employee"):
            search_scope = "all"

        search_company = (search_scope in ("all", "company")) and self.is_company_enabled
        search_employee = (search_scope in ("all", "employee")) and self.is_employee_enabled

        if not search_company and not search_employee:
            yield event(AgentEvent.ERROR, "No knowledgebase sources are enabled")
            return

        # ── 2. Session cache check ───────────────────────────────────── #
        cache_key = self._make_cache_key(query, search_scope, search_depth)
        if cache_key in self._search_cache:
            cached = self._search_cache[cache_key]
            _, _, confidence = _assess_quality(cached)
            logger.info(f"💾 Cache hit: '{query[:55]}' → {len(cached)} chunks")
            yield event(AgentEvent.OBSERVATION, f"Returning {len(cached)} cached result(s) (confidence={confidence})")
            yield event(AgentEvent.RESULT, self._build_result(
                query, cached, search_scope, search_depth, confidence,
                escalated=False, reformulated=False, gap_filled=False, source="cache"
            ))
            return

        # ── 3. Collect all candidate sources ─────────────────────────── #
        all_sources: List[Dict] = []
        if search_company:
            all_sources.extend(self.company_sources)
        if search_employee:
            all_sources.extend(self.employee_sources)

        if not all_sources:
            yield event(AgentEvent.ERROR, "No knowledgebase sources available")
            return

        # ── 4. Document routing ──────────────────────────────────────── #
        document_ids = _route_documents(query, all_sources)
        if not document_ids:
            yield event(AgentEvent.ERROR, "No knowledgebase sources available")
            return

        routed_count = len(document_ids)
        total_count = len(all_sources)
        if routed_count < total_count:
            yield event(AgentEvent.THOUGHT,
                f"Document routing: focusing on {routed_count} of {total_count} documents most relevant to query")

        # ── 5. Autonomy search loop ──────────────────────────────────── #
        async for evt in self._autonomous_search(
            query, document_ids, max_results, search_scope, search_depth, project_fid
        ):
            yield evt

    # ------------------------------------------------------------------ #
    #  Autonomy loop                                                       #
    # ------------------------------------------------------------------ #

    async def _autonomous_search(
        self,
        query: str,
        document_ids: List,
        max_results: int,
        search_scope: str,
        search_depth: str,
        project_fid: Optional[int],
    ) -> AsyncGenerator[Dict, None]:
        escalated = False
        reformulated = False
        gap_filled = False

        yield event(AgentEvent.PLAN,
            f"Running {search_depth} search across {len(document_ids)} document(s)")
        yield event(AgentEvent.TOOL_CALL, {
            "query": query,
            "document_count": len(document_ids),
            "max_results": max_results,
            "search_scope": search_scope,
            "search_depth": search_depth,
        })

        # ── A: initial search ────────────────────────────────────────── #
        try:
            if search_depth == "deep":
                chunks = await self._deep_fetch(query, document_ids, max_results, project_fid)
            else:
                raw = await self._execute_search(query, document_ids, max_results, project_fid)
                chunks = [self._format_chunk(c) for c in (raw or [])]
        except Exception as e:
            yield event(AgentEvent.ERROR, f"Search failed: {e}")
            return

        sufficient, max_rel, confidence = _assess_quality(chunks)
        logger.info(f"📊 Initial — max_rel={max_rel:.1f}% confidence={confidence} sufficient={sufficient}")

        # ── B: auto-escalate normal → deep ──────────────────────────── #
        if not sufficient and search_depth == "normal":
            escalated = True
            yield event(AgentEvent.THOUGHT,
                f"Normal search quality low ({max_rel:.0f}% top relevance) — escalating to deep")
            try:
                deep_chunks = await self._deep_fetch(query, document_ids, max_results, project_fid)
                chunks = self._merge(chunks, deep_chunks)
            except Exception as e:
                logger.warning(f"Auto-escalation failed: {e}")
            sufficient, max_rel, confidence = _assess_quality(chunks)
            logger.info(f"📊 Post-escalation — max_rel={max_rel:.1f}% confidence={confidence}")

        # ── C: reformulate if still poor ────────────────────────────── #
        if not sufficient:
            reformulated = True
            yield event(AgentEvent.THOUGHT,
                f"Results still poor ({max_rel:.0f}%) — reformulating query")
            ref_queries = await self._reformulate_query(query)
            if ref_queries:
                ref_results = await asyncio.gather(
                    *[self._execute_search(q, document_ids, max_results, project_fid) for q in ref_queries],
                    return_exceptions=True,
                )
                for result in ref_results:
                    if isinstance(result, Exception) or result is None:
                        continue
                    chunks = self._merge(chunks, [self._format_chunk(c) for c in result])
            sufficient, max_rel, confidence = _assess_quality(chunks)
            logger.info(f"📊 Post-reformulation — max_rel={max_rel:.1f}% confidence={confidence}")

        # ── D: gap-filling (only when we have partial results) ──────── #
        if chunks and confidence < 0.80:
            gap_queries = await self._find_gaps(query, chunks)
            if gap_queries:
                gap_filled = True
                yield event(AgentEvent.THOUGHT,
                    f"Detected {len(gap_queries)} unanswered aspect(s) — running gap searches")
                gap_results = await asyncio.gather(
                    *[self._execute_search(gq, document_ids, max_results, project_fid) for gq in gap_queries],
                    return_exceptions=True,
                )
                for result in gap_results:
                    if isinstance(result, Exception) or result is None:
                        continue
                    chunks = self._merge(chunks, [self._format_chunk(c) for c in result])
                _, max_rel, confidence = _assess_quality(chunks)
                logger.info(f"📊 Post-gap-fill — max_rel={max_rel:.1f}% confidence={confidence}")

        # ── E: re-rank ───────────────────────────────────────────────── #
        if chunks:
            chunks = _rerank(chunks, query)

        # Cap result set
        cap = max(max_results * 3, 15) if (escalated or search_depth == "deep" or gap_filled) else max_results
        chunks = chunks[:cap]

        # ── F: cache + emit ─────────────────────────────────────────── #
        if chunks:
            self._search_cache[self._make_cache_key(query, search_scope, search_depth)] = chunks

        obs_parts = [f"Found {len(chunks)} relevant chunk(s)"]
        if escalated:
            obs_parts.append("(auto-escalated to deep)")
        if reformulated:
            obs_parts.append("(query reformulated)")
        if gap_filled:
            obs_parts.append("(gap-filled)")
        yield event(AgentEvent.OBSERVATION, " ".join(obs_parts))

        final_depth = "deep" if (search_depth == "deep" or escalated) else "normal"
        yield event(AgentEvent.RESULT, self._build_result(
            query, chunks, search_scope, final_depth, confidence,
            escalated=escalated, reformulated=reformulated, gap_filled=gap_filled
        ))

    # ------------------------------------------------------------------ #
    #  Gap-filling                                                         #
    # ------------------------------------------------------------------ #

    async def _find_gaps(self, query: str, chunks: List[Dict]) -> List[str]:
        """
        Ask the LLM: given what we found so far, what aspects of the original
        question are still not answered? Returns up to 2 targeted search queries
        for those missing aspects. Returns [] if everything is covered or LLM
        is unavailable.
        """
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return []

        # Build a compact summary of found content (titles + headings only — no full text)
        found_summary = "\n".join(
            f"- {c.get('title', 'Untitled')} / {c.get('heading', '') or c.get('section_path', '')}"
            for c in chunks[:10]
        )

        prompt = (
            "You are evaluating knowledgebase search results.\n\n"
            f"Original question: {query}\n\n"
            f"Content found so far (document titles and sections):\n{found_summary}\n\n"
            "Identify up to 2 specific aspects of the original question that are NOT yet "
            "covered by the found content. For each gap, write a short, focused search query.\n"
            "If the question is fully answered, return an empty array.\n"
            "Return ONLY a JSON array of strings — no explanation.\n"
            'Example: ["aspect not covered 1", "aspect not covered 2"]'
        )

        out = await self._llm_call(prompt, max_tokens=150)
        if not out:
            return []

        try:
            match = re.search(r"\[.*?\]", out.strip(), re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    gaps = [str(g).strip() for g in parsed if str(g).strip()]
                    logger.info(f"🔍 Gap queries: {gaps}")
                    return gaps[:2]
        except Exception as e:
            logger.warning(f"Gap detection parse failed: {e}")

        return []

    # ------------------------------------------------------------------ #
    #  Deep fan-out fetch                                                  #
    # ------------------------------------------------------------------ #

    async def _deep_fetch(
        self,
        query: str,
        document_ids: List,
        max_results: int,
        project_fid: Optional[int],
    ) -> List[Dict]:
        sub_queries = await self._decompose_query(query)
        logger.info(f"🔀 Deep fetch: {len(sub_queries)} sub-queries")
        results = await asyncio.gather(
            *[self._execute_search(sq, document_ids, max_results, project_fid) for sq in sub_queries],
            return_exceptions=True,
        )
        chunks: List[Dict] = []
        for result in results:
            if isinstance(result, Exception) or result is None:
                continue
            chunks = self._merge(chunks, [self._format_chunk(c) for c in result])
        return chunks

    # ------------------------------------------------------------------ #
    #  LLM helpers                                                         #
    # ------------------------------------------------------------------ #

    async def _decompose_query(self, query: str) -> List[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return [query]
        prompt = (
            "Decompose the following query into 3 to 4 short, focused sub-queries for "
            "semantic search against a company knowledge base. Each sub-query should target "
            "a distinct aspect. Return ONLY a JSON array of strings.\n\n"
            f"Query: {query}\n\n"
            'Example: ["sub-query 1", "sub-query 2", "sub-query 3"]'
        )
        out = await self._llm_call(prompt, max_tokens=200)
        if not out:
            return [query]
        try:
            match = re.search(r"\[.*?\]", out.strip(), re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    sub_queries = [str(q).strip() for q in parsed if str(q).strip()]
                    if sub_queries:
                        if query not in sub_queries:
                            sub_queries.insert(0, query)
                        return sub_queries[:5]
        except Exception as e:
            logger.warning(f"Query decomposition parse failed: {e}")
        return [query]

    async def _reformulate_query(self, query: str) -> List[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return []
        prompt = (
            "A semantic search returned poor results for the query below. "
            "Generate 3 alternative queries: one broader, one using synonyms, "
            "one keyword-only (no stop words). "
            "Return ONLY a JSON array of 3 strings.\n\n"
            f"Original query: {query}\n\n"
            'Example: ["broader version", "synonym version", "keyword1 keyword2"]'
        )
        out = await self._llm_call(prompt, max_tokens=150)
        if not out:
            return []
        try:
            match = re.search(r"\[.*?\]", out.strip(), re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return [str(q).strip() for q in parsed if str(q).strip()][:3]
        except Exception as e:
            logger.warning(f"Reformulation parse failed: {e}")
        return []

    async def _llm_call(self, prompt: str, max_tokens: int = 200) -> Optional[str]:
        try:
            gen = self.llm_provider.generate(prompt, max_tokens=max_tokens)
            if gen is None:
                return None
            if hasattr(gen, "__aiter__"):
                out = ""
                async for chunk in gen:
                    if isinstance(chunk, dict):
                        out += chunk.get("text", "")
                    elif isinstance(chunk, str):
                        out += chunk
                return out.strip() or None
            if isinstance(gen, str):
                return gen.strip() or None
        except Exception as e:
            logger.warning(f"LLM call failed: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  HTTP search                                                         #
    # ------------------------------------------------------------------ #

    async def _execute_search(
        self,
        query: str,
        document_ids: List,
        limit: int,
        project_fid: Optional[int],
    ) -> Optional[List[Dict]]:
        url = f"{COMPANY_URL}/aiagentchat/knowledgebase/search"
        payload = {"query": query, "document_ids": document_ids, "limit": limit, "project_fid": project_fid}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": f"[{self.company_id}]",
                    },
                    json=payload,
                )
            if response.status_code == 200:
                result = response.json()
                if result.get("type") == "success" and result.get("data"):
                    chunks = result["data"].get("results", [])
                    logger.debug(f"  '{query[:55]}' → {len(chunks)} chunk(s)")
                    return chunks
                return []
            logger.error(f"KB HTTP {response.status_code} for '{query[:55]}': {response.text[:200]}")
            return None
        except httpx.TimeoutException:
            logger.error(f"Timeout for '{query[:55]}'")
            raise
        except httpx.RequestError as e:
            logger.error(f"Request error: {e}")
            raise

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_chunk(chunk: Dict) -> Dict:
        distance = chunk.get("distance", 1)
        relevance_score = max(0.0, min(100.0, round((1 - distance) * 100, 1)))
        return {
            "chunk_id": chunk.get("id"),
            "chunk_index": chunk.get("chunk_index"),
            "chunk_text": chunk.get("chunk_text"),
            "chunk_type": chunk.get("chunk_type"),
            "distance": distance,
            "relevance_score": relevance_score,
            "document_id": chunk.get("document_id"),
            "title": chunk.get("title"),
            "file_type": chunk.get("file_type"),
            "source_path": chunk.get("source_path"),
            "project_fid": chunk.get("project_fid"),
            "section_path": chunk.get("section_path"),
            "heading": chunk.get("heading"),
            "heading_level": chunk.get("heading_level"),
            "keywords": chunk.get("keywords"),
            "parent_id": chunk.get("parent_id"),
            "part_index": chunk.get("part_index"),
            "total_parts": chunk.get("total_parts"),
            "token_count": chunk.get("token_count"),
            "start_position": chunk.get("start_position"),
            "end_position": chunk.get("end_position"),
        }

    @staticmethod
    def _merge(base: List[Dict], additions: List[Dict]) -> List[Dict]:
        seen: Dict[Any, Dict] = {}
        for chunk in base + additions:
            cid = chunk.get("chunk_id")
            if cid is None:
                seen[id(chunk)] = chunk
            elif cid not in seen or chunk["relevance_score"] > seen[cid]["relevance_score"]:
                seen[cid] = chunk
        return list(seen.values())

    @staticmethod
    def _make_cache_key(query: str, scope: str, depth: str) -> str:
        return hashlib.md5(f"{query.strip().lower()}|{scope}|{depth}".encode()).hexdigest()

    @staticmethod
    def _build_result(
        query: str,
        chunks: List[Dict],
        search_scope: str,
        search_depth: str,
        confidence: float,
        escalated: bool,
        reformulated: bool,
        gap_filled: bool,
        source: str = "search",
    ) -> Dict:
        quality = (
            "high" if confidence >= 0.65
            else "medium" if confidence >= 0.35
            else "low"
        )
        return {
            "success": True,
            "query": query,
            "results": chunks,
            "total_results": len(chunks),
            "search_scope": search_scope,
            "search_depth": search_depth,
            "confidence": confidence,
            "quality": quality,
            "escalated": escalated,
            "reformulated": reformulated,
            "gap_filled": gap_filled,
            "source": source,
        }

    # ------------------------------------------------------------------ #
    #  Source detail lookup                                                #
    # ------------------------------------------------------------------ #

    async def get_source_details(self, document_id: int) -> Dict[str, Any]:
        if not COMPANY_URL:
            return {"success": False, "error": "Knowledgebase service not configured"}
        try:
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/{document_id}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "companyIds": f"[{self.company_id}]",
                    },
                )
            if response.status_code == 200:
                result = response.json()
                if result.get("type") == "success" and result.get("data"):
                    return {"success": True, "document": result["data"]}
                return {"success": False, "error": "Document not found"}
            return {"success": False, "error": f"Request failed: {response.status_code}"}
        except Exception as e:
            logger.error(f"Error getting document details: {e}")
            return {"success": False, "error": str(e)}
