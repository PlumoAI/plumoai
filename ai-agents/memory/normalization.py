from __future__ import annotations

from typing import Any, Dict, List


def normalize_memory(m: Any) -> Dict[str, Any]:
    """
    Guarantee a consistent memory dict shape regardless of API version.
    Resolves _id / id, createdAt / created_at, and provides zero-value defaults.
    """
    if not isinstance(m, dict):
        return {
            "memory_id": None,
            "content": "",
            "type": "fact",
            "scope": "personal",
            "importance_score": 0.0,
            "access_count": 0,
            "tags": [],
            "scores": {},
            "raw_context": None,
            "createdAt": None,
            "last_accessed_at": None,
            "_id": None,
        }
    mem_id = m.get("_id") or m.get("id") or m.get("memory_id")
    created = m.get("createdAt") or m.get("created_at")
    raw_scope = str(m.get("scope") or "personal").strip().lower()
    return {
        "memory_id": mem_id,
        "_id": mem_id,  # backward-compat alias
        "content": m.get("content") or "",
        "type": m.get("type") or "fact",
        "scope": "shared" if raw_scope == "shared" else "personal",
        "importance_score": float(m.get("importance_score") or 0),
        "access_count": int(m.get("access_count") or 0),
        "tags": m.get("tags") or [],
        "scores": m.get("scores") or {},
        "raw_context": m.get("raw_context"),
        "createdAt": created,
        "last_accessed_at": m.get("last_accessed_at") or created,
    }


def normalize_memories(memories: List[Any]) -> List[Dict[str, Any]]:
    return [normalize_memory(m) for m in (memories or []) if m]

