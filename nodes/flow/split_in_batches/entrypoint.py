from __future__ import annotations

from .split_in_batches_node import SplitInBatchesNode


def create_node() -> SplitInBatchesNode:
    """Entrypoint for the Split In Batches node."""
    return SplitInBatchesNode()
