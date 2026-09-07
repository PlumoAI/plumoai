from __future__ import annotations

from .schedule_node import ScheduleTriggerNode


def create_node() -> ScheduleTriggerNode:
    """Entrypoint for the Schedule Trigger node. Fully self-contained: no external dependencies."""
    return ScheduleTriggerNode()
