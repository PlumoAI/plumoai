from __future__ import annotations

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

"""
Knowledgebase Search Tool  —  self-driven, profile-aware

Autonomy pipeline (all internal — caller only passes a natural-language query):

  1.  Load DocumentProfiles for all sources  →  know doc_type & language per doc
  2.  Auto-detect query depth from query structure
  3.  Session cache check
  4.  Profile-aware document routing:
        • keyword overlap on title  (existing)
        • PLUS doc_type alignment   (new) — if query looks like a "menu" question,
          prefer menu-typed documents; policy queries prefer policy-typed docs
  5.  Build profile-tuned sub-queries:
        • recipe/menu  → short ingredient/item phrases
        • legal/policy → full formal sentences
        • process      → action-verb phrases
        • general      → natural language
  6.  Run normal or deep fan-out search (with doc_type filter in Milvus)
  7.  Quality check → auto-escalate → query reformulation
  8.  Iterative gap-filling
  9.  Re-rank (semantic × keyword blend, page-number proximity bonus)
  10. Emit RESULT with confidence + quality + doc_type distribution
"""

import os
import json
import re
import logging
import asyncio
import hashlib
import httpx
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple, AsyncGenerator
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COMPANY_URL = os.getenv("COMPANY_URL")

# ── Thresholds ────────────────────────────────────────────────────────────── #
QUALITY_THRESHOLD   = 38.0
MIN_RESULT_COUNT    = 2
RERANK_SEMANTIC_W   = 0.65
RERANK_KEYWORD_W    = 0.25
RERANK_PAGE_W       = 0.10   # small bonus for chunks near page 1 (title area)

_STOP_WORDS: Set[str] = {
    "a","an","the","is","are","was","were","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall","can",
    "need","dare","ought","used","to","of","in","for","on","with","at","by","from",
    "as","into","through","about","against","between","during","before","after",
    "above","below","up","down","out","off","over","under","again","then","once",
    "and","but","or","nor","so","yet","both","either","neither","not","only","own",
    "same","than","too","very","just","what","which","who","whom","how","when",
    "where","why","all","each","every","more","most","other","some","such","no",
    "if","me","my","i","we","our","you","your","he","she","it","they","their",
    "this","that","these","those","tell","give","show","explain","describe","get",
    "find","know","want","please",
}

# ── Signals that indicate a query matches a doc_type ──────────────────────── #
# Used to boost documents whose stored doc_type aligns with the query intent.
_DOC_TYPE_QUERY_SIGNALS: Dict[str, List[str]] = {
    "menu":      ["price","dish","serves","starter","main","dessert","order","allergen","vegan","vegetarian","drink","wine","cocktail"],
    "recipe":    ["ingredient","method","cook","bake","prep","serves","tablespoon","teaspoon","grams","oven","flour","butter","sugar"],
    "policy":    ["policy","rule","procedure","compliance","employee","allowed","required","must","shall","guideline","regulation","hr","leave","holiday"],
    "technical": ["install","configure","api","setup","endpoint","error","version","database","server","deploy","auth","parameter"],
    "legal":     ["agreement","contract","clause","liability","obligation","termination","party","parties","jurisdiction","warranty","breach"],
    "letter":    ["dear","sincerely","regards","reference","re:","subject:","writing","attached","application","appointment"],
    "process":   ["step","flow","workflow","checklist","process","sop","trigger","approval","escalation","milestone","stage"],
    "financial": ["invoice","budget","revenue","expense","tax","vat","cost","price","payment","profit","loss","balance","forecast"],
    "report":    ["summary","findings","analysis","recommendation","conclusion","methodology","objective","overview","background"],
}

# ── Sub-query style per doc_type ──────────────────────────────────────────── #
# These guide _decompose_query to produce style-matched sub-queries.
_SUBQUERY_STYLE_HINTS: Dict[str, str] = {
    "menu":      "short dish names, prices, or ingredient phrases — no full sentences",
    "recipe":    "short ingredient or cooking-step phrases — no full sentences",
    "policy":    "full policy question sentences including the rule being asked about",
    "legal":     "precise legal clause keywords plus the topic — formal language",
    "technical": "technical term plus action — e.g. 'configure SSL certificate'",
    "letter":    "key topic phrase or reference number — keep short",
    "process":   "action-verb phrases — e.g. 'submit approval request'",
    "financial": "metric or document name — e.g. 'Q3 revenue budget forecast'",
    "report":    "section or finding phrase — e.g. 'key findings customer satisfaction'",
    "general":   "natural language phrases that capture distinct aspects",
}


