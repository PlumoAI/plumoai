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

