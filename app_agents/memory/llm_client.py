from __future__ import annotations

import logging
from typing import Any, Optional

from .json_utils import strip_llm_json

logger = logging.getLogger(__name__)


class MemoryLlmClient:
    def __init__(self, llm_provider: Any):
        self._llm = llm_provider

    async def generate_json(self, prompt: str, *, max_tokens: int = 600) -> Optional[str]:
        """
        Call the LLM provider for internal memory reasoning.
        Uses get_response() — the same interface used elsewhere in the codebase.
        Returns raw text (expected JSON) or None on failure/unavailability.
        """
        if not self._llm:
            return None
        try:
            if hasattr(self._llm, "get_response"):
                result = await self._llm.get_response(
                    transcript=prompt,
                    system_prompt=(
                        "You are a precise JSON-only responder for an AI memory system. "
                        "Output only valid JSON. No markdown, no explanation."
                    ),
                    max_tokens=max_tokens,
                )
                out = (result or "").strip()
                return strip_llm_json(out) if out else None
        except Exception as exc:
            logger.warning("Memory LLM call failed: %s", exc)
        return None

