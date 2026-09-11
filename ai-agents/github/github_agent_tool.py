"""
GitHub Agent Tool for PlumoAI.

Manage repos, issues, PRs, branches, releases, commits, code search, users, and files.
Uses GitHub REST API: https://api.github.com

Design:
- Single intent pipeline: tool_args -> provided_data -> one LLM call -> action + params.
- API layer: repos, issues, pulls, branches, releases, commits, search, users, files.
- Handlers: list_repos, get_repo, create_repo, fork_repo, list_issues, create_issue, etc.
"""

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

GITHUB_API_BASE = "https://api.github.com"

_AUTH_URL = (os.getenv("AUTH_URL") or "https://api.plumoai.com").rstrip("/")
_COMPANY_URL = (os.getenv("COMPANY_URL") or _AUTH_URL).rstrip("/")


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content,
    }


def _redact_secrets_for_log(value: Any) -> Any:
    SENSITIVE_KEY_TOKENS = (
        "token", "secret", "password", "api_key", "access_token",
        "refresh_token", "authorization", "bearer", "private", "key",
        "client_secret", "credentials",
    )

    def _looks_sensitive_key(k: str) -> bool:
        return any(t in (k or "").lower() for t in SENSITIVE_KEY_TOKENS)

    def _mask_str(s: str) -> str:
        ss = (s or "").strip()
        if not ss:
            return ss
        if len(ss) >= 24:
            return ss[:6] + "…" + ss[-4:]
        return "***"

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _looks_sensitive_key(k):
                out[k] = _mask_str(str(v)) if v is not None else None
            else:
                out[k] = _redact_secrets_for_log(v)
        return out
    if isinstance(value, list):
        return [_redact_secrets_for_log(v) for v in value[:50]]
    if isinstance(value, str):
        if len(value) > 1200:
            return value[:600] + "…<truncated>…" + value[-120:]
        if len(value.strip()) >= 48:
            return _mask_str(value)
        return value
    return value