class AgentEvent:
    THOUGHT     = "thought"
    PLAN        = "plan"
    TOOL_CALL   = "tool_call"
    OBSERVATION = "observation"
    RESULT      = "result"
    ERROR       = "error"
    FINAL       = "final"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id":        str(uuid.uuid4()),
        "type":      event_type,
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "content":   content,
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


# ── Keyword-based doc_type inference (fast fallback) ─────────────────────── #
def _keyword_infer_query_doc_type(query: str, candidate_types: Optional[List[str]] = None) -> Optional[str]:
    """
    Fast keyword scoring fallback used when the LLM classifier is unavailable.
    Requires score ≥ 2 and clear dominance — intentionally conservative so that
    ambiguous / short queries return None (search all docs) rather than mis-route.
    """
    signals_to_check = {
        k: v for k, v in _DOC_TYPE_QUERY_SIGNALS.items()
        if candidate_types is None or k in candidate_types
    }
    if not signals_to_check:
        return None

    q_words = set(re.findall(r"\w+", query.lower()))
    scores: Dict[str, int] = {
        dtype: sum(1 for s in signals if s in q_words or s in query.lower())
        for dtype, signals in signals_to_check.items()
    }

    best_type, best_score = max(scores.items(), key=lambda x: x[1])
    second_score = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0

    # Only return a preference when the winner is clearly ahead
    if best_score >= 2 and best_score > second_score:
        return best_type
    return None


# ── Quality assessment ────────────────────────────────────────────────────── #
def _assess_quality(chunks: List[Dict]) -> Tuple[bool, float, float]:
    if not chunks:
        return False, 0.0, 0.0
    scores = sorted([c.get("relevance_score", 0.0) for c in chunks], reverse=True)
    max_rel  = scores[0]
    top3_avg = sum(scores[:3]) / len(scores[:3])
    confidence = round(top3_avg / 100.0, 3)
    sufficient = max_rel >= QUALITY_THRESHOLD and len(chunks) >= MIN_RESULT_COUNT
    return sufficient, max_rel, confidence


# ── Re-ranking (semantic + keyword + page proximity) ─────────────────────── #
def _rerank(chunks: List[Dict], query: str) -> List[Dict]:
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    if not query_words:
        return chunks

    scored: List[Tuple[float, Dict]] = []
    for chunk in chunks:
        text     = (chunk.get("chunk_text") or "").lower()
        heading  = (chunk.get("heading") or "").lower()
        keywords = (chunk.get("keywords") or "").lower()
        combined = set(re.findall(r"\w+", f"{text} {heading} {keywords}"))
        overlap  = len(query_words & combined) / max(len(query_words), 1)

        # Slight proximity bonus: chunks from early pages (title/intro) get a boost
        page = chunk.get("page_number") or chunk.get("page") or 0
        page_bonus = max(0.0, 1.0 - (page / 50.0)) if page else 0.5

        blended = (
            chunk.get("relevance_score", 0.0) * RERANK_SEMANTIC_W
            + overlap * 100.0              * RERANK_KEYWORD_W
            + page_bonus * 100.0           * RERANK_PAGE_W
        )
        scored.append((blended, chunk))

    return [c for _, c in sorted(scored, key=lambda x: x[0], reverse=True)]


# ── Profile-aware document routing ───────────────────────────────────────── #
def _route_documents(
    query: str,
    all_sources: List[Dict],
    query_doc_type: Optional[str],
    min_docs: int = 5,
) -> List[Any]:
    """
    Score each source by:
      1. Keyword overlap between query words and document title
      2. doc_type alignment bonus — if the stored doc_type matches the
         query's inferred type, the document gets an extra weight boost

    Falls back to all docs when fewer than min_docs score above zero.
    """
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS

    scored: List[Tuple[float, Any]] = []
    for src in all_sources:
        doc_id = src.get("agent_knowledgebase_id")
        if doc_id is None:
            continue

        title_words = set(re.findall(r"\w+", (src.get("title") or "").lower()))
        keyword_score = len(query_words & title_words) if query_words else 0

        # doc_type alignment bonus (from stored profile, if available)
        stored_type = src.get("doc_type") or ""
        type_bonus = 2.0 if (query_doc_type and stored_type == query_doc_type) else 0.0

        scored.append((keyword_score + type_bonus, doc_id))

    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [doc_id for score, doc_id in scored if score > 0]

    if len(matched) < min_docs:
        logger.debug(f"Document routing: only {len(matched)} match(es) — using all {len(scored)} docs")
        return [doc_id for _, doc_id in scored]

    logger.info(
        f"Document routing: {len(scored)} → {len(matched)} docs "
        f"(query_type={query_doc_type or 'any'})"
    )
    return matched


