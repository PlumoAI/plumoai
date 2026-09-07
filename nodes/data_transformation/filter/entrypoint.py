from __future__ import annotations

from .filter_node import FilterNode


def create_node() -> FilterNode:
    """Entrypoint for the Filter node. Fully self-contained: no external dependencies."""
    return FilterNode()
