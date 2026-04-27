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
import math
import logging
import asyncio
import hashlib
import httpx
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple, AsyncGenerator
from datetime import datetime

logger = logging.getLogger(__name__)

COMPANY_URL = os.getenv("COMPANY_URL")

# ── Thresholds ────────────────────────────────────────────────────────────── #
QUALITY_THRESHOLD   = 38.0
MIN_RESULT_COUNT    = 2
RERANK_SEMANTIC_W   = 0.60   # reduced slightly to make room for heading signal
RERANK_HEADING_W    = 0.15   # NEW: dedicated heading-alignment weight
RERANK_KEYWORD_W    = 0.18   # body-text + keyword overlap
RERANK_PAGE_W       = 0.07   # small bonus for chunks near page 1 (title area)

COHESION_BOOST      = 8.0    # additive score for chunks co-located with top anchor group

# ── Context expansion thresholds ─────────────────────────────────────────── #
ANCHOR_THRESHOLD_HIGH   = 55.0   # relevance ≥ 55% → window=2 (±2 neighbors)
ANCHOR_THRESHOLD_MEDIUM = 38.0   # relevance ≥ 38% → window=1 (±1 neighbor)
ANCHOR_MAX              = 3      # max anchors to expand (focus on top signals)
CONTEXT_TOKEN_BUDGET    = 3000   # estimated tokens for final LLM context
CONTEXT_CHARS_PER_TOKEN = 4      # rough estimate: 4 chars ≈ 1 token

# ── Fix 1: Hard pipeline caps (prevent runaway adaptive loops) ────────────── #
MAX_TOTAL_SEARCHES  = 6    # total _execute_search calls allowed per run()
MAX_EXPANSIONS      = 2    # max anchor expansions (structural + semantic + etc.)
MAX_GROUPS          = 5    # max context groups kept after ranking

# ── Fix 2: Retrieval source weights (signal normalization) ───────────────── #
# Each retrieval path has different precision. Weights scale the relevance_score
# contribution so high-noise sources (gap, keyword) don't override strong
# primary vector hits. Applied in _rerank before blending.
_SOURCE_WEIGHTS: Dict[str, float] = {
    "vector":       1.00,  # primary Milvus semantic search — most reliable
    "semantic":     0.85,  # anchor-text re-search within same doc
    "related":      0.80,  # pre-computed cosine cross-links (index time)
    "section":      0.75,  # same-heading chunks — metadata match, not semantic
    "structural":   0.70,  # positional ±window — proximity, not relevance
    "keyword":      0.65,  # exact term match — high precision, low recall
    "gap":          0.60,  # gap-fill sub-queries — exploratory, least certain
    "section_node": 0.80,  # expanded from virtual section centroid
}

def _sigmoid_normalize(score: float, midpoint: float = 50.0, scale: float = 0.08) -> float:
    """
    Map a 0-100 relevance score to 0-1 via sigmoid centred at `midpoint`.
    Compresses extreme outliers and gives a smooth gradient near the decision boundary.
    Used to normalise scores BEFORE source-weight blending so a raw 95 from a
    noisy source doesn't swamp a solid 70 from the primary vector path.
    """
    return 1.0 / (1.0 + math.exp(-scale * (score - midpoint)))

# ── Fix 3: Anchor-topic divergence threshold ─────────────────────────────── #
# Anchors with Jaccard word-overlap below this threshold are treated as
# covering DIFFERENT topics → kept in separate expansion groups so
# unrelated neighbors are never stitched together.
ANCHOR_DIVERGE_THRESHOLD = 0.40

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
        "timestamp": datetime.utcnow().isoformat(),
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


# ── Re-ranking (semantic + heading alignment + keyword + page + cohesion) ─── #
def _rerank(chunks: List[Dict], query: str) -> List[Dict]:
    """
    Blended re-scoring with four independent signals:

      1. Semantic (Milvus cosine distance)   — weight RERANK_SEMANTIC_W
      2. Heading alignment                   — weight RERANK_HEADING_W
         Separate from body-text overlap so that a chunk whose *heading*
         matches the query is boosted even when its body is dense/technical.
         Especially effective for policies, reports, and manuals where users
         navigate by section title ("what is the refund policy?").
      3. Body-text + keyword overlap          — weight RERANK_KEYWORD_W
      4. Page proximity (title/intro bias)    — weight RERANK_PAGE_W

    Additive bonuses (applied after blended score):
      • Neighbor chunks:  ×1.15 multiplicative (preserves context near anchors)
      • Cohesion group:   +COHESION_BOOST  (rewards chunks co-located with
                           the top anchor's section — keeps logical units together)
    """
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    if not query_words:
        return chunks

    # ── Identify dominant cohesion group from top-3 highest-relevance chunks ─ #
    top3 = sorted(chunks, key=lambda c: c.get("relevance_score", 0), reverse=True)[:3]
    dominant_groups: Set[str] = {
        c["cohesion_group"] for c in top3
        if c.get("cohesion_group")
    }

    scored: List[Tuple[float, Dict]] = []
    for chunk in chunks:
        heading  = (chunk.get("heading")    or "").lower()
        text     = (chunk.get("chunk_text") or "").lower()
        keywords = (chunk.get("keywords")   or "").lower()

        # ── Fix 2: source-aware semantic score ──────────────────────── #
        # Normalise raw relevance via sigmoid then apply per-source weight.
        # Prevents a noisy gap-fill chunk (score=85) from outranking a
        # solid primary-vector chunk (score=72) purely on raw number.
        source       = chunk.get("_expand_type") or "vector"
        src_weight   = _SOURCE_WEIGHTS.get(source, 1.0)
        norm_score   = _sigmoid_normalize(chunk.get("relevance_score", 0.0)) * 100.0
        weighted_sem = norm_score * src_weight

        # ── Signal 2: heading alignment ─────────────────────────────── #
        heading_words   = set(re.findall(r"\w+", heading)) - _STOP_WORDS
        heading_overlap = (
            len(query_words & heading_words) / max(len(query_words), 1)
            if heading_words else 0.0
        )

        # ── Signal 3: body-text + keyword overlap ───────────────────── #
        body_combined = set(re.findall(r"\w+", f"{text} {keywords}"))
        body_overlap  = len(query_words & body_combined) / max(len(query_words), 1)

        # ── Signal 4: page proximity ─────────────────────────────────── #
        page       = chunk.get("page_number") or chunk.get("page") or 0
        page_bonus = max(0.0, 1.0 - (page / 50.0)) if page else 0.5

        blended = (
            weighted_sem                       * RERANK_SEMANTIC_W
            + heading_overlap * 100.0          * RERANK_HEADING_W
            + body_overlap    * 100.0          * RERANK_KEYWORD_W
            + page_bonus      * 100.0          * RERANK_PAGE_W
        )

        # ── Additive bonus: neighbor proximity ──────────────────────── #
        if chunk.get("_is_neighbor"):
            blended *= 1.15

        # ── Additive bonus: cohesion group ───────────────────────────── #
        # Reward chunks that belong to the same logical section as the
        # top anchor — keeps "Steps 1-5" together even if step 4 scores
        # slightly lower than an unrelated high-similarity chunk.
        if chunk.get("cohesion_group") and chunk.get("cohesion_group") in dominant_groups:
            blended += COHESION_BOOST

        scored.append((blended, chunk))

    return [c for _, c in sorted(scored, key=lambda x: x[0], reverse=True)]


