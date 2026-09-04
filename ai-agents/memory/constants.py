from __future__ import annotations

import math
import os

# Memory API base URL (same env used across the backend)
COMPANY_URL = os.getenv("COMPANY_URL", "")

# ─── Storage heuristics / thresholds ──────────────────────────────────────────

# Global fallback threshold (LLM usually provides per-content thresholds)
IMPORTANCE_THRESHOLD = 0.5

# Days before unused memories begin to decay
DECAY_DAYS_UNUSED = 90

# Jaccard thresholds
DUPLICATE_OVERLAP_THRESHOLD = 0.72
POST_SUMMARY_DUPLICATE_THRESHOLD = 0.50
CONTRADICTION_OVERLAP_MIN = 0.30

# Network
API_TIMEOUT = 15.0

# Retrieval frequency normalisation ceiling (log scale)
_FREQ_LOG_CEIL = math.log1p(100)  # saturates at ~100 accesses → 1.0

# ─── Callback engine thresholds ───────────────────────────────────────────────

# Auto-reflect fires every N successful stores within a session
AUTO_REFLECT_AFTER_STORES: int = 10

# Auto-learn fires every N user/assistant exchange pairs
AUTO_LEARN_AFTER_EXCHANGES: int = 5

# Session-end reflect only fires if at least N stores happened this session
SESSION_END_MIN_STORES: int = 3

# ─── SelfLearner thresholds ───────────────────────────────────────────────────

# Rolling buffer of exchange pairs kept for implicit signal extraction
EXCHANGE_BUFFER_SIZE: int = 8

# Minimum LLM calls remaining before SelfLearner is allowed to fire
BUDGET_RESERVE_FOR_LEARN: int = 5

# Minimum confidence for an implicit signal to be stored
SELF_LEARN_CONFIDENCE_THRESHOLD: float = 0.75

