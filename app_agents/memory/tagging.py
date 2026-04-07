from __future__ import annotations

import re
from typing import Dict, List, Optional, Set


KNOWN_TOOL_CODES: Set[str] = {
    "ai_writer",
    "gmail",
    "call",
    "scheduler",
    "chartmaker",
    "websearch",
    "memory",
    "workflow_builder",
    "calculator_datetime",
}


def extract_tags(content: str, mem_type: Optional[str], *, effective_tool_codes: Set[str]) -> List[str]:
    """
    Extract tags from content:
    - Always include the memory type label.
    - If type has TOOL-SCOPE prefix (e.g. "ai_writer:preference"), add "tool_scope:ai_writer".
    - Capture Title-Case words / phrases and ALL-CAPS acronyms.
    - Deduplicate (preserve order) and cap at 8.
    """
    tags: List[str] = []
    if mem_type:
        tags.append(mem_type.lower())
        _type_lower = mem_type.lower()
        if ":" in _type_lower:
            _tool_prefix = _type_lower.split(":")[0].strip()
            if _tool_prefix in effective_tool_codes:
                _scope_tag = f"tool_scope:{_tool_prefix}"
                if _scope_tag not in tags:
                    tags.insert(0, _scope_tag)

    _STOP = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "i",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "it",
        "this",
        "that",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "also",
    }

    multi_caps = re.findall(r"\b(?:[A-Z][a-zA-Z]{1,}(?:\s|-))+[A-Z][a-zA-Z]{1,}\b", content or "")
    for phrase in multi_caps:
        phrase_clean = phrase.strip().rstrip("-")
        if phrase_clean.lower() not in _STOP:
            tags.append(phrase_clean)

    singles = re.findall(r"\b[A-Z][a-z]{2,}(?:-[A-Z][a-z]+)*\b", content or "")
    for word in singles:
        if word.lower() not in _STOP:
            tags.append(word)

    acronyms = re.findall(r"\b[A-Z]{3,4}\b", content or "")
    tags.extend(acronyms)

    seen: Dict[str, bool] = {}
    result: List[str] = []
    for t in tags:
        key = t.lower()
        if key not in seen:
            seen[key] = True
            result.append(t)
    return result[:8]