# ── Anchor detection ─────────────────────────────────────────────────────── #
def _anchor_too_similar(candidate: Dict, existing: List[Dict], threshold: float = 0.72) -> bool:
    """
    Jaccard word-overlap between candidate and any already-selected anchor.
    Prevents selecting redundant anchors that would expand the same idea twice.
    """
    cand_words = set(re.findall(r"\w+", (candidate.get("chunk_text") or "").lower())) - _STOP_WORDS
    if not cand_words:
        return False
    for anchor in existing:
        a_words = set(re.findall(r"\w+", (anchor.get("chunk_text") or "").lower())) - _STOP_WORDS
        if not a_words:
            continue
        union = len(cand_words | a_words)
        if union > 0 and (len(cand_words & a_words) / union) >= threshold:
            return True
    return False


def _anchors_diverge(a1: Dict, a2: Dict, threshold: float = ANCHOR_DIVERGE_THRESHOLD) -> bool:
    """
    Return True when two anchors cover clearly DIFFERENT topics.

    Uses Jaccard word-overlap on chunk text (stop words removed).
    Low overlap (< threshold) = different topic → must NOT be mixed into
    the same context group, because expanding one anchor's neighbors into
    the other's group introduces irrelevant noise.

    Threshold = 0.40: two chunks need to share at least 40% of their
    meaningful words to be considered the same topic.
    """
    words1 = set(re.findall(r"\w+", (a1.get("chunk_text") or "").lower())) - _STOP_WORDS
    words2 = set(re.findall(r"\w+", (a2.get("chunk_text") or "").lower())) - _STOP_WORDS
    if not words1 or not words2:
        return False
    union = len(words1 | words2)
    return union > 0 and (len(words1 & words2) / union) < threshold


def _select_anchors(chunks: List[Dict]) -> List[Dict]:
    """
    Identify high-signal, *diverse* chunks that deserve context expansion.

    Selection criteria (in priority order):
      1. relevance_score ≥ ANCHOR_THRESHOLD_MEDIUM
      2. Jaccard text similarity < 0.72 with already-selected anchors
         (prevents expanding the same idea twice when two chunks cover
          the same sentence or heading fragment)
      3. Cap at ANCHOR_MAX to avoid over-expansion

    Falls back to the single top chunk when nothing qualifies.
    """
    if not chunks:
        return []
    qualified = sorted(
        [c for c in chunks if c.get("relevance_score", 0) >= ANCHOR_THRESHOLD_MEDIUM],
        key=lambda c: c.get("relevance_score", 0), reverse=True,
    )
    anchors: List[Dict] = []
    for candidate in qualified:
        if _anchor_too_similar(candidate, anchors):
            continue
        anchors.append(candidate)
        if len(anchors) >= ANCHOR_MAX:
            break
    return anchors or [chunks[0]]


# ── Chunk stitching ───────────────────────────────────────────────────────── #
def _stitch_chunks(chunks: List[Dict]) -> List[Dict]:
    """
    Merge chunks that are adjacent by chunk_index within the same document
    into single coherent sections.

    Algorithm:
      1. Group by document_id
      2. Sort each group by chunk_index
      3. Merge consecutive runs (chunk_index[i+1] == chunk_index[i] + 1)
      4. For each merged run: combine text, keep highest relevance_score,
         carry metadata from the anchor chunk of the run (highest relevance)

    Chunks missing chunk_index are kept as-is (cannot be positioned).
    """
    if not chunks:
        return []

    # Separate stitchable (have doc_id + chunk_index) from orphan chunks
    stitchable = [c for c in chunks if c.get("document_id") is not None and c.get("chunk_index") is not None]
    orphans    = [c for c in chunks if c not in stitchable]

    # Group by document
    groups: Dict[Any, List[Dict]] = {}
    for chunk in stitchable:
        doc_id = chunk["document_id"]
        groups.setdefault(doc_id, []).append(chunk)

    stitched: List[Dict] = []
    for doc_id, doc_chunks in groups.items():
        doc_chunks.sort(key=lambda c: c["chunk_index"])

        buffer: List[Dict] = [doc_chunks[0]]
        for i in range(1, len(doc_chunks)):
            prev = buffer[-1]
            curr = doc_chunks[i]
            if curr["chunk_index"] == prev["chunk_index"] + 1:
                buffer.append(curr)
            else:
                stitched.append(_merge_chunk_run(buffer))
                buffer = [curr]
        stitched.append(_merge_chunk_run(buffer))

    return stitched + orphans


def _merge_chunk_run(run: List[Dict]) -> Dict:
    """Merge a list of adjacent chunks into one combined chunk dict."""
    if len(run) == 1:
        return run[0]

    # Anchor = highest relevance chunk in the run (carries the metadata)
    anchor = max(run, key=lambda c: c.get("relevance_score", 0))

    combined_text = "\n".join(
        c.get("chunk_text") or "" for c in run if c.get("chunk_text")
    ).strip()

    merged = dict(anchor)
    merged["chunk_text"]     = combined_text
    merged["relevance_score"] = anchor.get("relevance_score", 0)
    merged["chunk_index"]    = run[0]["chunk_index"]
    merged["chunk_index_end"] = run[-1]["chunk_index"]
    merged["is_stitched"]    = True
    merged["stitched_count"] = len(run)
    return merged


