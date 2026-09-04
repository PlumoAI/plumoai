"""
SelfLearner — implicit behavioral signal extraction from conversation exchanges.

The standard MemoryAgentTool stores things explicitly when the user states facts.
SelfLearner observes exchanges NON-INTRUSIVELY and extracts IMPLICIT signals:
  • Response format preferences revealed by follow-up patterns
  • Language/tone inferred from how the user writes
  • Topic interests visible from question patterns
  • Implicit corrections (rephrases, clarifications) → format lesson
  • Tool-usage patterns from successful executions

These are stored with type "implicit:{signal_type}" — a distinct namespace from
explicit identity facts — so they can be recalled, filtered, and decayed separately.

Budget gate: always checks remaining LLM budget before firing; skipped when low.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, Optional, Tuple

from .constants import (
    BUDGET_RESERVE_FOR_LEARN,
    EXCHANGE_BUFFER_SIZE,
    SELF_LEARN_CONFIDENCE_THRESHOLD,
)
from .json_utils import parse_json_response

if TYPE_CHECKING:
    from .memory_agent_tool import MemoryAgentTool

logger = logging.getLogger(__name__)

# Signal types the extraction LLM may classify
_IMPLICIT_SIGNAL_TYPES = frozenset({
    "response_format_preference",
    "detail_preference",
    "language_preference",
    "topic_interest",
    "implicit_correction",
    "communication_style",
    "tool_usage_preference",
})


class SelfLearner:
    """
    Observes conversation exchanges and tool results for implicit behavioral signals.
    Budget-gated to never deplete the shared LLM call budget.
    """

    def __init__(
        self,
        agent: "MemoryAgentTool",
        llm_provider: Any,
        budget_getter: Optional[Any] = None,
    ) -> None:
        """
        agent: the MemoryAgentTool instance
        llm_provider: raw provider (unused directly; agent._llm is used)
        budget_getter: optional callable() -> int of remaining LLM budget
        """
        self._agent = agent
        self._llm = agent._llm  # MemoryLlmClient
        self._budget_getter = budget_getter

        # Rolling buffer of (user_msg, assistant_msg) pairs
        self._exchange_buffer: Deque[Tuple[str, str]] = deque(maxlen=EXCHANGE_BUFFER_SIZE)

    def _budget_ok(self) -> bool:
        if self._budget_getter is None:
            return True
        try:
            remaining = self._budget_getter()
            return int(remaining) >= BUDGET_RESERVE_FOR_LEARN
        except Exception:
            return True

    async def learn_from_exchange(
        self,
        *,
        user_msg: str,
        assistant_msg: str,
        conversation: str = "",
    ) -> int:
        """
        Extract implicit behavioral signals from the rolling exchange buffer.
        Returns the number of signals successfully stored.
        """
        if not user_msg and not assistant_msg:
            return 0
        if not self._budget_ok():
            logger.debug("SelfLearner.learn_from_exchange: skipped (budget low)")
            return 0

        if user_msg or assistant_msg:
            self._exchange_buffer.append(
                (user_msg.strip()[:400], assistant_msg.strip()[:400])
            )

        if not self._exchange_buffer:
            return 0

        exchange_block = "\n\n".join(
            f"User: {u}\nAssistant: {a}"
            for u, a in self._exchange_buffer
            if u or a
        )
        allowed_types = ", ".join(sorted(_IMPLICIT_SIGNAL_TYPES))

        prompt = f"""You are an implicit behavioral signal extractor for an AI memory system.

Read this exchange history and identify IMPLICIT signals about the user's preferences —
things NOT explicitly stated, but clearly revealed through how they interact.

Exchange history (most recent last):
{exchange_block[:2000]}

━━━ RULES ━━━
- ONLY implicit signals (skip anything explicitly stated as a preference or fact)
- ONLY extract if confidence ≥ {SELF_LEARN_CONFIDENCE_THRESHOLD}
- Allowed signal types: {allowed_types}
- Each signal: one atomic, self-contained behavioral rule (no pronouns — write in third person)
- Max 2 signals per call to minimize noise
- Return empty signals array if no high-confidence implicit signals exist

