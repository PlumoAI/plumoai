from __future__ import annotations

from .remove_duplicates_node import RemoveDuplicatesNode


def create_node() -> RemoveDuplicatesNode:
    """Entrypoint for the Remove Duplicates node. Fully self-contained: no external dependencies."""
    return RemoveDuplicatesNode()
