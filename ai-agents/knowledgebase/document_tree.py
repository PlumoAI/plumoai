"""
Document outline tree (PageIndex-inspired).

Builds a lightweight hierarchical view from search hits so the agent can
reason over structure (summaries + node ids) before pulling dense chunks.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def make_node_id(document_id: Any, chunk_index: Any) -> str:
    return f"{document_id}:{chunk_index}"


def parse_node_id(node_id: str) -> Tuple[Optional[int], Optional[int]]:
    if not node_id or ":" not in node_id:
        return None, None
    left, _, right = node_id.partition(":")
    try:
        return int(left), int(right)
    except (TypeError, ValueError):
        return None, None


def _heading_depth(heading_level: Any, section_path: Optional[str]) -> int:
    try:
        hl = int(heading_level)
        if 1 <= hl <= 6:
            return hl
    except (TypeError, ValueError):
        pass
    if section_path and isinstance(section_path, str):
        parts = [p.strip() for p in re.split(r"[>/|]", section_path) if p.strip()]
        return max(1, min(6, len(parts)))
    return 1


def _summary_from_chunk(chunk: Dict[str, Any], max_len: int = 220) -> str:
    text = (chunk.get("chunk_text") or "").strip().replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def normalize_outline_chunk(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Align raw API chunk keys with tree builder expectations."""
    doc_id = raw.get("document_id")
    idx = raw.get("chunk_index")
    return {
        "chunk_id": raw.get("id") or raw.get("chunk_id"),
        "chunk_index": idx,
        "document_id": doc_id,
        "title": raw.get("title") or "",
        "heading": (raw.get("heading") or "").strip(),
        "section_path": (raw.get("section_path") or "").strip(),
        "heading_level": raw.get("heading_level"),
        "page_number": raw.get("page_number") or raw.get("page"),
        "chunk_text": raw.get("chunk_text") or "",
        "keywords": raw.get("keywords") or "",
        "node_id": make_node_id(doc_id, idx),
    }


def merge_unique_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe by (document_id, chunk_index); keep the richest chunk_text."""
    best: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for c in chunks:
        n = normalize_outline_chunk(c) if not isinstance(c, dict) or "node_id" not in c else dict(c)
        if "node_id" not in n:
            n = normalize_outline_chunk(n)
        key = (n.get("document_id"), n.get("chunk_index"))
        prev = best.get(key)
        if prev is None or len(n.get("chunk_text") or "") > len(prev.get("chunk_text") or ""):
            best[key] = n
    return sorted(
        best.values(),
        key=lambda x: (x.get("document_id") or 0, x.get("chunk_index") if x.get("chunk_index") is not None else 0),
    )


def build_forest(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build one tree root list per document_id using heading_level / section_path.
    Each node: node_id, title, heading, page_number, chunk_index, chunk_id, summary, children.
    """
    by_doc: Dict[Any, List[Dict[str, Any]]] = {}
    for c in merge_unique_chunks(chunks):
        did = c.get("document_id")
        by_doc.setdefault(did, []).append(c)

    forest: List[Dict[str, Any]] = []
    for doc_id in sorted(by_doc.keys(), key=lambda x: (x is None, str(x))):
        doc_chunks = by_doc[doc_id]
        title = next((dc.get("title") for dc in doc_chunks if dc.get("title")), "") or f"Document {doc_id}"

        doc_chunks.sort(
            key=lambda x: (x.get("chunk_index") is None, x.get("chunk_index") if x.get("chunk_index") is not None else 0)
        )

        stack: List[Tuple[int, Dict[str, Any]]] = []
        roots: List[Dict[str, Any]] = []

        for dc in doc_chunks:
            depth = _heading_depth(dc.get("heading_level"), dc.get("section_path"))
            node = {
                "node_id": dc["node_id"],
                "document_id": doc_id,
                "title": dc.get("heading") or dc.get("section_path") or "(body)",
                "heading": dc.get("heading") or "",
                "section_path": dc.get("section_path") or "",
                "page_number": dc.get("page_number"),
                "chunk_index": dc.get("chunk_index"),
                "chunk_id": dc.get("chunk_id"),
                "summary": _summary_from_chunk(dc),
                "children": [],
            }
            while stack and stack[-1][0] >= depth:
                stack.pop()
            if not stack:
                roots.append(node)
            else:
                stack[-1][1]["children"].append(node)
            stack.append((depth, node))

        forest.append(
            {
                "document_id": doc_id,
                "document_title": title,
                "roots": roots,
            }
        )
    return forest


def _serialize_node(node: Dict[str, Any], indent: int = 0) -> List[str]:
    pad = "  " * indent
    line = (
        f"{pad}- [{node.get('node_id')}] "
        f"{node.get('title') or ''} "
        f"(p.{node.get('page_number') or '?'}) — {node.get('summary') or ''}"
    )
    lines = [line.strip()]
    for ch in node.get("children") or []:
        lines.extend(_serialize_node(ch, indent + 1))
    return lines


def forest_node_index(forest: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map node_id → metadata for turning LLM-picked nodes into search queries."""

    out: Dict[str, Dict[str, Any]] = {}

    def walk(node: Dict[str, Any]) -> None:
        nid = node.get("node_id")
        if nid:
            out[str(nid)] = {
                "document_id": node.get("document_id"),
                "heading": node.get("heading") or "",
                "title": node.get("title") or "",
                "summary": node.get("summary") or "",
                "chunk_index": node.get("chunk_index"),
            }
        for ch in node.get("children") or []:
            walk(ch)

    for doc in forest:
        for root in doc.get("roots") or []:
            walk(root)
    return out


def forest_to_llm_outline(forest: List[Dict[str, Any]], max_lines: int = 120) -> str:
    parts: List[str] = []
    n = 0
    for doc in forest:
        parts.append(f"## doc {doc.get('document_id')} — {doc.get('document_title')}")
        for root in doc.get("roots") or []:
            for ln in _serialize_node(root, 0):
                parts.append(ln)
                n += 1
                if n >= max_lines:
                    parts.append("… (truncated)")
                    return "\n".join(parts)
    return "\n".join(parts)


def parse_node_id_list(raw_json: str) -> List[str]:
    """Parse LLM JSON like {\"node_ids\": [\"1:0\", \"1:3\"]} or [\"1:0\"]."""
    import json

    if not raw_json:
        return []
    try:
        m = re.search(r"\{[^{}]*\}", raw_json, re.DOTALL)
        if m:
            obj = json.loads(m.group())
            if isinstance(obj, dict) and "node_ids" in obj:
                ids = obj["node_ids"]
                if isinstance(ids, list):
                    return [str(x).strip() for x in ids if str(x).strip()][:8]
        m2 = re.search(r"\[.*?\]", raw_json, re.DOTALL)
        if m2:
            arr = json.loads(m2.group())
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()][:8]
    except Exception:
        return []
    return []


__all__ = [
    "make_node_id",
    "parse_node_id",
    "merge_unique_chunks",
    "build_forest",
    "forest_node_index",
    "forest_to_llm_outline",
    "parse_node_id_list",
    "normalize_outline_chunk",
]
