"""
MemoryCallbackEngine — push-model auto-trigger for periodic memory maintenance.

Pattern: callers notify after operations; no background threads; no polling.
  on_store()       → called after each successful memory store
  on_exchange()    → called after each user/assistant turn pair
  on_session_end() → called once per session lifecycle end

Internal counters drive two automatic behaviours:
  • Auto-reflect: every AUTO_REFLECT_AFTER_STORES successful stores
  • Auto-learn:   every AUTO_LEARN_AFTER_EXCHANGES exchanges (via SelfLearner)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from .constants import (
    AUTO_LEARN_AFTER_EXCHANGES,
    AUTO_REFLECT_AFTER_STORES,
    SESSION_END_MIN_STORES,
)

if TYPE_CHECKING:
    from .memory_agent_tool import MemoryAgentTool
    from .self_learner import SelfLearner

logger = logging.getLogger(__name__)


class MemoryCallbackEngine:
    """
    Push-model counter-based auto-trigger for reflect and learn operations.
    No background threads — all triggers run in-line from caller notification.
    """

    def __init__(
        self,
        agent: "MemoryAgentTool",
        self_learner: Optional["SelfLearner"] = None,
    ) -> None:
        self._agent = agent
        self._self_learner = self_learner

        # Session-scoped counters
        self._stores_since_reflect: int = 0
        self._exchanges_since_learn: int = 0
        self._total_stores_session: int = 0

    def attach_self_learner(self, self_learner: "SelfLearner") -> None:
        self._self_learner = self_learner

    async def on_store(self, stored: bool) -> None:
        """
        Notify after each store attempt.
        Fires auto-reflect every AUTO_REFLECT_AFTER_STORES successful stores.
        """
        if not stored:
            return
        self._stores_since_reflect += 1
        self._total_stores_session += 1

        if self._stores_since_reflect >= AUTO_REFLECT_AFTER_STORES:
            self._stores_since_reflect = 0
            logger.info(
                "MemoryCallbackEngine: auto-reflect triggered after %d stores",
                AUTO_REFLECT_AFTER_STORES,
            )
            try:
                await self._agent._op_reflect()
            except Exception as exc:
                logger.warning("MemoryCallbackEngine: auto-reflect failed: %s", exc)

    async def on_exchange(
        self,
        *,
        user_msg: str,
        assistant_msg: str,
        conversation: str = "",
    ) -> None:
        """
        Notify after each user/assistant exchange pair.
        Every AUTO_LEARN_AFTER_EXCHANGES exchanges triggers self_learner.learn_from_exchange().
        """
        self._exchanges_since_learn += 1

        if (
            self._self_learner is not None
            and self._exchanges_since_learn >= AUTO_LEARN_AFTER_EXCHANGES
        ):
            self._exchanges_since_learn = 0
            logger.debug(
                "MemoryCallbackEngine: auto-learn triggered after %d exchanges",
                AUTO_LEARN_AFTER_EXCHANGES,
            )
            try:
                count = await self._self_learner.learn_from_exchange(
                    user_msg=user_msg,
                    assistant_msg=assistant_msg,
                    conversation=conversation,
                )
                if count:
                    logger.info(
                        "MemoryCallbackEngine: auto-learn stored %d implicit signal(s)",
                        count,
                    )
            except Exception as exc:
                logger.warning("MemoryCallbackEngine: auto-learn failed: %s", exc)

    async def on_session_end(self, conversation: str = "") -> None:
        """
        Notify when the session lifecycle ends.
        Fires a final reflect when enough stores accumulated this session.
        """
        if self._total_stores_session >= SESSION_END_MIN_STORES:
            logger.info(
                "MemoryCallbackEngine: session-end reflect triggered (%d stores this session)",
                self._total_stores_session,
            )
            try:
                await self._agent._op_reflect()
            except Exception as exc:
                logger.warning("MemoryCallbackEngine: session-end reflect failed: %s", exc)

    def stats(self) -> Dict[str, int]:
        return {
            "total_stores_session": self._total_stores_session,
            "stores_since_reflect": self._stores_since_reflect,
            "exchanges_since_learn": self._exchanges_since_learn,
        }
