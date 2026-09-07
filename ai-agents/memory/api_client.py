from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from .constants import API_TIMEOUT, COMPANY_URL

logger = logging.getLogger(__name__)


class MemoryApiClient:
    def __init__(self, *, agent_id: str, token: str, company_id: str, user_id: Optional[int]):
        self.agent_id = agent_id
        self.token = token or ""
        self.company_id = company_id or ""
        self.user_id = user_id

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "companyids": f"[{self.company_id}]",
        }

    def _user_id_str(self) -> str:
        return str(self.user_id) if self.user_id is not None else ""

    async def store(
        self,
        *,
        content: str,
        mem_type: str,
        importance_score: float,
        scores: Dict[str, float],
        tags: List[str],
        raw_context: Optional[str],
        scope: str = "personal",
    ) -> Optional[Dict[str, Any]]:
        if not COMPANY_URL:
            logger.error("COMPANY_URL not configured — cannot store memory")
            return None

        payload: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": self._user_id_str(),
            "content": content,
            "type": mem_type,
            "importance_score": importance_score,
            "scores": scores,
            "scope": scope,
        }
        if tags:
            payload["tags"] = tags
        if raw_context:
            payload["raw_context"] = raw_context[:500]

        url = f"{COMPANY_URL}/aiagentchat/memory/store"
        logger.info("🧠 [Memory API] POST %s | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info("🧠 [Memory API] store → %s | response: %s", resp.status_code, resp.text[:300])
            if resp.status_code == 201:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data")
            logger.warning("Memory store failed: %s %s", resp.status_code, resp.text[:200])
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory store request error: %s", exc)
        return None

    async def store_bulk(self, *, memories: List[Dict[str, Any]]) -> List[Optional[Dict[str, Any]]]:
        if not memories:
            return []
        if not COMPANY_URL:
            logger.error("COMPANY_URL not configured — cannot bulk-store memories")
            return [None] * len(memories)

        url = f"{COMPANY_URL}/aiagentchat/memory/store/bulk"
        payload: Dict[str, Any] = {"memories": memories}
        logger.info(
            "🧠 [Memory API] POST %s | %d memories | payload: %s",
            url,
            len(memories),
            json.dumps(payload, default=str)[:600],
        )
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            stored_count = "?"
            if resp.status_code == 201:
                try:
                    stored_count = resp.json().get("data", {}).get("stored_count", "?")
                except Exception:
                    pass
            logger.info(
                "🧠 [Memory API] store/bulk → %s | stored_count=%s | response: %s",
                resp.status_code,
                stored_count,
                resp.text[:300],
            )
            if resp.status_code == 201:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data", {}).get("memories", [None] * len(memories))
            logger.warning(
                "Memory bulk-store failed (%s %s) — falling back to individual stores",
                resp.status_code,
                resp.text[:200],
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory bulk-store request error: %s — falling back to individual stores", exc)

        results: List[Optional[Dict[str, Any]]] = []
        for m in memories:
            results.append(
                await self.store(
                    content=m["content"],
                    mem_type=m["type"],
                    importance_score=m["importance_score"],
                    scores=m.get("scores") or {},
                    tags=m.get("tags") or [],
                    raw_context=m.get("raw_context"),
                    scope=m.get("scope", "personal"),
                )
            )
        return results

    async def recall(
        self,
        *,
        query: str,
        limit: int,
        mem_type: Optional[str] = None,
        types: Optional[List[str]] = None,
        min_importance: Optional[float] = None,
        scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not COMPANY_URL:
            return []

        payload: Dict[str, Any] = {"agent_id": self.agent_id, "user_id": self._user_id_str(), "limit": limit}
        if query and query.strip():
            payload["query"] = query.strip()
        if mem_type:
            payload["type"] = mem_type
        if types:
            payload["types"] = types
        if min_importance is not None:
            payload["min_importance"] = min_importance
        if scope:
            payload["scope"] = scope

        url = f"{COMPANY_URL}/aiagentchat/memory/recall"
        logger.info("🧠 [Memory API] POST %s | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info(
                "🧠 [Memory API] recall → %s | memories_returned: %s | response: %s",
                resp.status_code,
                len(resp.json().get("data", {}).get("memories", [])) if resp.status_code == 200 else 0,
                resp.text[:300],
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data", {}).get("memories", [])
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory recall request error: %s", exc)
        return []

    async def recall_shared(
        self,
        *,
        query: str,
        limit: int,
        mem_type: Optional[str] = None,
        types: Optional[List[str]] = None,
        min_importance: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        if not COMPANY_URL:
            return []

        payload: Dict[str, Any] = {"agent_id": self.agent_id, "scope": "shared", "limit": limit}
        if query and query.strip():
            payload["query"] = query.strip()
        if mem_type:
            payload["type"] = mem_type
        if types:
            payload["types"] = types
        if min_importance is not None:
            payload["min_importance"] = min_importance

        url = f"{COMPANY_URL}/aiagentchat/memory/recall"
        logger.info("🧠 [Memory API] POST %s (shared) | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info(
                "🧠 [Memory API] recall(shared) → %s | memories_returned: %s",
                resp.status_code,
                len(resp.json().get("data", {}).get("memories", [])) if resp.status_code == 200 else 0,
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    raw = body.get("data", {}).get("memories", [])
                    return [m for m in raw if (m.get("scope") or "personal") == "shared"]
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory recall(shared) request error: %s", exc)
        return []

    async def recall_bulk(self, *, queries: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        if not queries:
            return []
        if not COMPANY_URL:
            return [[] for _ in range(len(queries))]

        url = f"{COMPANY_URL}/aiagentchat/memory/recall/bulk"
        payload: Dict[str, Any] = {"queries": queries}
        logger.info(
            "🧠 [Memory API] POST %s | %d queries | payload: %s",
            url,
            len(queries),
            json.dumps(payload, default=str)[:600],
        )
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            total_count = "?"
            if resp.status_code == 200:
                try:
                    total_count = resp.json().get("data", {}).get("total_count", "?")
                except Exception:
                    pass
            logger.info(
                "🧠 [Memory API] recall/bulk → %s | total_count=%s | response: %s",
                resp.status_code,
                total_count,
                resp.text[:300],
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    raw_results = body.get("data", {}).get("results")
                    # API variants observed:
                    # - results: [ [mem,...], [mem,...] ]                  (preferred)
                    # - results: [ {"query": "...", "memories": [..]}, ... ] (older/alt)
                    if isinstance(raw_results, list) and raw_results and isinstance(raw_results[0], dict):
                        out: List[List[Dict[str, Any]]] = []
                        for item in raw_results:
                            mems = item.get("memories") if isinstance(item, dict) else None
                            out.append(mems if isinstance(mems, list) else [])
                        # Ensure length matches input (best effort)
                        if len(out) < len(queries):
                            out.extend([[] for _ in range(len(queries) - len(out))])
                        return out
                    if isinstance(raw_results, list):
                        # Expect list-of-lists; if shape mismatches, fall back to empty-per-query
                        if len(raw_results) == len(queries) and all(isinstance(x, list) for x in raw_results):
                            return raw_results  # type: ignore[return-value]
                        if all(isinstance(x, list) for x in raw_results):
                            return raw_results  # best-effort even if len differs
                    return [[] for _ in range(len(queries))]
            logger.warning("Memory bulk-recall failed (%s) — falling back to individual recalls", resp.status_code)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory bulk-recall request error: %s — falling back to individual recalls", exc)

        results: List[List[Dict[str, Any]]] = []
        for q in queries:
            q_text = (q.get("query") or "").strip()
            q_limit = int(q.get("limit") or 10)
            q_type = q.get("type") or None
            q_types = q.get("types") or None
            q_min_importance = q.get("min_importance")
            if (q.get("scope") or "").strip().lower() == "shared":
                results.append(await self.recall_shared(query=q_text, limit=q_limit, mem_type=q_type, types=q_types, min_importance=q_min_importance))
            else:
                results.append(await self.recall(query=q_text, limit=q_limit, mem_type=q_type, types=q_types, min_importance=q_min_importance))
        return results

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        mem_type: Optional[str] = None,
        types: Optional[List[str]] = None,
        min_importance: Optional[float] = None,
        scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not COMPANY_URL:
            return {"memories": [], "total": 0}

        url = f"{COMPANY_URL}/aiagentchat/memory/list"
        params: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": self._user_id_str(),
            "limit": limit,
            "offset": offset,
        }
        if mem_type:
            params["type"] = mem_type
        if types:
            params["types"] = ",".join(types)
        if min_importance is not None:
            params["min_importance"] = min_importance
        if scope:
            params["scope"] = scope
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data", {})
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory list request error: %s", exc)
        return {"memories": [], "total": 0}

    async def list_shared(self, *, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        if not COMPANY_URL:
            return {"memories": [], "total": 0}

        url = f"{COMPANY_URL}/aiagentchat/memory/list"
        params: Dict[str, Any] = {"agent_id": self.agent_id, "scope": "shared", "limit": limit, "offset": offset}
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data", {})
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory list(shared) request error: %s", exc)
        return {"memories": [], "total": 0}

    async def patch(self, *, memory_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not COMPANY_URL or not memory_id:
            return None
        url = f"{COMPANY_URL}/aiagentchat/memory/{memory_id}"
        logger.info("🧠 [Memory API] PATCH %s | updates: %s", url, json.dumps(updates, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.patch(url, headers=self._headers(), json=updates)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data")
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory patch request error: %s", exc)
        return None

    async def delete(self, *, memory_id: str) -> bool:
        if not COMPANY_URL or not memory_id:
            return False
        url = f"{COMPANY_URL}/aiagentchat/memory/{memory_id}"
        params = {"agent_id": self.agent_id, "user_id": self._user_id_str()}
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.delete(url, headers=self._headers(), params=params)
            return resp.status_code in (200, 204)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory delete request error: %s", exc)
        return False

    async def exists(
        self,
        *,
        query: str,
        threshold: float = 0.5,
        mem_type: Optional[str] = None,
        candidate_limit: int = 8,
    ) -> Dict[str, Any]:
        """Returns {"exists": bool, "memory_id": Optional[str], "similarity_score": Optional[float]}"""
        if not COMPANY_URL:
            return {"exists": False}
        url = f"{COMPANY_URL}/aiagentchat/memory/exists"
        payload: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": self._user_id_str(),
            "query": query,
            "threshold": threshold,
            "candidate_limit": candidate_limit,
        }
        if mem_type:
            payload["type"] = mem_type
        logger.info("🧠 [Memory API] POST %s | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info("🧠 [Memory API] exists → %s | response: %s", resp.status_code, resp.text[:200])
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return body.get("data") or {"exists": False}
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory exists request error: %s", exc)
        return {"exists": False}

    async def update(
        self,
        *,
        query: str,
        new_content: str,
        threshold: float = 0.4,
        mem_type: Optional[str] = None,
        importance_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Atomic semantic-match + patch. Returns {"success": bool, "memory_id"?: str, "error"?: str}"""
        if not COMPANY_URL:
            return {"success": False, "error": "COMPANY_URL not configured"}
        url = f"{COMPANY_URL}/aiagentchat/memory/update"
        payload: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": self._user_id_str(),
            "query": query,
            "new_content": new_content,
            "threshold": threshold,
        }
        if mem_type:
            payload["type"] = mem_type
        if importance_score is not None:
            payload["importance_score"] = importance_score
        logger.info("🧠 [Memory API] POST %s | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info("🧠 [Memory API] update → %s | response: %s", resp.status_code, resp.text[:300])
            if resp.status_code in (200, 201):
                body = resp.json()
                if body.get("type") == "success":
                    return {"success": True, **(body.get("data") or {})}
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:100]}"}
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory update request error: %s", exc)
        return {"success": False, "error": "request error"}

    async def reflect(
        self,
        *,
        max_memories: int = 100,
        similarity_threshold: float = 0.5,
        types: Optional[List[str]] = None,
        min_importance: float = 0.20,
    ) -> Dict[str, Any]:
        """Trigger server-side dedup + merge of similar memories."""
        if not COMPANY_URL:
            return {"success": False}
        url = f"{COMPANY_URL}/aiagentchat/memory/reflect"
        payload: Dict[str, Any] = {
            "agent_id": self.agent_id,
            "user_id": self._user_id_str(),
            "max_memories": max_memories,
            "similarity_threshold": similarity_threshold,
            "min_importance": min_importance,
        }
        if types:
            payload["types"] = types
        logger.info("🧠 [Memory API] POST %s | payload: %s", url, json.dumps(payload, default=str))
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info("🧠 [Memory API] reflect → %s | response: %s", resp.status_code, resp.text[:300])
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    return {"success": True, **(body.get("data") or {})}
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory reflect request error: %s", exc)
        return {"success": False}

    async def bulk_delete(self, *, memory_ids: List[str]) -> Dict[str, int]:
        if not COMPANY_URL or not memory_ids:
            return {"deleted_count": 0, "requested_count": len(memory_ids or [])}
        url = f"{COMPANY_URL}/aiagentchat/memory/bulk-delete"
        payload: Dict[str, Any] = {"agent_id": self.agent_id, "user_id": self._user_id_str(), "memory_ids": memory_ids}
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
            logger.info("🧠 [Memory API] bulk-delete → %s | response: %s", resp.status_code, resp.text[:300])
            if resp.status_code == 200:
                body = resp.json()
                if body.get("type") == "success":
                    data = body.get("data", {}) or {}
                    return {
                        "deleted_count": int(data.get("deleted_count") or 0),
                        "requested_count": int(data.get("requested_count") or len(memory_ids)),
                    }
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.error("Memory bulk-delete request error: %s", exc)
        return {"deleted_count": 0, "requested_count": len(memory_ids)}

