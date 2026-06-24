from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict


def _jsonable(content: Any) -> Any:
    if is_dataclass(content):
        return asdict(content)
    return content


def event(event_type: str, content: Any) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "content": _jsonable(content),
    }

