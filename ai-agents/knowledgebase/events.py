from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict


class AgentEvent:
    """Event types for agent communication."""

    THOUGHT = "thought"
    PLAN = "plan"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    RESULT = "result"
    ERROR = "error"
    FINAL = "final"


def event(event_type: str, content: Any) -> Dict[str, Any]:
    """Create an event dictionary."""

    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "content": content,
    }