# ── Token-budget compression ─────────────────────────────────────────────── #
def _compress_to_budget(chunks: List[Dict], token_budget: int = CONTEXT_TOKEN_BUDGET) -> List[Dict]:
    """
    Trim the chunk list so the total estimated token count stays within budget.
    Drops the lowest-relevance chunks last (highest relevance is always kept).
    At least 1 chunk is always returned regardless of budget.
    """
    char_budget = token_budget * CONTEXT_CHARS_PER_TOKEN
    sorted_chunks = sorted(chunks, key=lambda c: c.get("relevance_score", 0), reverse=True)

    kept: List[Dict] = []
    total_chars = 0
    for chunk in sorted_chunks:
        text_len = len(chunk.get("chunk_text") or "")
        if not kept or total_chars + text_len <= char_budget:
            kept.append(chunk)
            total_chars += text_len
        else:
            break  # everything after this is lower relevance and would exceed budget

    if not kept and chunks:
        kept = [chunks[0]]  # always keep at least one

    if len(kept) < len(chunks):
        logger.info(
            f"✂️ Context compressed: {len(chunks)} → {len(kept)} chunk(s) "
            f"(~{total_chars // CONTEXT_CHARS_PER_TOKEN} tokens, budget={token_budget})"
        )
    return kept


# ── Group-level ranking ───────────────────────────────────────────────────── #
def _rank_groups(groups: List[Dict], query: str) -> List[Dict]:
    """
    Score each anchor-centric context group by the quality of its stitched content.

    Formula (all signals normalised to 0–100 before weighting):

      score = 0.50 × anchor_score
            + 0.25 × coverage        — fraction of query words found in group text
            + 0.15 × diversity        — cross-document + cross-section spread
            + 0.10 × confidence       — mean relevance of ALL chunks (not just anchor)

    Why each weight:
      anchor  (0.50) — the primary retrieval signal; anchor quality sets the ceiling.
      coverage(0.25) — directly measures "does this group answer the question?"
      diversity(0.15)— punishes echo-chamber groups (single doc × single section);
                        rewards groups that synthesise multiple perspectives.
      confidence(0.10)— mean chunk score; prevents a high-anchor group with many
                         low-quality filler chunks from beating a tight, solid group.

    Diversity is computed as the blend of:
      • document diversity: unique doc IDs ÷ 3  (capped at 1.0)
      • section  diversity: unique headings  ÷ 5  (capped at 1.0)
    A single-doc, single-section group scores 0 on diversity.
    A group spanning 2+ docs and 3+ sections scores close to 1.0.
    """
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    scored: List[Tuple[float, Dict]] = []

    for group in groups:
        stitched = group.get("stitched_chunks") or []
        if not stitched:
            continue

        # ── Signal 1: anchor quality (0–100) ─────────────────────────── #
        anchor_score = group["anchor"].get("relevance_score", 0.0)

        # ── Signal 2: query-word coverage (0–1 → ×100) ───────────────── #
        combined_text  = " ".join(c.get("chunk_text") or "" for c in stitched).lower()
        combined_words = set(re.findall(r"\w+", combined_text))
        coverage = len(query_words & combined_words) / max(len(query_words), 1)

        # ── Signal 3: diversity (0–1 → ×100) ─────────────────────────── #
        # Document diversity: how many distinct source documents contribute.
        # Capped at 3 docs = full doc-diversity score (diminishing returns beyond).
        unique_docs = len({
            c.get("document_id") for c in stitched
            if c.get("document_id") is not None
        })
        doc_diversity = min((unique_docs - 1) / 2.0, 1.0) if unique_docs > 1 else 0.0

        # Section diversity: how many distinct headings are represented.
        # Capped at 5 headings = full section-diversity score.
        unique_headings = len({c.get("heading") for c in stitched if c.get("heading")})
        sec_diversity = min(unique_headings / 5.0, 1.0)

        # Blend: doc diversity weighted slightly higher (cross-doc = stronger signal)
        diversity = 0.6 * doc_diversity + 0.4 * sec_diversity

        # ── Signal 4: mean chunk confidence (0–1 → ×100) ─────────────── #
        # Measures whether the group's non-anchor chunks are also strong,
        # not just the anchor. Penalises groups that padded with weak neighbors.
        all_scores = [c.get("relevance_score", 0.0) for c in stitched]
        confidence = (sum(all_scores) / len(all_scores) / 100.0) if all_scores else 0.0

        # ── Composite score ───────────────────────────────────────────── #
        group_score = (
            anchor_score     * 0.50
            + coverage       * 100.0 * 0.25
            + diversity      * 100.0 * 0.15
            + confidence     * 100.0 * 0.10
        )

        group["group_score"]    = round(group_score,  2)
        group["_coverage"]      = round(coverage,     3)
        group["_diversity"]     = round(diversity,    3)
        group["_confidence"]    = round(confidence,   3)

        scored.append((group_score, group))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [g for _, g in scored]


# ── Group-aware token-budget compression ─────────────────────────────────── #
def _compress_groups_to_budget(
    groups: List[Dict], token_budget: int = CONTEXT_TOKEN_BUDGET
) -> List[Dict]:
    """
    Retain complete context groups within the token budget.
    A group is kept or dropped as a unit — never split mid-group.
    Groups must be sorted by score descending before calling.
    Always returns at least the best group regardless of budget.
    """
    char_budget = token_budget * CONTEXT_CHARS_PER_TOKEN
    kept: List[Dict] = []
    total_chars = 0
    for group in groups:
        group_text  = " ".join(
            c.get("chunk_text") or ""
            for c in (group.get("stitched_chunks") or [])
        )
        group_chars = len(group_text)
        if not kept or total_chars + group_chars <= char_budget:
            kept.append(group)
            total_chars += group_chars
        else:
            break
    if not kept and groups:
        kept = [groups[0]]  # always keep at least the best group
    if len(kept) < len(groups):
        logger.info(
            f"✂️ Group compression: {len(groups)} → {len(kept)} group(s) "
            f"(~{total_chars // CONTEXT_CHARS_PER_TOKEN} tokens, budget={token_budget})"
        )
    return kept


