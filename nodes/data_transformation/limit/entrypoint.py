from __future__ import annotations

from .limit_node import LimitNode


def create_node() -> LimitNode:
    """Entrypoint for the Limit node. Fully self-contained: no external dependencies."""
    return LimitNode()
