from __future__ import annotations

from typing import Any, Dict, List


def _relevance_score_from_distance(distance: Any) -> float:
    try:
        d = float(distance)
    except (TypeError, ValueError):
        d = 1.0
    return round((1 - d) * 100, 1)


def format_search_results(search_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalize the API result chunks into the tool's public result shape.
    Keep all rich metadata that the knowledgebase API returns.
    """

    formatted: List[Dict[str, Any]] = []
    for chunk in search_results or []:
        distance = chunk.get("distance", 1)
        formatted.append(
            {
                # Core content
                "chunk_id": chunk.get("id"),
                "chunk_index": chunk.get("chunk_index"),
                "chunk_text": chunk.get("chunk_text"),
                "chunk_type": chunk.get("chunk_type"),
                # Relevance scoring
                "distance": distance,
                "relevance_score": _relevance_score_from_distance(distance),
                # Document context
                "document_id": chunk.get("document_id"),
                "title": chunk.get("title"),
                "file_type": chunk.get("file_type"),
                "source_path": chunk.get("source_path"),
                "project_fid": chunk.get("project_fid"),
                # Hierarchical context
                "section_path": chunk.get("section_path"),
                "heading": chunk.get("heading"),
                "heading_level": chunk.get("heading_level"),
                # Semantic metadata
                "keywords": chunk.get("keywords"),
                # Parent-child chunking
                "parent_id": chunk.get("parent_id"),
                "part_index": chunk.get("part_index"),
                "total_parts": chunk.get("total_parts"),
                # Size/position info
                "token_count": chunk.get("token_count"),
                "start_position": chunk.get("start_position"),
                "end_position": chunk.get("end_position"),
            }
        )
    return formatted

