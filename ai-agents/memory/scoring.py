from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .constants import DECAY_DAYS_UNUSED, IMPORTANCE_THRESHOLD, _FREQ_LOG_CEIL

logger = logging.getLogger(__name__)


def log_freq(count: int) -> float:
    """
    Normalised log frequency signal.
    log(1 + count) / log(1 + 5) saturates at count=5 → 1.0.
    """
    return round(min(math.log1p(count) / math.log1p(5), 1.0), 4)


def count_similar_in_list(content: str, memories: List[Dict]) -> int:
    """Count memories with > 35% Jaccard word-overlap with `content`."""
    cw = set(re.findall(r"\b\w{3,}\b", (content or "").lower()))
    count = 0
    for m in memories or []:
        mw = set(re.findall(r"\b\w{3,}\b", (m.get("content") or "").lower()))
        union = len(cw | mw)
        if union and len(cw & mw) / union > 0.35:
            count += 1
    return count


def compute_retrieval_boost(memory: Dict) -> float:
    """
    Dynamic boost: 0.1 × log(1 + access_count) / log(101)
    Saturates at 0.1 after ~100 retrievals.
    """
    access_count = float(memory.get("access_count") or 0)
    return round(0.1 * min(math.log1p(access_count) / _FREQ_LOG_CEIL, 1.0), 4)


def compute_decay_factor(memory: Dict) -> Optional[float]:
    """
    Return decayed importance if unused for DECAY_DAYS_UNUSED days, else None.
    Uses fractional days for sub-day accuracy. Result clamped to [0.05, 1.0].
    """
    anchor = memory.get("last_accessed_at") or memory.get("createdAt")
    if not anchor:
        return None
    try:
        la = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
        days_unused = (datetime.now(timezone.utc) - la).total_seconds() / 86400.0
        if days_unused > DECAY_DAYS_UNUSED:
            decay = (days_unused - DECAY_DAYS_UNUSED) / 180.0 * 0.3
            base = float(memory.get("importance_score") or 0)
            return round(max(0.05, min(1.0, base - decay)), 4)
    except (ValueError, TypeError):
        return None
    return None


def effective_threshold(*, base_threshold: float, active_goal: Optional[str], eval_result: Dict) -> float:
    """
    Use the threshold the LLM set in eval_result['threshold'].
    Fallback chain: LLM threshold → instance base_threshold → module IMPORTANCE_THRESHOLD.
    If active_goal overlaps content summary or goal_relevance is high, reduce by 15%.
    """
    llm_threshold = eval_result.get("threshold")
    base: Optional[float]
    if llm_threshold is not None:
        try:
            base = float(llm_threshold)
        except (TypeError, ValueError):
            base = None
    else:
        base = None

    if base is None or base <= 0:
        base = base_threshold if base_threshold > 0 else IMPORTANCE_THRESHOLD

    goal_applied = False
    if active_goal and active_goal.strip():
        goal_words = set(re.findall(r"\b\w{3,}\b", active_goal.lower()))
        content_words = set(re.findall(r"\b\w{3,}\b", (eval_result.get("summary") or "").lower()))
        goal_relevance = float(eval_result.get("goal_relevance") or 0)
        overlap_ratio = (len(goal_words & content_words) / len(goal_words)) if goal_words else 0.0
        if overlap_ratio > 0.25 or goal_relevance > 0.6:
            base = round(base * 0.85, 4)
            goal_applied = True

    base = round(max(0.05, min(0.99, base)), 4)
    logger.debug(
        "Threshold applied: llm_suggested=%.4f final=%.4f goal_coupling=%s",
        float(llm_threshold or 0),
        base,
        goal_applied,
    )
    return base


def compute_confidence(memory: Dict) -> float:
    """
    confidence = importance × recency × frequency
    Uses fractional days for smooth recency rather than integer jumps.
    """
    importance = max(0.0, min(1.0, float(memory.get("importance_score") or 0)))

    recency = 1.0
    anchor = memory.get("last_accessed_at") or memory.get("createdAt")
    if anchor:
        try:
            la = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
            days_ago = (datetime.now(timezone.utc) - la).total_seconds() / 86400.0
            recency = max(0.1, min(1.0, 1.0 - days_ago / 365.0))
        except (ValueError, TypeError):
            pass

    access_count = float(memory.get("access_count") or 0)
    frequency = max(0.1, min(1.0, math.log1p(access_count) / _FREQ_LOG_CEIL))

    return round(importance * recency * frequency, 4)


def score_relevance(query: str, memory: Dict) -> float:
    """Rank an API-returned memory: keyword overlap + importance + short-term recency bonus."""
    query_words = set(re.findall(r"\b\w{3,}\b", (query or "").lower()))
    mem_text = (memory.get("content") or "").lower()
    mem_words = set(re.findall(r"\b\w{3,}\b", mem_text))

    if not query_words:
        return float(memory.get("importance_score") or 0)

    overlap = len(query_words & mem_words) / len(query_words)
    importance_boost = float(memory.get("importance_score") or 0) * 0.3

    recency_bonus = 0.0
    anchor = memory.get("last_accessed_at")
    if anchor:
        try:
            la = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
            days_ago = (datetime.now(timezone.utc) - la).total_seconds() / 86400.0
            recency_bonus = max(0.0, (30.0 - days_ago) / 30.0) * 0.1
        except (ValueError, TypeError):
            pass

    return overlap + importance_boost + recency_bonus

