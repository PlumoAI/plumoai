from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from llm_tools.mcp_agent_tool import MCPAgentTool
from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

logger = logging.getLogger(__name__)

_Lusha_MCP_URL = "https://mcp.lusha.com"


class LushaMCPTool(ConnectedServiceToolAgent):
    """
    ConnectedServiceToolAgent wrapper for the Lusha hosted MCP server.

    Connects via SSE/HTTP to https://mcp.Lusha.com/mcp using the OAuth 2.1
    Bearer token from the connected service credential. Token refresh is
    delegated to MCPAgentTool via connected_service_id.
    """

    TOOL_DESCRIPTION = (
        "Lusha AI Agent allows this AI Digital Employee to securely connect with Lusha and interact with contact data, company intelligence, enrichment, and credits using natural language.",
        "The agent can read and write data strictly based on the permissions granted to the connected Lusha credentials while remaining compliant with platform policies."
    )

    def __init__(
        self,
        *,
        llm_provider: Any,
        token: str,
        user_id: int,
        company_id: Optional[str],
        agent_id: str,
        app_config: Dict[str, Any],
    ) -> None:
        super().__init__(
            token=token,
            company_id=company_id,
            user_id=user_id,
            app_config=app_config,
        )
        self.llm_provider = llm_provider
        self.agent_id = agent_id
        self._delegate: Optional[MCPAgentTool] = None

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def _active_config(self) -> Dict[str, Any]:
        active = (
            self.app_config.get("app_config")
            or self.app_config.get("shared_config")
            or self.app_config.get("personal_config")
            or {}
        )
        if isinstance(active, str):
            try:
                active = json.loads(active)
            except json.JSONDecodeError:
                active = {}
        return active if isinstance(active, dict) else {}

    def _effective_connected_service_id(self) -> Optional[int]:
        """
        Resolve connected_service_id from the nested service_credential (standard injection)
        OR from top-level app_config keys (Lusha plugin path where service_credential is not injected).
        """
        cid = self.connected_service_id  # ConnectedServiceToolAgent reads from service_credential
        if cid:
            return cid
        raw = (
            self.app_config.get("connected_service_id")
            or self.app_config.get("personal_service_id")
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _build_mcp_config(self) -> Dict[str, Any]:
        active = self._active_config()
        cs_id = self._effective_connected_service_id()

        headers: Dict[str, str] = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        logger.info(
            "Lusha MCP config build: connected_service_id=%s token_present=%s",
            cs_id,
            bool(self.access_token),
        )

        mcp_config: Dict[str, Any] = {
            "server_type": "sse",
            "url": active.get("mcp_url") or _Lusha_MCP_URL,
            "headers": headers,
            "command": None,
            "args": [],
            "env": {},
            "custom_description": self.app_config.get("custom_description", "") or self.TOOL_DESCRIPTION,
            "special_instructions": active.get("special_instructions", ""),
        }

        if cs_id:
            mcp_config["connected_service_id"] = cs_id

        return mcp_config

    async def initialize(self) -> None:
        # If ConnectedServiceToolAgent found no token (Lusha plugin sends connected_service_id
        # at the top level of app_config rather than nested in service_credential), inject it
        # into service_credential so refresh_access_token() resolves the live OAuth token.
        if not self._access_token:
            cs_id = self._effective_connected_service_id()
            if cs_id:
                self.service_credential["connected_service_id"] = cs_id
                refreshed = await self.refresh_access_token()
                logger.info("Lusha pre-init token refresh: cs_id=%s success=%s", cs_id, refreshed)

        mcp_config = self._build_mcp_config()
        self._delegate = MCPAgentTool(
            mcp_config=mcp_config,
            llm_provider=self.llm_provider,
            token=self.token,
            company_id=self.company_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            connected_service_id=mcp_config.get("connected_service_id"),
        )
        await self._delegate.initialize()

    async def cleanup(self) -> None:
        if self._delegate and hasattr(self._delegate, "cleanup"):
            await self._delegate.cleanup()

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        conversation_history: Optional[Any] = None,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[Any] = None,
        **tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        if self._delegate is None:
            await self.initialize()
        assert self._delegate is not None

        # MCPAgentTool.run expects: query, tool_name (optional), tool_args (optional),
        # resource_uri (optional), provided_data (optional).
        run_kwargs: Dict[str, Any] = {
            "query": user_query,
            "provided_data": provided_data,
        }
        if tool_args:
            run_kwargs["tool_args"] = tool_args

        async for event in self._delegate.run(**run_kwargs):
            yield event