# ── Metadata-only document routing (fast fallback) ───────────────────────── #
def _route_documents_metadata(
    query: str,
    all_sources: List[Dict],
    query_doc_type: Optional[str],
    min_docs: int = 3,
) -> List[Any]:
    """
    Pure metadata routing: title keyword overlap + doc_type alignment bonus.
    Used as the fallback when the semantic pre-scan is unavailable or fails.
    Returns all docs when fewer than min_docs score above zero.
    """
    query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
    scored: List[Tuple[float, Any]] = []
    for src in all_sources:
        doc_id = src.get("agent_knowledgebase_id")
        if doc_id is None:
            continue
        title_words = set(re.findall(r"\w+", (src.get("title") or "").lower()))
        keyword_score = len(query_words & title_words) if query_words else 0
        stored_type = src.get("doc_type") or ""
        type_bonus = 2.0 if (query_doc_type and stored_type == query_doc_type) else 0.0
        scored.append((keyword_score + type_bonus, doc_id))

    scored.sort(key=lambda x: x[0], reverse=True)
    matched = [doc_id for score, doc_id in scored if score > 0]
    if len(matched) < min_docs:
        return [doc_id for _, doc_id in scored]
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
        self._search_count: int = 0   # Fix 1: tracks _execute_search calls per run()

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
            payload: Dict[str, Any] = {
                "document_ids": all_ids,
                "project_fid":  self.agent_id,
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "companyIds": f"[{self.company_id}]",
                    },
                    json=payload,
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
    #  Semantic document routing (embedding pre-scan)                     #
    # ------------------------------------------------------------------ #

    async def _route_documents_semantic(
        self,
        query: str,
        all_sources: List[Dict],
        query_doc_type: Optional[str],
        min_docs: int = 3,
        relevance_threshold: float = 20.0,
    ) -> List[Any]:
        """
        Rank documents by how well their *actual content* matches the query,
        not by title keywords or metadata.

        Strategy — one pre-scan Milvus search across all documents:
          limit = min(len(all_sources) * 3, 45)
          → gives each document up to 3 representative chunks in the result set
          → group by document_id → per-doc max relevance score
          → blend: 70% semantic + 20% title overlap + 10% doc_type alignment
          → exclude docs below relevance_threshold unless too few qualify

        Fallback to metadata-only routing on any error (non-fatal).
        """
        all_ids = [
            src.get("agent_knowledgebase_id")
            for src in all_sources
            if src.get("agent_knowledgebase_id") is not None
        ]
        if not all_ids:
            return _route_documents_metadata(query, all_sources, query_doc_type, min_docs)

        # Skip pre-scan when there are very few docs — just search them all
        if len(all_ids) <= min_docs:
            logger.debug(f"🗺️ Routing: only {len(all_ids)} doc(s) — skipping pre-scan, using all")
            return all_ids

        pre_scan_limit = min(len(all_ids) * 3, 45)
        logger.info(
            f"🗺️ Routing pre-scan: query='{query[:60]}' | "
            f"{len(all_ids)} docs | limit={pre_scan_limit}"
            f" | agent_id={self.agent_id}"
        )

        try:
            raw_chunks = await self._execute_search(
                query, all_ids, pre_scan_limit, doc_type=None
            )
        except Exception as e:
            logger.warning(f"🗺️ Routing pre-scan failed ({e}) — falling back to metadata routing")
            return _route_documents_metadata(query, all_sources, query_doc_type, min_docs)

        if not raw_chunks:
            logger.info("🗺️ Routing pre-scan returned no chunks — falling back to metadata routing")
            return _route_documents_metadata(query, all_sources, query_doc_type, min_docs)

        # ── Aggregate per-document semantic scores ─────────────────────── #
        # semantic_score[doc_id] = max relevance score across all returned chunks
        semantic_scores: Dict[Any, float] = {}
        for chunk in raw_chunks:
            doc_id = chunk.get("document_id")
            if doc_id is None:
                continue
            distance  = chunk.get("distance", 1.0)
            rel_score = max(0.0, min(100.0, round((1.0 - distance) * 100, 1)))
            if rel_score > semantic_scores.get(doc_id, 0.0):
                semantic_scores[doc_id] = rel_score

        logger.info(
            f"🗺️ Routing pre-scan scores: "
            + ", ".join(
                f"doc{did}={sc:.1f}%"
                for did, sc in sorted(semantic_scores.items(), key=lambda x: -x[1])[:8]
            )
        )

        # ── Build source lookup for metadata scoring ────────────────────── #
        query_words = set(re.findall(r"\w+", query.lower())) - _STOP_WORDS
        src_by_id = {
            src.get("agent_knowledgebase_id"): src
            for src in all_sources
            if src.get("agent_knowledgebase_id") is not None
        }

        # ── Blended score per document ──────────────────────────────────── #
        # Weights: 70% semantic (actual content match), 20% title, 10% doc_type
        SEMANTIC_W  = 0.70
        TITLE_W     = 0.20
        DOCTYPE_W   = 0.10

        blended: List[Tuple[float, Any]] = []
        for doc_id in all_ids:
            src        = src_by_id.get(doc_id) or {}
            sem_score  = semantic_scores.get(doc_id, 0.0)           # 0–100

            title_words = set(re.findall(r"\w+", (src.get("title") or "").lower()))
            title_score = (
                min(len(query_words & title_words) / max(len(query_words), 1), 1.0) * 100.0
                if query_words else 0.0
            )                                                         # 0–100

            stored_type  = src.get("doc_type") or ""
            dtype_score  = 100.0 if (query_doc_type and stored_type == query_doc_type) else 0.0

            combined = (
                sem_score  * SEMANTIC_W
                + title_score * TITLE_W
                + dtype_score * DOCTYPE_W
            )
            blended.append((combined, doc_id))

        blended.sort(key=lambda x: x[0], reverse=True)

        # ── Gate: exclude docs below relevance threshold ────────────────── #
        qualified = [doc_id for score, doc_id in blended if score >= relevance_threshold * SEMANTIC_W]
        if len(qualified) < min_docs:
            # Threshold too strict — return top min_docs instead
            qualified = [doc_id for _, doc_id in blended[:max(min_docs, len(blended))]]

        logger.info(
            f"🗺️ Semantic routing: {len(all_ids)} → {len(qualified)} doc(s) "
            f"(threshold={relevance_threshold:.0f}%, query_type={query_doc_type or 'any'})"
        )
        return qualified

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def run(
        self,
        query: str,
        search_scope: str = "all",
        max_results: int = 10,
        search_depth: str = "normal",
    ) -> AsyncGenerator[Dict, None]:
        # ── Fix 1: reset search budget for this run ─────────────────── #
        self._search_count = 0

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

        logger.info(f"🗺️ Semantic routing: query='{query[:60]}' | agent_id={self.agent_id}")

        # ── 5. Semantic document routing (embedding pre-scan) ────────── #
        document_ids = await self._route_documents_semantic(
            query, all_sources, query_doc_type
        )
        if not document_ids:
            yield event(AgentEvent.ERROR, "No knowledgebase sources available after routing")
            return

        routed_count = len(document_ids)
        total_count  = len(all_sources)
        if routed_count < total_count:
            yield event(AgentEvent.THOUGHT,
                f"Semantic routing: focused on {routed_count}/{total_count} most relevant "
                f"document(s) (query_type='{query_doc_type or 'any'}')"
            )

        # ── 6. Autonomy search loop ──────────────────────────────────── #
        async for evt in self._autonomous_search(
            query, document_ids, max_results, search_scope, search_depth,
            query_doc_type
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

        # ── A: initial search + keyword expansion (parallel) ────────── #
        # Both paths run concurrently — vector search for semantic match,
        # keyword lookup for exact/rare-term match. Results are merged so
        # domain-specific terms (ingredient names, policy codes, etc.) are
        # reliably found even when embeddings under-weight them.
        try:
            if search_depth == "deep":
                vector_task   = self._deep_fetch(query, document_ids, max_results, query_doc_type)
                keyword_task  = self._keyword_expansion(query, document_ids)
                vector_result, keyword_result = await asyncio.gather(
                    vector_task, keyword_task, return_exceptions=True
                )
                chunks = vector_result if not isinstance(vector_result, Exception) else []
                kw_chunks = keyword_result if not isinstance(keyword_result, Exception) else []
            else:
                vector_task  = self._execute_search(query, document_ids, max_results, query_doc_type)
                keyword_task = self._keyword_expansion(query, document_ids)
                raw_result, keyword_result = await asyncio.gather(
                    vector_task, keyword_task, return_exceptions=True
                )
                raw       = raw_result if not isinstance(raw_result, Exception) else []
                chunks    = [self._format_chunk(c) for c in (raw or [])]
                kw_chunks = keyword_result if not isinstance(keyword_result, Exception) else []

            if kw_chunks:
                before = len(chunks)
                chunks = self._merge(chunks, kw_chunks)
                if len(chunks) > before:
                    logger.info(
                        f"🔑 Keyword expansion added {len(chunks) - before} new chunk(s)"
                    )
        except Exception as e:
            yield event(AgentEvent.ERROR, f"Search failed: {e}")
            return

        # ── A1: section-node expansion ───────────────────────────────── #
        # If Milvus returned any section-node virtual records, expand them
        # to their full constituent chunk sets immediately — before quality
        # assessment — so the rest of the pipeline sees real chunks only.
        section_nodes_found = any(c.get("chunk_type") == "section_node" for c in chunks)
        if section_nodes_found:
            chunks = await self._expand_section_nodes(chunks)
            if any(c.get("_expand_type") == "section_node" for c in chunks):
                yield event(AgentEvent.THOUGHT,
                    "Section-level match found — expanded to complete section context"
                )

        sufficient, max_rel, confidence = _assess_quality(chunks)
        logger.info(f"📊 Initial — max_rel={max_rel:.1f}% confidence={confidence} sufficient={sufficient}")

        # ── B: auto-escalate normal→deep ─────────────────────────────── #
        # Checkpoint: only escalate when budget allows (≥2 searches remaining)
        if not sufficient and search_depth == "normal" and self._search_count < MAX_TOTAL_SEARCHES - 1:
            escalated = True
            yield event(AgentEvent.THOUGHT,
                f"Normal search quality low ({max_rel:.0f}%) — escalating to deep")
            try:
                deep_chunks = await self._deep_fetch(query, document_ids, max_results, query_doc_type)
                chunks = self._merge(chunks, deep_chunks)
            except Exception as e:
                logger.warning(f"Auto-escalation failed: {e}")
            sufficient, max_rel, confidence = _assess_quality(chunks)
        elif not sufficient and self._search_count >= MAX_TOTAL_SEARCHES - 1:
            logger.info("⚡ Skipping escalation — search budget near limit")

        # ── C: reformulate if still poor ─────────────────────────────── #
        # Checkpoint: only reformulate when budget allows (≥1 search remaining)
        if not sufficient and self._search_count < MAX_TOTAL_SEARCHES:
            reformulated = True
            yield event(AgentEvent.THOUGHT,
                f"Results still poor ({max_rel:.0f}%) — reformulating query")
            ref_queries = await self._reformulate_query(query, query_doc_type)
            if ref_queries:
                ref_results = await asyncio.gather(
                    *[self._execute_search(q, document_ids, max_results, None) for q in ref_queries],
                    return_exceptions=True,
                )
                for r in ref_results:
                    if isinstance(r, Exception) or r is None: continue
                    chunks = self._merge(chunks, [self._format_chunk(c) for c in r])
            sufficient, max_rel, confidence = _assess_quality(chunks)
        elif not sufficient:
            logger.info("⚡ Skipping reformulation — search budget exhausted")

        # ── D: gap-filling ───────────────────────────────────────────── #
        # Checkpoint: only gap-fill when budget allows AND confidence genuinely low
        if chunks and confidence < 0.80 and self._search_count < MAX_TOTAL_SEARCHES:
            gap_queries = await self._find_gaps(query, chunks, query_doc_type)
            if gap_queries:
                gap_filled = True
                yield event(AgentEvent.THOUGHT,
                    f"Detected {len(gap_queries)} unanswered aspect(s) — running gap searches")
                gap_results = await asyncio.gather(
                    *[self._execute_search(gq, document_ids, max_results, None) for gq in gap_queries],
                    return_exceptions=True,
                )
                for r in gap_results:
                    if isinstance(r, Exception) or r is None: continue
                    if isinstance(r, list):
                        # Tag gap-fill chunks with source so _rerank weights them lower
                        gap_formatted = []
                        for c in r:
                            fc = self._format_chunk(c)
                            fc["_expand_type"] = "gap"
                            gap_formatted.append(fc)
                        chunks = self._merge(chunks, gap_formatted)
                _, max_rel, confidence = _assess_quality(chunks)
        elif chunks and confidence < 0.80:
            logger.info("⚡ Skipping gap-fill — search budget exhausted")

        # ── E: Build anchor-centric hybrid context groups ────────────── #
        # Fix 1: cap at MAX_EXPANSIONS anchors.
        # Fix 3: cluster divergent anchors → separate groups, never mixed.
        context_expanded = False
        groups: List[Dict] = []
        if chunks:
            anchors = _select_anchors(chunks)[:MAX_EXPANSIONS]  # hard cap

            # Fix 3: cluster anchors by topic similarity.
            # Anchors with Jaccard < ANCHOR_DIVERGE_THRESHOLD cover different
            # topics and MUST NOT share expansion pools — doing so introduces
            # irrelevant neighbors from the wrong topic into every group.
            anchor_clusters: List[List[Dict]] = []
            for anchor in anchors:
                placed = False
                for cluster in anchor_clusters:
                    # Same topic if NOT diverging from the cluster's lead anchor
                    if not _anchors_diverge(anchor, cluster[0]):
                        cluster.append(anchor)
                        placed = True
                        break
                if not placed:
                    anchor_clusters.append([anchor])

            if len(anchor_clusters) > 1:
                logger.info(
                    f"🧩 Anchor clustering: {len(anchors)} anchor(s) → "
                    f"{len(anchor_clusters)} topic cluster(s)"
                )
                yield event(AgentEvent.THOUGHT,
                    f"Anchors cover {len(anchor_clusters)} distinct topic(s) — "
                    f"building separate context groups per topic"
                )

            # Build one set of groups per cluster (anchors within a cluster
            # share expansion pool; anchors across clusters do NOT).
            all_groups: List[Dict] = []
            for cluster in anchor_clusters:
                cluster_groups = await self._build_context_groups(
                    cluster, chunks, document_ids
                )
                all_groups.extend(cluster_groups)
            groups = all_groups

            if groups:
                context_expanded = True
                extra = sum(len(g["all_chunks"]) - 1 for g in groups)
                logger.info(
                    f"🧩 Context groups: {len(groups)} group(s) "
                    f"| +{extra} neighbor/semantic/section chunk(s)"
                )
                yield event(AgentEvent.THOUGHT,
                    f"Built {len(groups)} anchor-centric context group(s) "
                    f"(structural + semantic + section expansion)"
                )

        # ── F: Rank groups by stitched context quality ───────────────── #
        # Score = 60% anchor relevance + 30% query coverage + 10% breadth
        if groups:
            groups = _rank_groups(groups, query)

        # ── G: Compress groups to token budget (never split a group) ─── #
        # Fix 1: also enforce MAX_GROUPS hard cap before budget compression.
        if groups:
            if len(groups) > MAX_GROUPS:
                logger.info(f"✂️ Groups capped: {len(groups)} → {MAX_GROUPS}")
                groups = groups[:MAX_GROUPS]
            groups = _compress_groups_to_budget(groups)

        # ── H: Flatten groups → final chunk list, re-rank ───────────── #
        # Use stitched_chunks from each ranked group as the unit of context.
        # Re-rank the flat list for final ordering within the kept budget.
        if groups:
            flat: List[Dict] = []
            for g in groups:
                flat.extend(g.get("stitched_chunks") or [])
            chunks = _rerank(self._merge([], flat), query)
        elif chunks:
            # Fallback: no groups built — stitch + rerank flat chunks
            before_stitch = len(chunks)
            chunks = _stitch_chunks(chunks)
            if len(chunks) < before_stitch:
                logger.info(
                    f"🧵 Fallback stitching: {before_stitch} → {len(chunks)} section(s)"
                )
            chunks = _rerank(chunks, query)
            # Apply original cap + budget on fallback path
            cap    = max(max_results * 3, 15) if (escalated or search_depth == "deep" or gap_filled) else max_results
            chunks = chunks[:cap]
            if chunks:
                chunks = _compress_to_budget(chunks)

        # ── I: cache + emit ──────────────────────────────────────────── #
        if chunks:
            self._search_cache[self._make_cache_key(query, search_scope, search_depth)] = chunks

        # Recompute quality after expansion
        _, max_rel, confidence = _assess_quality(chunks)

        # Compute doc_type distribution in results
        from collections import Counter
        type_dist = dict(Counter(
            c.get("doc_type", "unknown") for c in chunks if c.get("doc_type")
        ).most_common(5))

        obs_parts = [f"Found {len(chunks)} relevant chunk(s)"]
        if escalated:        obs_parts.append("(auto-escalated to deep)")
        if reformulated:     obs_parts.append("(query reformulated)")
        if gap_filled:       obs_parts.append("(gap-filled)")
        if context_expanded: obs_parts.append("(context expanded)")
        yield event(AgentEvent.OBSERVATION, " ".join(obs_parts))

        final_depth = "deep" if (search_depth == "deep" or escalated) else "normal"
        yield event(AgentEvent.RESULT, self._build_result(
            query, chunks, search_scope, final_depth, confidence,
            escalated=escalated, reformulated=reformulated, gap_filled=gap_filled,
            doc_type_distribution=type_dist,
        ))

    # ------------------------------------------------------------------ #
    #  Context expansion — anchor-centric hybrid groups                   #
    # ------------------------------------------------------------------ #

    async def _build_context_groups(
        self,
        anchors: List[Dict],
        existing_chunks: List[Dict],
        document_ids: List,
    ) -> List[Dict]:
        """
        Build anchor-centric context groups with three expansion strategies:

          1. Structural  — chunk_index ± window (fast, positional)
          2. Semantic    — vector search on anchor text, same document
                           (finds relevant content even when physically distant)
          3. Section     — other chunks sharing the same heading, from the
                           already-retrieved pool (zero-cost, exploits metadata)

        Returns a list of group dicts:
          {
            "anchor":          chunk,
            "all_chunks":      [deduped list — anchor + all expansion chunks],
            "stitched_chunks": [after stitch_chunks within this group],
            "group_score":     0.0  (filled later by _rank_groups)
          }

        Each anchor is expanded in parallel via asyncio.gather.
        Non-fatal on any per-anchor failure.
        """
        if not anchors:
            return []

        tasks = [self._expand_anchor(anchor, existing_chunks) for anchor in anchors]
        expansions = await asyncio.gather(*tasks, return_exceptions=True)

        groups: List[Dict] = []
        for anchor, expansion in zip(anchors, expansions):
            if isinstance(expansion, Exception):
                logger.warning(f"Anchor expansion error: {expansion}")
                expansion = {"structural": [], "semantic": [], "section": []}

            # Pool: anchor + all four expansion types
            pool = (
                [anchor]
                + expansion["structural"]
                + expansion["semantic"]
                + expansion["section"]
                + expansion.get("related", [])
            )

            # Dedup by chunk_id — keep first occurrence (anchor wins on tie)
            seen: Dict[Any, Dict] = {}
            for c in pool:
                cid = c.get("chunk_id")
                if cid is None:
                    seen[id(c)] = c
                elif cid not in seen:
                    seen[cid] = c
            all_chunks = list(seen.values())

            # Stitch adjacent chunks within the group
            stitched = _stitch_chunks(all_chunks)

            groups.append({
                "anchor":          anchor,
                "all_chunks":      all_chunks,
                "stitched_chunks": stitched,
                "group_score":     0.0,
            })

        return groups

    async def _expand_anchor(
        self,
        anchor: Dict,
        existing_chunks: List[Dict],
    ) -> Dict[str, List[Dict]]:
        """
        Expand a single anchor using all three strategies.
        Returns {"structural": [...], "semantic": [...], "section": [...]}.
        All failures are caught per-strategy so one bad API call doesn't
        block the other two.
        """
        doc_id         = anchor.get("document_id")
        chunk_index    = anchor.get("chunk_index")
        rel            = anchor.get("relevance_score", 0)
        anchor_text    = anchor.get("chunk_text") or ""
        anchor_heading = (anchor.get("heading") or "").strip().lower()
        anchor_id      = anchor.get("chunk_id")

        structural: List[Dict] = []
        semantic:   List[Dict] = []
        section:    List[Dict] = []

        # ── 1. Structural expansion (chunk_index ± window) ─────────── #
        if doc_id is not None and chunk_index is not None and COMPANY_URL:
            if rel >= ANCHOR_THRESHOLD_HIGH:
                window = 2
            elif rel >= ANCHOR_THRESHOLD_MEDIUM:
                window = 1
            else:
                window = 0
            if window > 0:
                try:
                    url = f"{COMPANY_URL}/aiagentchat/knowledgebase/context/expand"
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(
                            url,
                            headers={
                                "Authorization": f"Bearer {self.token}",
                                "Content-Type":  "application/json",
                                "companyIds":    f"[{self.company_id}]",
                            },
                            json={"anchors": [{"doc_id": doc_id, "chunk_index": chunk_index, "window": window}]},
                        )
                    if resp.status_code == 200:
                        raw = (resp.json().get("data") or {}).get("chunks") or []
                        for c in raw:
                            fc = self._format_chunk(c)
                            fc["_is_neighbor"]  = True
                            fc["_expand_type"]  = "structural"
                            structural.append(fc)
                except Exception as e:
                    logger.warning(
                        f"Structural expansion failed doc{doc_id}[{chunk_index}]: {e}"
                    )

        # ── 2. Semantic expansion (vector search on anchor text) ────── #
        if anchor_text and doc_id is not None:
            try:
                raw_sem = await self._execute_search(
                    query=anchor_text,
                    document_ids=[doc_id],
                    limit=4,
                    doc_type=None,
                )
                anc_words = (
                    set(re.findall(r"\w+", anchor_text.lower())) - _STOP_WORDS
                )
                for c in (raw_sem or []):
                    fc = self._format_chunk(c)
                    if fc.get("chunk_id") == anchor_id:
                        continue   # skip self
                    # Skip near-duplicates of the anchor (Jaccard > 0.75)
                    cand_words = set(re.findall(r"\w+", (fc.get("chunk_text") or "").lower())) - _STOP_WORDS
                    if cand_words and anc_words:
                        union = len(cand_words | anc_words)
                        if union > 0 and (len(cand_words & anc_words) / union) > 0.75:
                            continue
                    fc["_is_neighbor"]  = True
                    fc["_expand_type"]  = "semantic"
                    semantic.append(fc)
            except Exception as e:
                logger.warning(f"Semantic expansion failed doc{doc_id}: {e}")

        # ── 3. Section expansion (same heading, free from existing pool) ── #
        if anchor_heading:
            for c in existing_chunks:
                if c.get("chunk_id") == anchor_id:
                    continue
                if (c.get("heading") or "").strip().lower() == anchor_heading:
                    sc = dict(c)
                    sc["_is_neighbor"]  = True
                    sc["_expand_type"]  = "section"
                    section.append(sc)

        # ── 4. Related-chunk expansion (semantic cross-links at index time) ─ #
        # related_chunk_ids is stored as a JSON string in Milvus, e.g.
        # '["1234_chunk_5","1234_chunk_12"]' — computed via pairwise cosine
        # similarity across all chunks in the document at processing time.
        # This finds semantically relevant content even when it is not
        # adjacent and does not share a heading — directly bridges intro,
        # explanation, and example fragments that can be chapters apart.
        related: List[Dict] = []
        raw_related = anchor.get("related_chunk_ids")
        if raw_related:
            try:
                related_ids: List[str] = (
                    json.loads(raw_related)
                    if isinstance(raw_related, str)
                    else list(raw_related)
                )
            except Exception:
                related_ids = []
            if related_ids:
                fetched_related = await self._fetch_chunks_by_ids(related_ids)
                for fc in fetched_related:
                    if fc.get("chunk_id") == anchor_id:
                        continue   # skip self (shouldn't happen but be safe)
                    fc["_is_neighbor"]  = True
                    fc["_expand_type"]  = "related"
                    related.append(fc)

        logger.info(
            f"🧩 Anchor doc{doc_id}[{chunk_index}] rel={rel:.0f}%: "
            f"{len(structural)} structural + {len(semantic)} semantic + "
            f"{len(section)} section + {len(related)} related"
        )
        return {
            "structural": structural,
            "semantic":   semantic,
            "section":    section,
            "related":    related,
        }

    # ------------------------------------------------------------------ #
    #  Keyword-graph expansion                                            #
    # ------------------------------------------------------------------ #

    async def _keyword_expansion(
        self, query: str, document_ids: List
    ) -> List[Dict]:
        """
        Exact/rare-term expansion via the keyword inverted index built at ingestion.

        Extracts non-stop words from the query, sends them to the keyword
        lookup endpoint, receives matching chunk IDs (pre-computed at index time),
        then fetches and returns those chunks from Milvus.

        This path is orthogonal to vector search:
          • Vector search finds semantically similar content (paraphrase-robust)
          • Keyword expansion finds exact term matches (domain-term-robust)

        Example: query "how many cups of flour for pear tart"
          → keywords: ["cups", "flour", "pear", "tart"]
          → keyword index returns chunk IDs from pear tart recipe
          → even if the recipe says "200g flour" (numeric, not cups),
            the "pear" + "tart" exact match still surfaces it

        Non-fatal — returns [] on any failure.
        """
        if not COMPANY_URL:
            return []

        # Extract non-stop query words (≥3 chars, no punctuation)
        query_keywords = [
            w for w in re.findall(r"\b[a-z]{3,}\b", query.lower())
            if w not in _STOP_WORDS
        ]
        if not query_keywords:
            return []

        try:
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/keywords/lookup"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type":  "application/json",
                        "companyIds":    f"[{self.company_id}]",
                    },
                    json={
                        "keywords":     query_keywords[:15],   # cap
                        "document_ids": [int(d) for d in document_ids if str(d).isdigit()],
                    },
                )
            if resp.status_code != 200:
                return []

            chunk_ids: List[str] = (resp.json().get("data") or {}).get("chunk_ids") or []
            if not chunk_ids:
                return []

            fetched = await self._fetch_chunks_by_ids(chunk_ids)
            for fc in fetched:
                fc["_is_neighbor"]  = True
                fc["_expand_type"]  = "keyword"
            logger.info(
                f"🔑 Keyword expansion: {len(query_keywords)} term(s) "
                f"→ {len(chunk_ids)} id(s) → {len(fetched)} chunk(s)"
            )
            return fetched

        except Exception as e:
            logger.warning(f"Keyword expansion failed (non-fatal): {e}")
            return []

    # ------------------------------------------------------------------ #
    #  Section node expansion                                             #
    # ------------------------------------------------------------------ #

    async def _expand_section_nodes(self, chunks: List[Dict]) -> List[Dict]:
        """
        Detect virtual section-node chunks returned by Milvus search and
        replace them with their constituent real chunks.

        A section node (chunk_type == 'section_node') is a virtual record
        whose embedding is the centroid of all chunks in a logical section.
        It stores the real chunk IDs in `section_chunk_ids` (JSON array).

        When a section node matches a query, it means the *entire section*
        is relevant — expanding it gives the LLM complete context without
        relying on ±window guesses.

        Non-fatal: section nodes that fail to expand are left in the list
        as best-effort context (their text field contains the first-chunk
        preview stored at index time).
        """
        real:    List[Dict] = []
        expanded = False

        for chunk in chunks:
            if chunk.get("chunk_type") != "section_node":
                real.append(chunk)
                continue

            # Parse section_chunk_ids
            raw = chunk.get("section_chunk_ids")
            ids: List[str] = []
            if raw:
                try:
                    ids = json.loads(raw) if isinstance(raw, str) else list(raw)
                except Exception:
                    ids = []

            if not ids:
                real.append(chunk)   # fallback: keep node as-is
                continue

            fetched = await self._fetch_chunks_by_ids(ids)
            if fetched:
                expanded = True
                # Mark section-expanded chunks for re-ranking boost
                for fc in fetched:
                    fc["_is_neighbor"]  = True
                    fc["_expand_type"]  = "section_node"
                logger.info(
                    f"🗂️ Section node expanded: heading='{chunk.get('heading','?')}' "
                    f"→ {len(fetched)} real chunk(s)"
                )
                real.extend(fetched)
            else:
                real.append(chunk)   # expansion failed — keep node preview

        if expanded:
            # Dedup after possible overlap between section and other results
            return list({
                c.get("chunk_id", id(c)): c for c in real
            }.values())
        return real

    # ------------------------------------------------------------------ #
    #  Related-chunk fetch by primary key                                  #
    # ------------------------------------------------------------------ #

    async def _fetch_chunks_by_ids(self, chunk_ids: List[str]) -> List[Dict]:
        """
        Retrieve specific chunks by their Milvus primary-key IDs via the
        POST /knowledgebase/chunks/fetch endpoint.

        Used exclusively by the related-chunk expansion strategy — chunk IDs
        were pre-computed at index time via pairwise cosine similarity so
        this call is a cheap key-lookup, not a new vector search.

        Non-fatal on any error; returns [] on failure.
        """
        if not chunk_ids or not COMPANY_URL:
            return []
        try:
            url = f"{COMPANY_URL}/aiagentchat/knowledgebase/chunks/fetch"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type":  "application/json",
                        "companyIds":    f"[{self.company_id}]",
                    },
                    json={"chunk_ids": chunk_ids},
                )
            if resp.status_code == 200:
                raw = (resp.json().get("data") or {}).get("chunks") or []
                return [self._format_chunk(c) for c in raw]
            logger.warning(f"_fetch_chunks_by_ids HTTP {resp.status_code} — skipping")
        except Exception as e:
            logger.warning(f"_fetch_chunks_by_ids failed (non-fatal): {e}")
        return []

    # ------------------------------------------------------------------ #
    #  Deep fan-out fetch                                                  #
    # ------------------------------------------------------------------ #

    async def _deep_fetch(
        self,
        query: str,
        document_ids: List,
        max_results: int,
        query_doc_type: Optional[str],
    ) -> List[Dict]:
        sub_queries = await self._decompose_query(query, query_doc_type)
        logger.info(f"🔀 Deep fetch: {len(sub_queries)} sub-queries (style={query_doc_type or 'general'})")

        results = await asyncio.gather(
            *[self._execute_search(sq, document_ids, max_results, query_doc_type)
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
        if not self.llm_provider:
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
        if not self.llm_provider:
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
        if not self.llm_provider:
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
        doc_type: Optional[str],
    ) -> Optional[List[Dict]]:
        # ── Fix 1: enforce search budget ────────────────────────────── #
        self._search_count += 1
        if self._search_count > MAX_TOTAL_SEARCHES:
            logger.warning(
                f"🚫 Search budget exhausted ({MAX_TOTAL_SEARCHES} calls) — "
                f"skipping query '{query[:60]}'"
            )
            return []

        url     = f"{COMPANY_URL}/aiagentchat/knowledgebase/search"
        payload: Dict[str, Any] = {
            "query":        query,
            "document_ids": document_ids,
            "limit":        limit,
            # project_fid intentionally NOT sent:
            # The Node.js API builds a Milvus filter `project_fid == <value>` when
            # this field is present. Chunks are stored with the project's project_fid
            # (often 0 or the ingestion-time value) which does NOT match agent_id.
            # Sending it would silently exclude ALL chunks → empty results.
            # document_ids scoping is sufficient to constrain the search.
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
            "doc_type":           chunk.get("doc_type"),
            "language":           chunk.get("language"),
            "page_number":        chunk.get("page_number"),
            # Semantic cross-links stored at index time (JSON array string or list)
            "related_chunk_ids":  chunk.get("related_chunk_ids"),
            # Section-level virtual node field: JSON list of constituent chunk IDs
            # Only present when chunk_type == 'section_node'
            "section_chunk_ids":  chunk.get("section_chunk_ids"),
            # Cohesion group: parent_id or section_path hash — used by _rerank
            # to boost chunks that belong to the same logical unit as top anchors
            "cohesion_group":     chunk.get("cohesion_group"),
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