class KnowledgebaseSearchTool(BaseToolAgent):
    """
    Self-driven, profile-aware knowledgebase search tool.

    Sources may carry a 'doc_type' field loaded from the DocumentProfile
    collection. When present, the tool uses it to:
      • Route queries to type-aligned documents first
      • Produce style-matched sub-queries for deep search
      • Pass doc_type as a Milvus filter for focused retrieval
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
        self.token            = token
        self.company_id       = company_id
        self.agent_id         = agent_id
        self.llm_provider     = llm_provider
        self.company_sources  = company_sources or []
        self.employee_sources = employee_sources or []
        self.is_company_enabled  = is_company_enabled
        self.is_employee_enabled = is_employee_enabled

        self._search_cache: Dict[str, List[Dict]] = {}

        logger.info("📚 Knowledgebase Search Tool initialised")
        logger.info(f"   Company KB : {len(self.company_sources)} source(s) (enabled={is_company_enabled})")
        logger.info(f"   Employee KB: {len(self.employee_sources)} source(s) (enabled={is_employee_enabled})")

        # Log doc_type distribution so we know what kinds of docs are loaded
        all_types = [
            s.get("doc_type", "unknown")
            for s in self.company_sources + self.employee_sources
            if s.get("doc_type")
        ]
        if all_types:
            from collections import Counter
            dist = dict(Counter(all_types).most_common(5))
            logger.info(f"   Doc types  : {dist}")

    # ------------------------------------------------------------------ #
    #  LLM tool description                                                #
    # ------------------------------------------------------------------ #

    def get_tool_responsibility(self) -> str:
        all_sources = self.company_sources + self.employee_sources

        # ── Derive content signal from actual loaded doc_types ────────────
        # No static keyword lists — everything comes from the real documents.
        from collections import Counter
        type_counts = Counter(
            s.get("doc_type", "general")
            for s in all_sources
            if s.get("doc_type") and s.get("doc_type") != "general"
        )
        # Build a natural-language phrase from the actual types present,
        # e.g. "recipe (2), policy (1)" → "recipe, policy content"
        if type_counts:
            type_phrases = ", ".join(
                f"{t} ({n})" if n > 1 else t
                for t, n in type_counts.most_common()
            )
            content_hint = f"{type_phrases} content"
        else:
            content_hint = "all uploaded documents"

        # ── Build document titles list ────────────────────────────────────
        doc_lines = []
        if self.is_company_enabled and self.company_sources:
            doc_lines.append(f"Company ({len(self.company_sources)}):")
            for doc in self.company_sources[:10]:
                dtype = f" [{doc.get('doc_type')}]" if doc.get("doc_type") else ""
                doc_lines.append(f"  • {doc.get('title', 'Untitled')}{dtype}")
            if len(self.company_sources) > 10:
                doc_lines.append(f"  ... +{len(self.company_sources) - 10} more")
        if self.is_employee_enabled and self.employee_sources:
            doc_lines.append(f"Personal ({len(self.employee_sources)}):")
            for doc in self.employee_sources[:8]:
                dtype = f" [{doc.get('doc_type')}]" if doc.get("doc_type") else ""
                doc_lines.append(f"  • {doc.get('title', 'Untitled')}{dtype}")
            if len(self.employee_sources) > 8:
                doc_lines.append(f"  ... +{len(self.employee_sources) - 8} more")

        docs_section = "\n".join(doc_lines) if doc_lines else "(no documents loaded)"
        total = len(all_sources)

        # ── Description — first sentence is the compact planner signal ────
        # _compact_tool_for_planner truncates at 220 chars to the last ". "
        # so the MANDATORY instruction must be complete within that budget.
        # First sentence (~160 chars): MANDATORY mandate with dynamic content_hint.
        # Everything after is detail for the full tool context window.
        return (
            f"MANDATORY: Search this Brain Memory FIRST for ANY question about "
            f"{content_hint}. Never answer from general knowledge when documents "
            f"are available. {total} document(s) loaded — self-driven search "
            f"handles routing, depth, reformulation, and re-ranking automatically.\n\n"
            f"Loaded documents:\n{docs_section}\n\n"
            f"ALWAYS use for: any factual or domain-specific question whose answer "
            f"could be in the uploaded documents.\n"
            f"DO NOT use for: pure arithmetic, real-time live data, or external API calls."
        )

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def initialize(self):
        """
        Load DocumentProfiles for all sources so routing and sub-query
        style are available immediately. Non-fatal on failure.
        """
        await self._load_profiles()

    async def _load_profiles(self):
        """
        Fetch DocumentProfiles from the backend and attach doc_type /
        language to each source dict. Safe to call multiple times.
        """
        if not COMPANY_URL:
            return
        all_ids = [
            src.get("agent_knowledgebase_id")
            for src in self.company_sources + self.employee_sources
            if src.get("agent_knowledgebase_id") is not None
        ]
        if not all_ids:
            return

        try:
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/profiles"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": f"[{self.company_id}]",
                    },
                    json={"document_ids": all_ids},
                )
            if resp.status_code == 200:
                result = resp.json()
                # API returns { type: "success", data: { profiles: [...] } }
                raw_data = result.get("data") or {}
                profile_list = (
                    raw_data.get("profiles")
                    if isinstance(raw_data, dict)
                    else raw_data
                ) or []
                if result.get("type") == "success" and profile_list:
                    profiles: Dict[int, Dict] = {
                        p["document_id"]: p
                        for p in profile_list
                        if isinstance(p, dict) and p.get("document_id")
                    }
                    # Attach to source dicts
                    for src in self.company_sources + self.employee_sources:
                        doc_id = src.get("agent_knowledgebase_id")
                        if doc_id and doc_id in profiles:
                            src["doc_type"] = profiles[doc_id].get("doc_type", "general")
                            src["language"] = profiles[doc_id].get("language", "en")
                    loaded = sum(1 for src in self.company_sources + self.employee_sources if src.get("doc_type"))
                    logger.info(f"📋 Profiles loaded: {loaded}/{len(all_ids)} sources now have doc_type")
        except Exception as e:
            logger.warning(f"Profile loading failed (non-fatal): {e}")

    # ------------------------------------------------------------------ #
    #  LLM-based query → doc_type classifier                              #
    # ------------------------------------------------------------------ #

    async def _classify_query_doc_type(self, query: str, candidate_types: List[str]) -> Optional[str]:
        """
        Classify which doc_type a query is asking about using the LLM as a semantic
        reasoner, then fall back to keyword scoring if the LLM is unavailable or slow.

        Design:
        • Only considers doc_types that are *actually present* in loaded documents —
          no point routing to 'legal' if no legal docs are loaded.
        • Passes document titles alongside type names so the LLM has full context.
        • Returns None when genuinely ambiguous (search all types).
        • Falls back to keyword heuristic on any LLM failure (non-fatal).
        • Results are cached per (query, frozenset(candidate_types)) to avoid
          redundant LLM calls within the same session.
        """
        if not candidate_types:
            return None

        # ── Single candidate — no classification needed ───────────────── #
        if len(candidate_types) == 1:
            return candidate_types[0]

        # ── Session-level cache (keyed by query + type set) ───────────── #
        cache_key = hashlib.md5(
            f"{query.strip().lower()}|{','.join(sorted(candidate_types))}".encode()
        ).hexdigest()
        if not hasattr(self, "_doc_type_cache"):
            self._doc_type_cache: Dict[str, Optional[str]] = {}
        if cache_key in self._doc_type_cache:
            cached = self._doc_type_cache[cache_key]
            logger.debug(f"🗂️ doc_type cache hit: '{cached}' for '{query[:60]}'")
            return cached

        # ── Build per-type document title list for LLM context ─────────── #
        type_doc_map: Dict[str, List[str]] = {}
        for src in self.company_sources + self.employee_sources:
            dt = src.get("doc_type") or "general"
            if dt in candidate_types:
                type_doc_map.setdefault(dt, []).append(src.get("title") or "Untitled")

        type_line_parts = []
        for dt, titles in type_doc_map.items():
            quoted_titles = ", ".join('"' + t + '"' for t in titles[:4])
            suffix = f" (+{len(titles) - 4} more)" if len(titles) > 4 else ""
            type_line_parts.append(f'  "{dt}": {quoted_titles}{suffix}')
        type_lines = "\n".join(type_line_parts)

        prompt = (
            f"You are a document-type classifier for a knowledge base search engine.\n\n"
            f"Available document types and their loaded documents:\n{type_lines}\n\n"
            f"User query: \"{query}\"\n\n"
            f"Which document type does this query most likely need? "
            f"Return ONLY a JSON object with one key:\n"
            f'  {{"doc_type": "<type_name>"}}\n'
            f"Use null if the query is genuinely ambiguous or could span multiple types.\n"
            f"Valid values: {json.dumps(candidate_types + [None])}\n"
            f"No explanation, no markdown — JSON only."
        )

        llm_ran = False
        result: Optional[str] = None

        if self.llm_provider:
            try:
                raw = await self._llm_call(prompt, max_tokens=60)
                if raw:
                    llm_ran = True
                    match = re.search(r'\{.*?\}', raw.strip(), re.DOTALL)
                    if match:
                        parsed = json.loads(match.group())
                        classified = parsed.get("doc_type")
                        if classified in candidate_types:
                            result = classified
                            logger.info(
                                f"🧭 LLM doc_type: '{result}' for '{query[:60]}'"
                            )
                        elif classified is None or str(classified).lower() in ("null", "none", ""):
                            result = None   # Explicitly ambiguous — don't keyword-fallback
                            llm_ran = True  # Signal: LLM said "null" intentionally
                            logger.info(
                                f"🧭 LLM doc_type: ambiguous (null) for '{query[:60]}'"
                            )
                        else:
                            logger.warning(
                                f"🧭 LLM returned unknown doc_type '{classified}' "
                                f"— falling back to keyword heuristic"
                            )
                            llm_ran = False  # Unknown response → fall through to keyword
            except Exception as e:
                logger.warning(f"LLM doc_type classification failed: {e} — using keyword fallback")

        # ── Keyword fallback: only when LLM didn't explicitly say null ─── #
        if not llm_ran:
            kw_result = _keyword_infer_query_doc_type(query, candidate_types)
            if kw_result:
                logger.info(
                    f"🧭 Keyword fallback doc_type: '{kw_result}' for '{query[:60]}'"
                )
                result = kw_result

        # ── Cache and return ──────────────────────────────────────────── #
        self._doc_type_cache[cache_key] = result
        return result

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def run(
        self,
        query: str,
        search_scope: str = "all",
        max_results: int = 10,
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

        search_company  = (search_scope in ("all","company"))  and self.is_company_enabled
        search_employee = (search_scope in ("all","employee")) and self.is_employee_enabled

        if not search_company and not search_employee:
            yield event(AgentEvent.ERROR, "No knowledgebase sources are enabled")
            return

        # ── 2. Cache check ───────────────────────────────────────────── #
        cache_key = self._make_cache_key(query, search_scope, search_depth)
        if cache_key in self._search_cache:
            cached = self._search_cache[cache_key]
            _, _, confidence = _assess_quality(cached)
            yield event(AgentEvent.OBSERVATION, f"Returning {len(cached)} cached result(s) (confidence={confidence})")
            yield event(AgentEvent.RESULT, self._build_result(
                query, cached, search_scope, search_depth, confidence,
                escalated=False, reformulated=False, gap_filled=False, source="cache"
            ))
            return

        # ── 3. Collect sources ───────────────────────────────────────── #
        all_sources: List[Dict] = []
        if search_company:  all_sources.extend(self.company_sources)
        if search_employee: all_sources.extend(self.employee_sources)
        if not all_sources:
            yield event(AgentEvent.ERROR, "No knowledgebase sources available")
            return

        # ── 4. Classify query doc_type (LLM-semantic + keyword fallback) ─ #
        # Derive candidate types only from *actually loaded* documents so the
        # classifier never routes to a type that has no documents behind it.
        loaded_types: List[str] = list({
            src.get("doc_type")
            for src in all_sources
            if src.get("doc_type") and src.get("doc_type") != "general"
        })
        query_doc_type = await self._classify_query_doc_type(query, loaded_types)
        if query_doc_type:
            yield event(AgentEvent.THOUGHT, f"Query type inferred: {query_doc_type} — routing to matching documents")

        # ── 5. Profile-aware routing ─────────────────────────────────── #
        document_ids = _route_documents(query, all_sources, query_doc_type)
        if not document_ids:
            yield event(AgentEvent.ERROR, "No knowledgebase sources available after routing")
            return

        routed_count = len(document_ids)
        total_count  = len(all_sources)
        if routed_count < total_count:
            yield event(AgentEvent.THOUGHT,
                f"Document routing: searching {routed_count}/{total_count} documents "
                f"(type-aligned to '{query_doc_type or 'any'}')"
            )

        # ── 6. Autonomy search loop ──────────────────────────────────── #
        async for evt in self._autonomous_search(
            query, document_ids, max_results, search_scope, search_depth,
            project_fid, query_doc_type
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
        query_doc_type: Optional[str],
    ) -> AsyncGenerator[Dict, None]:
        escalated   = False
        reformulated = False
        gap_filled  = False

        yield event(AgentEvent.PLAN,
            f"Running {search_depth} search across {len(document_ids)} document(s)"
            + (f" [type={query_doc_type}]" if query_doc_type else "")
        )
        yield event(AgentEvent.TOOL_CALL, {
            "query":          query,
            "document_count": len(document_ids),
            "max_results":    max_results,
            "search_scope":   search_scope,
            "search_depth":   search_depth,
            "query_doc_type": query_doc_type,
        })

        # ── A: initial search ────────────────────────────────────────── #
        try:
            if search_depth == "deep":
                chunks = await self._deep_fetch(query, document_ids, max_results, project_fid, query_doc_type)
            else:
                raw    = await self._execute_search(query, document_ids, max_results, project_fid, query_doc_type)
                chunks = [self._format_chunk(c) for c in (raw or [])]
        except Exception as e:
            yield event(AgentEvent.ERROR, f"Search failed: {e}")
            return

        sufficient, max_rel, confidence = _assess_quality(chunks)
        logger.info(f"📊 Initial — max_rel={max_rel:.1f}% confidence={confidence} sufficient={sufficient}")

        # ── B: auto-escalate normal→deep ─────────────────────────────── #
        if not sufficient and search_depth == "normal":
            escalated = True
            yield event(AgentEvent.THOUGHT,
                f"Normal search quality low ({max_rel:.0f}%) — escalating to deep")
            try:
                deep_chunks = await self._deep_fetch(query, document_ids, max_results, project_fid, query_doc_type)
                chunks = self._merge(chunks, deep_chunks)
            except Exception as e:
                logger.warning(f"Auto-escalation failed: {e}")
            sufficient, max_rel, confidence = _assess_quality(chunks)

        # ── C: reformulate if still poor ─────────────────────────────── #
        if not sufficient:
            reformulated = True
            yield event(AgentEvent.THOUGHT,
                f"Results still poor ({max_rel:.0f}%) — reformulating query")
            ref_queries = await self._reformulate_query(query, query_doc_type)
            if ref_queries:
                ref_results = await asyncio.gather(
                    *[self._execute_search(q, document_ids, max_results, project_fid, None) for q in ref_queries],
                    return_exceptions=True,
                )
                for r in ref_results:
                    if isinstance(r, Exception) or r is None: continue
                    chunks = self._merge(chunks, [self._format_chunk(c) for c in r])
            sufficient, max_rel, confidence = _assess_quality(chunks)

        # ── D: gap-filling ───────────────────────────────────────────── #
        if chunks and confidence < 0.80:
            gap_queries = await self._find_gaps(query, chunks, query_doc_type)
            if gap_queries:
                gap_filled = True
                yield event(AgentEvent.THOUGHT,
                    f"Detected {len(gap_queries)} unanswered aspect(s) — running gap searches")
                gap_results = await asyncio.gather(
                    *[self._execute_search(gq, document_ids, max_results, project_fid, None) for gq in gap_queries],
                    return_exceptions=True,
                )
                for r in gap_results:
                    if isinstance(r, Exception) or r is None: continue
                    chunks = self._merge(chunks, [self._format_chunk(c) for c in r])
                _, max_rel, confidence = _assess_quality(chunks)

        # ── E: re-rank ───────────────────────────────────────────────── #
        if chunks:
            chunks = _rerank(chunks, query)

        cap = max(max_results * 3, 15) if (escalated or search_depth == "deep" or gap_filled) else max_results
        chunks = chunks[:cap]

        # ── F: cache + emit ─────────────────────────────────────────── #
        if chunks:
            self._search_cache[self._make_cache_key(query, search_scope, search_depth)] = chunks

        # Compute doc_type distribution in results
        from collections import Counter
        type_dist = dict(Counter(
            c.get("doc_type", "unknown") for c in chunks if c.get("doc_type")
        ).most_common(5))

        obs_parts = [f"Found {len(chunks)} relevant chunk(s)"]
        if escalated:    obs_parts.append("(auto-escalated to deep)")
        if reformulated: obs_parts.append("(query reformulated)")
        if gap_filled:   obs_parts.append("(gap-filled)")
        yield event(AgentEvent.OBSERVATION, " ".join(obs_parts))

        final_depth = "deep" if (search_depth == "deep" or escalated) else "normal"
        yield event(AgentEvent.RESULT, self._build_result(
            query, chunks, search_scope, final_depth, confidence,
            escalated=escalated, reformulated=reformulated, gap_filled=gap_filled,
            doc_type_distribution=type_dist,
        ))

    # ------------------------------------------------------------------ #
    #  Deep fan-out fetch                                                  #
    # ------------------------------------------------------------------ #

    async def _deep_fetch(
        self,
        query: str,
        document_ids: List,
        max_results: int,
        project_fid: Optional[int],
        query_doc_type: Optional[str],
    ) -> List[Dict]:
        sub_queries = await self._decompose_query(query, query_doc_type)
        logger.info(f"🔀 Deep fetch: {len(sub_queries)} sub-queries (style={query_doc_type or 'general'})")

        results = await asyncio.gather(
            *[self._execute_search(sq, document_ids, max_results, project_fid, query_doc_type)
              for sq in sub_queries],
            return_exceptions=True,
        )
        chunks: List[Dict] = []
        for r in results:
            if isinstance(r, Exception) or r is None: continue
            chunks = self._merge(chunks, [self._format_chunk(c) for c in r])
        return chunks

    # ------------------------------------------------------------------ #
    #  LLM helpers (profile-aware)                                         #
    # ------------------------------------------------------------------ #

    async def _decompose_query(self, query: str, doc_type: Optional[str]) -> List[str]:
        """Decompose query into focused sub-queries, style-matched to doc_type."""
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return [query]

        style_hint = _SUBQUERY_STYLE_HINTS.get(doc_type or "general", _SUBQUERY_STYLE_HINTS["general"])
        prompt = (
            f"You are a search query optimizer for a company knowledge base.\n"
            f"The documents being searched are of type: {doc_type or 'general'}.\n"
            f"Sub-query style: {style_hint}.\n\n"
            f"Decompose the following query into 3 to 4 focused sub-queries that "
            f"each target a distinct aspect of the question.\n"
            f"Return ONLY a JSON array of strings — no explanation, no markdown.\n\n"
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
                    subs = [str(q).strip() for q in parsed if str(q).strip()]
                    if subs:
                        if query not in subs:
                            subs.insert(0, query)
                        return subs[:5]
        except Exception as e:
            logger.warning(f"Query decomposition parse failed: {e}")
        return [query]

    async def _reformulate_query(self, query: str, doc_type: Optional[str]) -> List[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return []
        style_hint = _SUBQUERY_STYLE_HINTS.get(doc_type or "general", _SUBQUERY_STYLE_HINTS["general"])
        prompt = (
            f"A semantic search for documents of type '{doc_type or 'general'}' returned poor results.\n"
            f"Sub-query style: {style_hint}.\n\n"
            f"Generate 3 alternative queries for this original query: one broader, "
            f"one using synonyms, one keyword-only (no stop words).\n"
            f"Return ONLY a JSON array of 3 strings.\n\n"
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

    async def _find_gaps(self, query: str, chunks: List[Dict], doc_type: Optional[str]) -> List[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return []
        found_summary = "\n".join(
            f"- {c.get('title','Untitled')} / {c.get('heading','') or c.get('section_path','')}"
            for c in chunks[:10]
        )
        style_hint = _SUBQUERY_STYLE_HINTS.get(doc_type or "general", _SUBQUERY_STYLE_HINTS["general"])
        prompt = (
            f"Documents type: {doc_type or 'general'}. Sub-query style: {style_hint}.\n\n"
            f"Original question: {query}\n\n"
            f"Content found so far:\n{found_summary}\n\n"
            "Identify up to 2 specific aspects of the question NOT yet covered. "
            "For each gap write a short, focused search query in the style above.\n"
            "If fully covered, return []. Return ONLY a JSON array.\n"
            'Example: ["gap query 1", "gap query 2"]'
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

    async def _llm_call(self, prompt: str, max_tokens: int = 200) -> Optional[str]:
        """
        Call the LLM provider with a plain prompt string.

        Supports the AIProvider contract (get_response / stream_response) as well
        as legacy providers that expose a generate() method — whichever is present.
        """
        if not self.llm_provider:
            return None
        try:
            # ── Path 1: AIProvider contract (get_response) ─────────────── #
            # OpenRouterProvider, OpenAIProvider, etc. all implement get_response.
            if hasattr(self.llm_provider, "get_response"):
                result = await self.llm_provider.get_response(
                    transcript=prompt,
                    max_tokens=max_tokens,
                )
                return result.strip() if isinstance(result, str) else None

            # ── Path 2: legacy generate() — sync or async ─────────────── #
            if hasattr(self.llm_provider, "generate"):
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
    #  HTTP search (passes doc_type filter when known)                     #
    # ------------------------------------------------------------------ #

    async def _execute_search(
        self,
        query: str,
        document_ids: List,
        limit: int,
        project_fid: Optional[int],
        doc_type: Optional[str],
    ) -> Optional[List[Dict]]:
        url     = f"{COMPANY_URL}/aiagentchat/knowledgebase/search"
        payload: Dict[str, Any] = {
            "query":        query,
            "document_ids": document_ids,
            "limit":        limit,
            "project_fid":  project_fid,
        }
        # Do NOT send doc_type as a Milvus filter when document_ids are already
        # scoped. Old chunks indexed before the schema change have an empty doc_type
        # field in Milvus — adding a filter would silently exclude them even though
        # the document-level routing already selected the right documents.
        # doc_type is only useful for broad unscoped searches (no document_ids).
        if doc_type and not payload.get("document_ids"):
            payload["doc_type"] = doc_type

        logger.info(f"🔎 KB search request | query='{query[:80]}' | doc_ids={document_ids} | limit={limit} | doc_type={doc_type or 'none'} | url={url}")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type":  "application/json",
                        "companyIds":    f"[{self.company_id}]",
                    },
                    json=payload,
                )

            logger.info(f"🔎 KB search response | status={response.status_code} | query='{query[:80]}'")

            if response.status_code == 200:
                result = response.json()
                resp_type = result.get("type")
                resp_data = result.get("data")

                logger.info(f"🔎 KB response body | type={resp_type} | data_keys={list(resp_data.keys()) if isinstance(resp_data, dict) else type(resp_data).__name__}")

                if resp_type == "success" and resp_data:
                    chunks = resp_data.get("results", [])
                    count  = resp_data.get("count", len(chunks))
                    logger.info(f"✅ KB chunks returned: {count} | query='{query[:80]}'")
                    for i, ch in enumerate(chunks[:5], 1):
                        logger.info(
                            f"   chunk {i}: id={ch.get('id')} doc_id={ch.get('document_id')} "
                            f"score={ch.get('distance','?')} title='{str(ch.get('title',''))[:40]}' "
                            f"text='{str(ch.get('chunk_text',''))[:80].replace(chr(10),' ')}'"
                        )
                    if len(chunks) > 5:
                        logger.info(f"   ... +{len(chunks)-5} more chunk(s)")
                    return chunks

                logger.warning(f"⚠️  KB response not success | type={resp_type} | data={str(resp_data)[:200]}")
                return []

            logger.error(f"❌ KB HTTP {response.status_code} | query='{query[:80]}' | body={response.text[:300]}")
            return None

        except httpx.TimeoutException:
            logger.error(f"❌ KB search timeout | query='{query[:80]}'")
            raise
        except httpx.RequestError as e:
            logger.error(f"❌ KB request error | query='{query[:80]}' | error={e}")
            raise

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_chunk(chunk: Dict) -> Dict:
        distance = chunk.get("distance", 1)
        relevance_score = max(0.0, min(100.0, round((1 - distance) * 100, 1)))
        return {
            "chunk_id":      chunk.get("id"),
            "chunk_index":   chunk.get("chunk_index"),
            "chunk_text":    chunk.get("chunk_text"),
            "chunk_type":    chunk.get("chunk_type"),
            "distance":      distance,
            "relevance_score": relevance_score,
            "document_id":   chunk.get("document_id"),
            "title":         chunk.get("title"),
            "file_type":     chunk.get("file_type"),
            "source_path":   chunk.get("source_path"),
            "project_fid":   chunk.get("project_fid"),
            "section_path":  chunk.get("section_path"),
            "heading":       chunk.get("heading"),
            "heading_level": chunk.get("heading_level"),
            "keywords":      chunk.get("keywords"),
            "parent_id":     chunk.get("parent_id"),
            "part_index":    chunk.get("part_index"),
            "total_parts":   chunk.get("total_parts"),
            "token_count":   chunk.get("token_count"),
            "start_position": chunk.get("start_position"),
            "end_position":  chunk.get("end_position"),
            # Self-learned fields (from DocumentProfile via Milvus)
            "doc_type":      chunk.get("doc_type"),
            "language":      chunk.get("language"),
            "page_number":   chunk.get("page_number"),
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
        doc_type_distribution: Optional[Dict] = None,
    ) -> Dict:
        quality = (
            "high"   if confidence >= 0.65
            else "medium" if confidence >= 0.35
            else "low"
        )
        return {
            "success":               True,
            "query":                 query,
            "results":               chunks,
            "total_results":         len(chunks),
            "search_scope":          search_scope,
            "search_depth":          search_depth,
            "confidence":            confidence,
            "quality":               quality,
            "escalated":             escalated,
            "reformulated":          reformulated,
            "gap_filled":            gap_filled,
            "source":                source,
            "doc_type_distribution": doc_type_distribution or {},
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
                        "companyIds":    f"[{self.company_id}]",
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
