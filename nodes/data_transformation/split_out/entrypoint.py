from __future__ import annotations

from .split_out_node import SplitOutNode


def create_node() -> SplitOutNode:
    """Entrypoint for the Split Out node. Fully self-contained: no external dependencies."""
    return SplitOutNode()
