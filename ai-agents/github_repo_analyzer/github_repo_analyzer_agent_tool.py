from __future__ import annotations

"""
GitHub Repository Analyzer Agent Tool (app_code: github_repo_analyzer)
Built-in tool that analyzes public GitHub repositories.

Input:
- GitHub repository URL

Output:
- Repository metadata
- Project summary
- Technologies used
- Contributor insights
- Learning recommendations for developers
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

logger = logging.getLogger(__name__)

GITHUB_ANALYSIS_SYSTEM_PROMPT = """You are analyzing a GitHub repository based on its metadata.

Provide a concise analysis covering:
1. Project purpose — what problem it solves
2. Likely target users — who would use or contribute to it
3. Contributor difficulty — how approachable the project is for new contributors

Keep the response clear and factual. Base your analysis only on the provided metadata."""


def event(event_type: str, content: Any) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.now(datetime.UTC).isoformat() + "Z",
        "content": content,
    }


class GitHubRepoAnalyzerAgent(BaseToolAgent):
    TOOL_DESCRIPTION = """
"Fetches and analyzes any public GitHub repository. "
"Expects tool_args={'url': '<github repo url>'} or a plain GitHub URL in user_query. "
"Returns repository metadata (name, owner, stars, forks, language, open issues) "
"and an LLM-generated summary of the project's purpose, target users, and contributor difficulty."
    """
    TOOL_NAME = "GitHub Repository Analyzer"

    APP_CODE = "github_rep_analyzer"

    # Only valid GitHub owner/repo characters; avoids capturing trailing punctuation.
    _REPO_PATTERN = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION
    
    def get_description(self) -> str:
        return self.get_tool_responsibility()

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
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config or {}

    def _parse_owner_repo(self, text: str) -> tuple[str, str] | tuple[None, None]:
        """Return (owner, repo) parsed from a GitHub URL, or (None, None) if not found."""
        match = self._REPO_PATTERN.search(text)
        if not match:
            return None, None
        owner = match.group(1)
        repo = match.group(2).removesuffix(".git")
        return owner, repo

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict, None]:
        tool_args = tool_args or {}

        # Accept URL from tool_args, a JSON blob in user_query, or plain user_query.
        raw = (tool_args.get("url") or "").strip()
        if not raw:
            s = (user_query or "").strip()
            if s.startswith("{") and "}" in s:
                try:
                    obj = json.loads(s[:2000])
                    raw = str(obj.get("url", "")).strip()
                except json.JSONDecodeError:
                    pass
            if not raw:
                raw = s[:500]

        owner, repo = self._parse_owner_repo(raw)
        if not owner:
            out = {"success": False, "error": "Please provide a valid GitHub repository URL.", "result": None}
            yield event("error", out)
            yield event("final", out)
            return

        yield event("thought", f"Fetching metadata for {owner}/{repo}...")

        try:
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.get(f"https://api.github.com/repos/{owner}/{repo}")

            if response.status_code == 403:
                out = {
                    "success": False,
                    "error": "GitHub API rate limit exceeded. Try again later.",
                    "result": None,
                }

                yield event("error", out)
                yield event("final", out)
                return            
            
            if response.status_code == 404:
                out = {
                    "success": False, 
                    "error": f"Repository not found: {owner}/{repo}", 
                    "result": None
                }
                yield event("error", out)
                yield event("final", out)
                return

            response.raise_for_status()
            data = response.json()

        except httpx.HTTPStatusError as exc:
            logger.exception("GitHub API returned an unexpected status")
            out = {"success": False, 
                "error": f"GitHub API error {exc.response.status_code}: {exc.response.text}", 
                "result": None
                }
            yield event("error", out)
            yield event("final", out)
            return

        repo_info = {
            "name": data.get("name"),
            "owner": data.get("owner", {}).get("login"),
            "description": data.get("description"),
            "language": data.get("language"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "url": data.get("html_url"),
        }

        summary = None
        if self.llm_provider and getattr(self.llm_provider, "get_response", None):
            yield event("thought", f"Generating analysis for {owner}/{repo}...")
            try:
                summary = await self.llm_provider.get_response(
                    transcript=json.dumps(repo_info),
                    system_prompt=GITHUB_ANALYSIS_SYSTEM_PROMPT,
                    max_tokens=300,
                    temperature=0.3,
                )
            except Exception as exc:
                logger.warning("LLM summarization failed for %s/%s: %s", owner, repo, exc)
                summary = None

        out = {
            "success": True,
            "response": summary,
            "result": {
                "repository": repo_info,
                "analysis": summary,
            },
        }
        yield event("result", out)
        yield event("final", out)

    async def initialize(self) -> None:
        logger.debug(" Search agent initialized")

    async def cleanup(self) -> None:
        logger.debug("Web Search agent cleaned up")