class GitHubAgentTool(ConnectedServiceToolAgent):
    """
    GitHub app agent. Capabilities:
    - Repos: list, get info, create, fork.
    - Issues: list, create, get, update, add comment, list comments.
    - Pull Requests: list, create, get, merge, list reviews, add review.
    - Commits: list, get.
    - Code Search: search code, search repos, search issues.
    - Branches: list, create, delete.
    - Releases: list, create.
    - Users: get authenticated user, get user info.
    - Files: get file contents, create/update file.
    """

    TOOL_NAME = "GitHub"
    TOOL_DESCRIPTION = """GitHub AI Agent: manage repositories, issues, pull requests, branches, releases, code search, and files.

USE WHEN: user mentions GitHub, repository, repo, issue, pull request, PR, branch, commit, release, code search, fork, merge, or source code management.

ACTIONS: list_repos, get_repo, create_repo, fork_repo, list_issues, create_issue, get_issue, update_issue, add_issue_comment, list_issue_comments, list_pulls, create_pull, get_pull, merge_pull, list_reviews, add_review, list_commits, get_commit, search_code, search_repos, search_issues, list_branches, create_branch, delete_branch, list_releases, create_release, get_authenticated_user, get_user, get_file_contents, create_or_update_file."""

    ACTION_DESCRIPTIONS = (
        "list_repos=list repositories for user or org; get_repo=get repository details; "
        "create_repo=create a new repository; fork_repo=fork a repository; "
        "list_issues=list issues in a repo; create_issue=create a new issue; "
        "get_issue=get issue details; update_issue=update an issue; "
        "add_issue_comment=add comment to issue; list_issue_comments=list comments on issue; "
        "list_pulls=list pull requests; create_pull=create a pull request; "
        "get_pull=get pull request details; merge_pull=merge a pull request; "
        "list_reviews=list reviews on a PR; add_review=add review to a PR; "
        "list_commits=list commits in a repo; get_commit=get commit details; "
        "search_code=search code across GitHub; search_repos=search repositories; "
        "search_issues=search issues and PRs; list_branches=list branches; "
        "create_branch=create a new branch; delete_branch=delete a branch; "
        "list_releases=list releases; create_release=create a new release; "
        "get_authenticated_user=get current user info; get_user=get user profile; "
        "get_file_contents=get file contents from repo; create_or_update_file=create or update a file in repo"
    )

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def __init__(
        self,
        llm_provider: Any,
        agent_id: Optional[str] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id or ""
        super().__init__(token=token, company_id=company_id, user_id=user_id, app_config=app_config)
        self._httpx_client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _refresh_access_token_if_needed(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def _llm_generate_text(self, prompt: str, max_tokens: int = 500) -> Optional[str]:
        if not self.llm_provider or not hasattr(self.llm_provider, "generate"):
            return None
        try:
            gen = self.llm_provider.generate(prompt, max_tokens=max_tokens)
            if gen is None:
                return None
            if hasattr(gen, "__aiter__"):
                out = ""
                async for chunk in gen:
                    if isinstance(chunk, dict) and "text" in chunk:
                        out += chunk.get("text", "")
                    elif isinstance(chunk, str):
                        out += chunk
                return out.strip() if out else None
            if isinstance(gen, str):
                return gen.strip() or None
        except Exception as e:
            logger.debug("GitHub LLM generate failed: %s", e)
        return None

    # ----- GitHub API layer -----
    async def _github_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Optional[Any]:
        url = f"{GITHUB_API_BASE}/{path.lstrip('/')}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0)
        headers = self._headers()

        r = await self._httpx_client.request(method, url, json=json_body, params=params, headers=headers)

        if r.status_code == 401 and retry_401 and await self._refresh_access_token_if_needed():
            return await self._github_request(method, path, json_body=json_body, params=params, retry_401=False)
        if r.status_code == 204:
            return {"ok": True}
        if r.status_code >= 400:
            logger.warning("GitHub API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            try:
                err = r.json()
                return {"error": err.get("message", r.text[:200]), "status": r.status_code}
            except Exception:
                return {"error": r.text[:200], "status": r.status_code}
        try:
            return r.json()
        except Exception:
            return None

    # ----- Repo operations -----
    async def _list_repos(self, owner: Optional[str] = None, limit: int = 30, repo_type: str = "all") -> List[Dict]:
        if owner:
            path = f"users/{owner}/repos"
        else:
            path = "user/repos"
        params: Dict[str, Any] = {"per_page": min(limit, 100), "sort": "updated", "type": repo_type}
        data = await self._github_request("GET", path, params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "full_name": r.get("full_name", ""),
                "name": r.get("name", ""),
                "private": r.get("private", False),
                "description": r.get("description", ""),
                "language": r.get("language", ""),
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "updated_at": r.get("updated_at", ""),
                "html_url": r.get("html_url", ""),
            }
            for r in data[:limit]
        ]

    async def _get_repo(self, owner: str, repo: str) -> Optional[Dict]:
        data = await self._github_request("GET", f"repos/{owner}/{repo}")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {
            "full_name": data.get("full_name", ""),
            "name": data.get("name", ""),
            "private": data.get("private", False),
            "description": data.get("description", ""),
            "language": data.get("language", ""),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "default_branch": data.get("default_branch", ""),
            "html_url": data.get("html_url", ""),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
            "topics": data.get("topics", []),
        }

    async def _create_repo(self, name: str, description: str = "", private: bool = False, auto_init: bool = True) -> Optional[Dict]:
        body = {"name": name, "description": description, "private": private, "auto_init": auto_init}
        data = await self._github_request("POST", "user/repos", json_body=body)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"full_name": data.get("full_name", ""), "html_url": data.get("html_url", ""), "private": data.get("private", False)}

    async def _fork_repo(self, owner: str, repo: str) -> Optional[Dict]:
        data = await self._github_request("POST", f"repos/{owner}/{repo}/forks")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"full_name": data.get("full_name", ""), "html_url": data.get("html_url", "")}

    # ----- Issue operations -----
    async def _list_issues(self, owner: str, repo: str, state: str = "open", limit: int = 30) -> List[Dict]:
        params = {"state": state, "per_page": min(limit, 100)}
        data = await self._github_request("GET", f"repos/{owner}/{repo}/issues", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "user": (i.get("user") or {}).get("login", ""),
                "labels": [l.get("name", "") for l in (i.get("labels") or [])],
                "created_at": i.get("created_at", ""),
                "html_url": i.get("html_url", ""),
            }
            for i in data[:limit]
            if not i.get("pull_request")  # exclude PRs from issues list
        ]

    async def _create_issue(self, owner: str, repo: str, title: str, body: str = "", labels: Optional[List[str]] = None, assignees: Optional[List[str]] = None) -> Optional[Dict]:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = await self._github_request("POST", f"repos/{owner}/{repo}/issues", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"number": data.get("number"), "title": data.get("title", ""), "html_url": data.get("html_url", "")}

    async def _get_issue(self, owner: str, repo: str, issue_number: int) -> Optional[Dict]:
        data = await self._github_request("GET", f"repos/{owner}/{repo}/issues/{issue_number}")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {
            "number": data.get("number"),
            "title": data.get("title", ""),
            "state": data.get("state", ""),
            "body": (data.get("body") or "")[:2000],
            "user": (data.get("user") or {}).get("login", ""),
            "labels": [l.get("name", "") for l in (data.get("labels") or [])],
            "assignees": [(a or {}).get("login", "") for a in (data.get("assignees") or [])],
            "comments": data.get("comments", 0),
            "created_at": data.get("created_at", ""),
            "html_url": data.get("html_url", ""),
        }

    async def _update_issue(self, owner: str, repo: str, issue_number: int, **updates) -> Optional[Dict]:
        payload = {k: v for k, v in updates.items() if v is not None}
        data = await self._github_request("PATCH", f"repos/{owner}/{repo}/issues/{issue_number}", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"number": data.get("number"), "title": data.get("title", ""), "state": data.get("state", ""), "html_url": data.get("html_url", "")}

    async def _add_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> Optional[Dict]:
        data = await self._github_request("POST", f"repos/{owner}/{repo}/issues/{issue_number}/comments", json_body={"body": body})
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"id": data.get("id"), "html_url": data.get("html_url", ""), "body": (data.get("body") or "")[:500]}

    async def _list_issue_comments(self, owner: str, repo: str, issue_number: int, limit: int = 30) -> List[Dict]:
        params = {"per_page": min(limit, 100)}
        data = await self._github_request("GET", f"repos/{owner}/{repo}/issues/{issue_number}/comments", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "id": c.get("id"),
                "user": (c.get("user") or {}).get("login", ""),
                "body": (c.get("body") or "")[:500],
                "created_at": c.get("created_at", ""),
            }
            for c in data[:limit]
        ]

    # ----- Pull Request operations -----
    async def _list_pulls(self, owner: str, repo: str, state: str = "open", limit: int = 30) -> List[Dict]:
        params = {"state": state, "per_page": min(limit, 100)}
        data = await self._github_request("GET", f"repos/{owner}/{repo}/pulls", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "number": p.get("number"),
                "title": p.get("title", ""),
                "state": p.get("state", ""),
                "user": (p.get("user") or {}).get("login", ""),
                "head": (p.get("head") or {}).get("ref", ""),
                "base": (p.get("base") or {}).get("ref", ""),
                "created_at": p.get("created_at", ""),
                "html_url": p.get("html_url", ""),
            }
            for p in data[:limit]
        ]

    async def _create_pull(self, owner: str, repo: str, title: str, head: str, base: str, body: str = "", draft: bool = False) -> Optional[Dict]:
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
        data = await self._github_request("POST", f"repos/{owner}/{repo}/pulls", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"number": data.get("number"), "title": data.get("title", ""), "html_url": data.get("html_url", "")}

    async def _get_pull(self, owner: str, repo: str, pull_number: int) -> Optional[Dict]:
        data = await self._github_request("GET", f"repos/{owner}/{repo}/pulls/{pull_number}")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {
            "number": data.get("number"),
            "title": data.get("title", ""),
            "state": data.get("state", ""),
            "body": (data.get("body") or "")[:2000],
            "user": (data.get("user") or {}).get("login", ""),
            "head": (data.get("head") or {}).get("ref", ""),
            "base": (data.get("base") or {}).get("ref", ""),
            "mergeable": data.get("mergeable"),
            "merged": data.get("merged", False),
            "additions": data.get("additions", 0),
            "deletions": data.get("deletions", 0),
            "changed_files": data.get("changed_files", 0),
            "html_url": data.get("html_url", ""),
        }

    async def _merge_pull(self, owner: str, repo: str, pull_number: int, merge_method: str = "merge", commit_title: Optional[str] = None) -> Optional[Dict]:
        payload: Dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        data = await self._github_request("PUT", f"repos/{owner}/{repo}/pulls/{pull_number}/merge", json_body=payload)
        if not isinstance(data, dict):
            return None
        if data.get("merged"):
            return {"merged": True, "sha": data.get("sha", ""), "message": data.get("message", "")}
        return data

    async def _list_reviews(self, owner: str, repo: str, pull_number: int) -> List[Dict]:
        data = await self._github_request("GET", f"repos/{owner}/{repo}/pulls/{pull_number}/reviews")
        if not isinstance(data, list):
            return []
        return [
            {
                "id": r.get("id"),
                "user": (r.get("user") or {}).get("login", ""),
                "state": r.get("state", ""),
                "body": (r.get("body") or "")[:500],
                "submitted_at": r.get("submitted_at", ""),
            }
            for r in data
        ]

    async def _add_review(self, owner: str, repo: str, pull_number: int, body: str, review_event: str = "COMMENT") -> Optional[Dict]:
        payload = {"body": body, "event": review_event}
        data = await self._github_request("POST", f"repos/{owner}/{repo}/pulls/{pull_number}/reviews", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"id": data.get("id"), "state": data.get("state", ""), "html_url": data.get("html_url", "")}

    # ----- Commit operations -----
    async def _list_commits(self, owner: str, repo: str, sha: Optional[str] = None, limit: int = 30) -> List[Dict]:
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if sha:
            params["sha"] = sha
        data = await self._github_request("GET", f"repos/{owner}/{repo}/commits", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "sha": c.get("sha", "")[:12],
                "message": ((c.get("commit") or {}).get("message") or "")[:200],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name", ""),
                "date": ((c.get("commit") or {}).get("author") or {}).get("date", ""),
                "html_url": c.get("html_url", ""),
            }
            for c in data[:limit]
        ]

    async def _get_commit(self, owner: str, repo: str, ref: str) -> Optional[Dict]:
        data = await self._github_request("GET", f"repos/{owner}/{repo}/commits/{ref}")
        if not isinstance(data, dict) or data.get("error"):
            return data
        commit = data.get("commit") or {}
        return {
            "sha": data.get("sha", ""),
            "message": (commit.get("message") or "")[:500],
            "author": (commit.get("author") or {}).get("name", ""),
            "date": (commit.get("author") or {}).get("date", ""),
            "stats": data.get("stats", {}),
            "files": [
                {"filename": f.get("filename", ""), "status": f.get("status", ""), "changes": f.get("changes", 0)}
                for f in (data.get("files") or [])[:50]
            ],
            "html_url": data.get("html_url", ""),
        }

    # ----- Search operations -----
    async def _search_code(self, query: str, limit: int = 20) -> List[Dict]:
        params = {"q": query, "per_page": min(limit, 100)}
        data = await self._github_request("GET", "search/code", params=params)
        if not isinstance(data, dict):
            return []
        return [
            {
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "repository": (item.get("repository") or {}).get("full_name", ""),
                "html_url": item.get("html_url", ""),
            }
            for item in (data.get("items") or [])[:limit]
        ]

    async def _search_repos(self, query: str, limit: int = 20) -> List[Dict]:
        params = {"q": query, "per_page": min(limit, 100), "sort": "stars"}
        data = await self._github_request("GET", "search/repositories", params=params)
        if not isinstance(data, dict):
            return []
        return [
            {
                "full_name": r.get("full_name", ""),
                "description": (r.get("description") or "")[:200],
                "stars": r.get("stargazers_count", 0),
                "language": r.get("language", ""),
                "html_url": r.get("html_url", ""),
            }
            for r in (data.get("items") or [])[:limit]
        ]

    async def _search_issues(self, query: str, limit: int = 20) -> List[Dict]:
        params = {"q": query, "per_page": min(limit, 100)}
        data = await self._github_request("GET", "search/issues", params=params)
        if not isinstance(data, dict):
            return []
        return [
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "repository_url": item.get("repository_url", ""),
                "html_url": item.get("html_url", ""),
            }
            for item in (data.get("items") or [])[:limit]
        ]

    # ----- Branch operations -----
    async def _list_branches(self, owner: str, repo: str, limit: int = 30) -> List[Dict]:
        params = {"per_page": min(limit, 100)}
        data = await self._github_request("GET", f"repos/{owner}/{repo}/branches", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "name": b.get("name", ""),
                "sha": (b.get("commit") or {}).get("sha", "")[:12],
                "protected": b.get("protected", False),
            }
            for b in data[:limit]
        ]

    async def _create_branch(self, owner: str, repo: str, branch_name: str, from_branch: Optional[str] = None) -> Optional[Dict]:
        # Get SHA of source branch
        source = from_branch or "main"
        ref_data = await self._github_request("GET", f"repos/{owner}/{repo}/git/ref/heads/{source}")
        if not isinstance(ref_data, dict) or ref_data.get("error"):
            # Try 'master' as fallback
            if not from_branch:
                ref_data = await self._github_request("GET", f"repos/{owner}/{repo}/git/ref/heads/master")
            if not isinstance(ref_data, dict) or ref_data.get("error"):
                return ref_data
        sha = (ref_data.get("object") or {}).get("sha", "")
        if not sha:
            return {"error": "Could not resolve source branch SHA."}
        data = await self._github_request("POST", f"repos/{owner}/{repo}/git/refs", json_body={"ref": f"refs/heads/{branch_name}", "sha": sha})
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"branch": branch_name, "sha": sha[:12], "created": True}

    async def _delete_branch(self, owner: str, repo: str, branch_name: str) -> Optional[Dict]:
        data = await self._github_request("DELETE", f"repos/{owner}/{repo}/git/refs/heads/{branch_name}")
        if isinstance(data, dict) and data.get("ok"):
            return {"branch": branch_name, "deleted": True}
        return data

    # ----- Release operations -----
    async def _list_releases(self, owner: str, repo: str, limit: int = 20) -> List[Dict]:
        params = {"per_page": min(limit, 100)}
        data = await self._github_request("GET", f"repos/{owner}/{repo}/releases", params=params)
        if not isinstance(data, list):
            return []
        return [
            {
                "id": r.get("id"),
                "tag_name": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "draft": r.get("draft", False),
                "prerelease": r.get("prerelease", False),
                "published_at": r.get("published_at", ""),
                "html_url": r.get("html_url", ""),
            }
            for r in data[:limit]
        ]

    async def _create_release(self, owner: str, repo: str, tag_name: str, name: str = "", body: str = "", draft: bool = False, prerelease: bool = False) -> Optional[Dict]:
        payload = {"tag_name": tag_name, "name": name or tag_name, "body": body, "draft": draft, "prerelease": prerelease}
        data = await self._github_request("POST", f"repos/{owner}/{repo}/releases", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {"id": data.get("id"), "tag_name": data.get("tag_name", ""), "html_url": data.get("html_url", "")}

    # ----- User operations -----
    async def _get_authenticated_user(self) -> Optional[Dict]:
        data = await self._github_request("GET", "user")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {
            "login": data.get("login", ""),
            "name": data.get("name", ""),
            "email": data.get("email", ""),
            "bio": data.get("bio", ""),
            "public_repos": data.get("public_repos", 0),
            "followers": data.get("followers", 0),
            "following": data.get("following", 0),
            "html_url": data.get("html_url", ""),
        }

    async def _get_user(self, username: str) -> Optional[Dict]:
        data = await self._github_request("GET", f"users/{username}")
        if not isinstance(data, dict) or data.get("error"):
            return data
        return {
            "login": data.get("login", ""),
            "name": data.get("name", ""),
            "bio": data.get("bio", ""),
            "public_repos": data.get("public_repos", 0),
            "followers": data.get("followers", 0),
            "html_url": data.get("html_url", ""),
        }

    # ----- File operations -----
    async def _get_file_contents(self, owner: str, repo: str, path: str, ref: Optional[str] = None) -> Optional[Dict]:
        params = {}
        if ref:
            params["ref"] = ref
        data = await self._github_request("GET", f"repos/{owner}/{repo}/contents/{path}", params=params)
        if not isinstance(data, dict) or data.get("error"):
            return data
        if data.get("type") == "file":
            content = data.get("content", "")
            try:
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception:
                decoded = "(binary content)"
            return {
                "name": data.get("name", ""),
                "path": data.get("path", ""),
                "size": data.get("size", 0),
                "sha": data.get("sha", ""),
                "content": decoded[:10000],
                "html_url": data.get("html_url", ""),
            }
        elif data.get("type") == "dir" or isinstance(data, list):
            items = data if isinstance(data, list) else [data]
            return {
                "type": "directory",
                "entries": [
                    {"name": e.get("name", ""), "path": e.get("path", ""), "type": e.get("type", "")}
                    for e in items[:100]
                ],
            }
        return data

    async def _create_or_update_file(self, owner: str, repo: str, path: str, content: str, message: str, branch: Optional[str] = None, sha: Optional[str] = None) -> Optional[Dict]:
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        payload: Dict[str, Any] = {"message": message, "content": encoded}
        if branch:
            payload["branch"] = branch
        if sha:
            payload["sha"] = sha
        else:
            # Try to get existing file SHA for update
            existing = await self._github_request("GET", f"repos/{owner}/{repo}/contents/{path}", params={"ref": branch} if branch else None)
            if isinstance(existing, dict) and existing.get("sha") and not existing.get("error"):
                payload["sha"] = existing["sha"]
        data = await self._github_request("PUT", f"repos/{owner}/{repo}/contents/{path}", json_body=payload)
        if not isinstance(data, dict) or data.get("error"):
            return data
        file_info = data.get("content") or {}
        return {
            "path": file_info.get("path", path),
            "sha": file_info.get("sha", ""),
            "html_url": file_info.get("html_url", ""),
            "commit_sha": ((data.get("commit") or {}).get("sha") or "")[:12],
        }

    # ----- Helper: parse owner/repo -----
    @staticmethod
    def _parse_repo(params: Dict) -> Tuple[str, str]:
        owner = params.get("owner", "")
        repo = params.get("repo", "")
        if not owner and not repo:
            full = params.get("full_name", "") or params.get("repository", "")
            if "/" in full:
                parts = full.split("/", 1)
                owner, repo = parts[0], parts[1]
        return owner.strip(), repo.strip()

    # ----- Intent pipeline -----
    async def _decide_action(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if tool_args and isinstance(tool_args, dict):
            if tool_args.get("action"):
                action = str(tool_args["action"]).strip().lower()
                params = dict(tool_args)
                params.pop("action", None)
                params.pop("step_action", None)
                return {"action": action, "params": params}

        result = await self._decide_action_with_llm(user_query, provided_data, tool_args=tool_args)
        if result:
            return result

        return {"action": "list_repos", "params": {"limit": 20}}

    async def _decide_action_with_llm(
        self,
        user_query: str,
        provided_data: Optional[Any],
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        step_action = None
        if tool_args and isinstance(tool_args, dict) and tool_args.get("step_action"):
            step_action = str(tool_args.get("step_action"))[:300]

        context_parts = []
        if provided_data and isinstance(provided_data, list):
            for item in provided_data[:3]:
                if isinstance(item, dict):
                    context_parts.append(json.dumps(
                        {k: v for k, v in item.items() if k in ("owner", "repo", "full_name", "number", "title", "branch", "path")}
                    ))
        if step_action:
            context_parts.append("Step/context: " + step_action)
        context = " | ".join(context_parts) if context_parts else ""

        prompt = f"""You are a GitHub assistant. Output exactly one JSON object. No markdown, no explanation.

ACTION DESCRIPTIONS:
{self.ACTION_DESCRIPTIONS}

JSON keys (use exactly):
- "action": one of the actions listed above
- "owner": repository owner (user or org)
- "repo": repository name
- "full_name": owner/repo format
- "title": title for issue/PR/release
- "body": body text for issue/PR/comment/release
- "state": open/closed/all
- "labels": array of label names
- "assignees": array of usernames
- "issue_number": issue number
- "pull_number": PR number
- "head": head branch for PR
- "base": base branch for PR
- "merge_method": merge/squash/rebase
- "commit_title": merge commit title
- "review_event": APPROVE/REQUEST_CHANGES/COMMENT
- "sha": commit SHA or ref
- "branch_name": branch name
- "from_branch": source branch for new branch
- "query": search query
- "path": file path in repo
- "content": file content
- "message": commit message
- "tag_name": release tag
- "name": repo or release name
- "private": boolean
- "draft": boolean
- "prerelease": boolean
- "username": GitHub username
- "ref": git ref (branch/tag/sha)
- "limit": number of results

Context:
{context}

User request:
{(user_query or "").strip()[:800]}

JSON:"""
        out = await self._llm_generate_text(prompt, max_tokens=400)
        if not out:
            return None
        out = out.strip()
        for prefix in ("```json", "```"):
            if out.startswith(prefix):
                out = out[len(prefix):].strip()
            if out.endswith("```"):
                out = out[:-3].strip()
        try:
            data = json.loads(out)
            action = (data.get("action") or "list_repos").lower()
            valid_actions = (
                "list_repos", "get_repo", "create_repo", "fork_repo",
                "list_issues", "create_issue", "get_issue", "update_issue",
                "add_issue_comment", "list_issue_comments",
                "list_pulls", "create_pull", "get_pull", "merge_pull",
                "list_reviews", "add_review",
                "list_commits", "get_commit",
                "search_code", "search_repos", "search_issues",
                "list_branches", "create_branch", "delete_branch",
                "list_releases", "create_release",
                "get_authenticated_user", "get_user",
                "get_file_contents", "create_or_update_file",
            )
            if action not in valid_actions:
                action = "list_repos"
            params = {k: v for k, v in data.items() if k != "action" and v is not None}
            return {"action": action, "params": params}
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    # ----- Handler dispatch -----
    async def _execute_action(self, action: str, params: Dict) -> Dict:
        try:
            owner, repo = self._parse_repo(params)

            # ----- Repos -----
            if action == "list_repos":
                repos = await self._list_repos(owner=params.get("owner") or params.get("username"), limit=params.get("limit", 30))
                return {"success": True, "action": action, "repos": repos, "count": len(repos)}

            elif action == "get_repo":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                info = await self._get_repo(owner, repo)
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "repo": info}

            elif action == "create_repo":
                name = params.get("name", "")
                if not name:
                    return {"success": False, "response": "Repository name is required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["name"]}
                result = await self._create_repo(name, description=params.get("description", ""), private=params.get("private", False))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "repo": result, "response": f"Repository {result.get('full_name', name)} created."}

            elif action == "fork_repo":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                result = await self._fork_repo(owner, repo)
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "fork": result, "response": f"Forked to {result.get('full_name', '')}."}

            # ----- Issues -----
            elif action == "list_issues":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                issues = await self._list_issues(owner, repo, state=params.get("state", "open"), limit=params.get("limit", 30))
                return {"success": True, "action": action, "issues": issues, "count": len(issues)}

            elif action == "create_issue":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                title = params.get("title", "")
                if not title:
                    return {"success": False, "response": "Issue title is required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["title"]}
                result = await self._create_issue(owner, repo, title, body=params.get("body", ""), labels=params.get("labels"), assignees=params.get("assignees"))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "issue": result, "response": f"Issue #{result.get('number')} created."}

            elif action == "get_issue":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                issue_number = params.get("issue_number")
                if not issue_number:
                    return {"success": False, "response": "Issue number is required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["issue_number"]}
                info = await self._get_issue(owner, repo, int(issue_number))
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "issue": info}

            elif action == "update_issue":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                issue_number = params.get("issue_number")
                if not issue_number:
                    return {"success": False, "response": "Issue number is required."}
                updates = {}
                for field in ("title", "body", "state", "labels", "assignees"):
                    if params.get(field) is not None:
                        updates[field] = params[field]
                result = await self._update_issue(owner, repo, int(issue_number), **updates)
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "issue": result, "response": f"Issue #{issue_number} updated."}

            elif action == "add_issue_comment":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                issue_number = params.get("issue_number")
                body = params.get("body", "")
                if not issue_number or not body:
                    return {"success": False, "response": "Issue number and body are required.", "execution_issue": True, "need_discovery": True, "missing_fields": [f for f in ["issue_number", "body"] if not params.get(f)]}
                result = await self._add_issue_comment(owner, repo, int(issue_number), body)
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "comment": result, "response": "Comment added."}

            elif action == "list_issue_comments":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                issue_number = params.get("issue_number")
                if not issue_number:
                    return {"success": False, "response": "Issue number is required."}
                comments = await self._list_issue_comments(owner, repo, int(issue_number), limit=params.get("limit", 30))
                return {"success": True, "action": action, "comments": comments, "count": len(comments)}

            # ----- Pull Requests -----
            elif action == "list_pulls":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["owner", "repo"]}
                pulls = await self._list_pulls(owner, repo, state=params.get("state", "open"), limit=params.get("limit", 30))
                return {"success": True, "action": action, "pulls": pulls, "count": len(pulls)}

            elif action == "create_pull":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                title = params.get("title", "")
                head = params.get("head", "")
                base = params.get("base", "")
                if not title or not head or not base:
                    return {"success": False, "response": "Title, head, and base are required.", "execution_issue": True, "need_discovery": True,
                            "missing_fields": [f for f in ["title", "head", "base"] if not params.get(f)]}
                result = await self._create_pull(owner, repo, title, head, base, body=params.get("body", ""), draft=params.get("draft", False))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "pull": result, "response": f"PR #{result.get('number')} created."}

            elif action == "get_pull":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                pull_number = params.get("pull_number")
                if not pull_number:
                    return {"success": False, "response": "Pull request number is required."}
                info = await self._get_pull(owner, repo, int(pull_number))
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "pull": info}

            elif action == "merge_pull":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                pull_number = params.get("pull_number")
                if not pull_number:
                    return {"success": False, "response": "Pull request number is required."}
                result = await self._merge_pull(owner, repo, int(pull_number), merge_method=params.get("merge_method", "merge"), commit_title=params.get("commit_title"))
                if isinstance(result, dict) and result.get("merged"):
                    return {"success": True, "action": action, "result": result, "response": f"PR #{pull_number} merged."}
                error = (result or {}).get("error") or (result or {}).get("message", "Merge failed.")
                return {"success": False, "response": f"Failed: {error}"}

            elif action == "list_reviews":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                pull_number = params.get("pull_number")
                if not pull_number:
                    return {"success": False, "response": "Pull request number is required."}
                reviews = await self._list_reviews(owner, repo, int(pull_number))
                return {"success": True, "action": action, "reviews": reviews, "count": len(reviews)}

            elif action == "add_review":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                pull_number = params.get("pull_number")
                body = params.get("body", "")
                if not pull_number or not body:
                    return {"success": False, "response": "Pull number and body are required."}
                result = await self._add_review(owner, repo, int(pull_number), body, review_event=params.get("review_event", "COMMENT"))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "review": result, "response": "Review added."}

            # ----- Commits -----
            elif action == "list_commits":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                commits = await self._list_commits(owner, repo, sha=params.get("sha") or params.get("branch_name"), limit=params.get("limit", 30))
                return {"success": True, "action": action, "commits": commits, "count": len(commits)}

            elif action == "get_commit":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                ref = params.get("sha") or params.get("ref", "")
                if not ref:
                    return {"success": False, "response": "Commit SHA or ref is required."}
                info = await self._get_commit(owner, repo, ref)
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "commit": info}

            # ----- Search -----
            elif action == "search_code":
                query = params.get("query", "")
                if not query:
                    return {"success": False, "response": "Search query is required."}
                results = await self._search_code(query, limit=params.get("limit", 20))
                return {"success": True, "action": action, "results": results, "count": len(results)}

            elif action == "search_repos":
                query = params.get("query", "")
                if not query:
                    return {"success": False, "response": "Search query is required."}
                results = await self._search_repos(query, limit=params.get("limit", 20))
                return {"success": True, "action": action, "results": results, "count": len(results)}

            elif action == "search_issues":
                query = params.get("query", "")
                if not query:
                    return {"success": False, "response": "Search query is required."}
                results = await self._search_issues(query, limit=params.get("limit", 20))
                return {"success": True, "action": action, "results": results, "count": len(results)}

            # ----- Branches -----
            elif action == "list_branches":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                branches = await self._list_branches(owner, repo, limit=params.get("limit", 30))
                return {"success": True, "action": action, "branches": branches, "count": len(branches)}

            elif action == "create_branch":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                branch_name = params.get("branch_name", "")
                if not branch_name:
                    return {"success": False, "response": "Branch name is required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["branch_name"]}
                result = await self._create_branch(owner, repo, branch_name, from_branch=params.get("from_branch"))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "branch": result, "response": f"Branch '{branch_name}' created."}

            elif action == "delete_branch":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                branch_name = params.get("branch_name", "")
                if not branch_name:
                    return {"success": False, "response": "Branch name is required."}
                result = await self._delete_branch(owner, repo, branch_name)
                if isinstance(result, dict) and result.get("deleted"):
                    return {"success": True, "action": action, "response": f"Branch '{branch_name}' deleted."}
                error = (result or {}).get("error", "Delete failed.")
                return {"success": False, "response": f"Failed: {error}"}

            # ----- Releases -----
            elif action == "list_releases":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                releases = await self._list_releases(owner, repo, limit=params.get("limit", 20))
                return {"success": True, "action": action, "releases": releases, "count": len(releases)}

            elif action == "create_release":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                tag_name = params.get("tag_name", "")
                if not tag_name:
                    return {"success": False, "response": "Tag name is required.", "execution_issue": True, "need_discovery": True, "missing_fields": ["tag_name"]}
                result = await self._create_release(owner, repo, tag_name, name=params.get("name", ""), body=params.get("body", ""),
                                                     draft=params.get("draft", False), prerelease=params.get("prerelease", False))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "release": result, "response": f"Release {tag_name} created."}

            # ----- Users -----
            elif action == "get_authenticated_user":
                info = await self._get_authenticated_user()
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "user": info}

            elif action == "get_user":
                username = params.get("username", "")
                if not username:
                    return {"success": False, "response": "Username is required."}
                info = await self._get_user(username)
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "user": info}

            # ----- Files -----
            elif action == "get_file_contents":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                path = params.get("path", "")
                if not path:
                    return {"success": False, "response": "File path is required."}
                info = await self._get_file_contents(owner, repo, path, ref=params.get("ref"))
                if isinstance(info, dict) and info.get("error"):
                    return {"success": False, "response": f"Failed: {info.get('error')}"}
                return {"success": True, "action": action, "file": info}

            elif action == "create_or_update_file":
                if not owner or not repo:
                    return {"success": False, "response": "Owner and repo are required."}
                path = params.get("path", "")
                content = params.get("content", "")
                message = params.get("message", "")
                if not path or not message:
                    return {"success": False, "response": "Path and commit message are required.", "execution_issue": True, "need_discovery": True,
                            "missing_fields": [f for f in ["path", "message"] if not params.get(f)]}
                result = await self._create_or_update_file(owner, repo, path, content, message, branch=params.get("branch_name") or params.get("ref"), sha=params.get("sha"))
                if isinstance(result, dict) and result.get("error"):
                    return {"success": False, "response": f"Failed: {result.get('error')}"}
                return {"success": True, "action": action, "file": result, "response": f"File '{path}' saved."}

            else:
                return {"success": False, "response": f"Unknown action: {action}"}

        except Exception as e:
            logger.exception("GitHub action %s failed: %s", action, e)
            return {"success": False, "response": f"Error executing {action}: {str(e)}"}

    # ----- Main entry point (called by the runner) -----
    async def execute(
        self,
        user_query: str,
        *,
        provided_data: Optional[Any] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict, None]:
        try:
            yield event(AgentEvent.THOUGHT, f"Processing GitHub request: {(user_query or '')[:200]}")

            decision = await self._decide_action(user_query, provided_data, tool_args=tool_args)
            action = decision.get("action", "list_repos")
            params = decision.get("params", {})

            yield event(AgentEvent.PLAN, f"Action: {action}, params: {json.dumps(_redact_secrets_for_log(params), default=str)[:500]}")

            result = await self._execute_action(action, params)

            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, result)

        except Exception as e:
            logger.exception("GitHub agent execute failed: %s", e)
            error_result = {"success": False, "response": f"GitHub agent error: {str(e)}"}
            yield event(AgentEvent.ERROR, error_result)
            yield event(AgentEvent.FINAL, error_result)
