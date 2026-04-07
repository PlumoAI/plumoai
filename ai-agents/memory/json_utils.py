from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


def strip_llm_json(text: str) -> str:
    """Remove markdown fences, inline JS comments, and trailing commas."""
    # Strip code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.strip().strip("`").strip()
    # Strip single-line // comments inside JSON (common LLM artefact)
    text = re.sub(r"//[^\n]*", "", text)
    # Remove trailing commas before } or ] (invalid JSON but common LLM output)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text.strip()


def parse_json_response(
    text: str,
    *,
    required_keys: Optional[List[str]] = None,
    defaults: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Tolerant JSON parser.
    1. Strip fences / comments / trailing commas.
    2. json.loads — fast path.
    3. Regex-extract first {...} block — fallback.
    4. Validate and backfill required_keys with defaults.
    """
    if not text:
        return None
    cleaned = strip_llm_json(text)
    parsed: Optional[Dict[str, Any]] = None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                candidate = strip_llm_json(m.group())
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                parsed = None

    if not isinstance(parsed, dict):
        return None

    if required_keys:
        fill = defaults or {}
        for key in required_keys:
            if key not in parsed:
                parsed[key] = fill.get(key)

    return parsed

