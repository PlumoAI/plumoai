from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx


def _company_url() -> Optional[str]:
    return os.getenv("COMPANY_URL")


class KnowledgebaseApiClient:
    def __init__(self, *, token: str, company_id: str):
        self._token = token
        self._company_id = company_id

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "companyIds": f"[{self._company_id}]",
        }

    async def search(
        self,
        *,
        query: str,
        document_ids: list,
        limit: int,
        project_fid: Optional[int],
        timeout_s: float = 30.0,
    ) -> Dict[str, Any]:
        base = _company_url()
        if not base:
            return {"ok": False, "error": "COMPANY_URL not configured"}

        url = f"{base}/aiagentchat/knowledgebase/search"
        payload: Dict[str, Any] = {
            "query": query,
            "document_ids": document_ids,
            "limit": limit,
            "project_fid": project_fid,
        }

        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json=payload,
            )
            return {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "json": (resp.json() if resp.content else None),
                "text": resp.text,
            }

    async def get_document_details(self, *, document_id: int, timeout_s: float = 10.0) -> Dict[str, Any]:
        base = _company_url()
        if not base:
            return {"ok": False, "error": "COMPANY_URL not configured"}

        url = f"{base}/aiagentchat/knowledgebase/{document_id}"
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url, headers=self._auth_headers())
            return {
                "ok": resp.status_code == 200,
                "status_code": resp.status_code,
                "json": (resp.json() if resp.content else None),
                "text": resp.text,
            }