Respond ONLY with valid JSON:
{{
  "signals": [
    {{
      "content": "prefers concise answers under 3 sentences for factual questions",
      "type": "response_format_preference",
      "confidence": 0.82
    }}
  ]
}}"""

        raw = await self._llm.generate_json(prompt, max_tokens=400)
        if not raw:
            return 0

        parsed = (
            parse_json_response(raw, required_keys=["signals"], defaults={"signals": []})
            or {}
        )
        signals = parsed.get("signals") or []

        stored_count = 0
        for sig in signals:
            if not isinstance(sig, dict):
                continue
            content = (sig.get("content") or "").strip()
            sig_type = (sig.get("type") or "implicit_behavior").strip()
            try:
                confidence = float(sig.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0

            if not content or confidence < SELF_LEARN_CONFIDENCE_THRESHOLD:
                continue

            # Normalise to allowed signal type or fall back to generic
            if sig_type not in _IMPLICIT_SIGNAL_TYPES:
                sig_type = "implicit_behavior"

            try:
                result = await self._agent._op_store(
                    content=content,
                    type_hint=f"implicit:{sig_type}",
                    raw_context=None,
                    active_goal=None,
                )
                if result.get("stored") or result.get("stored_multiple"):
                    stored_count += 1
                    logger.debug(
                        "SelfLearner: stored implicit signal [%s] conf=%.2f: %s",
                        sig_type,
                        confidence,
                        content[:80],
                    )
            except Exception as exc:
                logger.warning("SelfLearner: failed to store implicit signal: %s", exc)

        return stored_count

    async def learn_from_tool_result(
        self,
        *,
        tool_name: str,
        tool_code: str,
        query: str,
        result: Any,
        success: bool,
    ) -> int:
        """
        Extract reusable tool-specific preferences from a successful tool execution.
        Only fires for known/connected tool codes; skipped on failures.
        Returns number of signals stored (0 or 1).
        """
        if not success or not tool_code:
            return 0
        if tool_code.strip().lower() not in self._agent._effective_tool_codes():
            return 0
        if not self._budget_ok():
            return 0

        try:
            if isinstance(result, dict):
                result_summary = str(result)[:400]
            elif isinstance(result, list):
                result_summary = f"list of {len(result)} items: {str(result[:2])[:200]}"
            else:
                result_summary = str(result)[:400]
        except Exception:
            result_summary = "(unparseable)"

        prompt = f"""You are a tool-usage pattern extractor for an AI memory system.

Tool used: {tool_name} (code: {tool_code})
User query: {query[:200]}
Result summary: {result_summary}

TASK: Identify ONE reusable tool-usage preference or behavioral pattern from this execution
that would improve FUTURE uses of this same tool for the same user.

Good signals: format preferences, naming conventions, scheduling habits, output style choices.
Skip: one-time task details, general tool facts, anything not reusable across sessions.

Respond ONLY with valid JSON:
{{
  "found": true | false,
  "content": "...",
  "confidence": 0.0
}}"""

        raw = await self._llm.generate_json(prompt, max_tokens=200)
        if not raw:
            return 0

        parsed = (
            parse_json_response(
                raw,
                required_keys=["found", "content", "confidence"],
                defaults={"found": False, "content": "", "confidence": 0.0},
            )
            or {}
        )
        if not parsed.get("found"):
            return 0

        content = (parsed.get("content") or "").strip()
        try:
            confidence = float(parsed.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0

        if not content or confidence < SELF_LEARN_CONFIDENCE_THRESHOLD:
            return 0

        try:
            store_result = await self._agent._op_store(
                content=content,
                type_hint=f"{tool_code}:usage_preference",
                raw_context=None,
                active_goal=None,
            )
            if store_result.get("stored") or store_result.get("stored_multiple"):
                logger.debug(
                    "SelfLearner: stored tool-scope signal [%s] conf=%.2f: %s",
                    tool_code,
                    confidence,
                    content[:80],
                )
                return 1
        except Exception as exc:
            logger.warning("SelfLearner: failed to store tool-scope signal: %s", exc)

        return 0
