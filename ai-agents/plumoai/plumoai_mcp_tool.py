from __future__ import annotations

import json
import os
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent
from llm_tools.mcp_agent_tool import MCPAgentTool

logger = logging.getLogger(__name__)


class PlumoAIMCPTool(BaseToolAgent):
    """
    BaseToolAgent-compatible wrapper around MCPAgentTool for the PlumoAI MCP server.

    Supports:
    - HTTP MCP: https://mcp.plumoai.com/mcp
    - stdio MCP: command/args/env config, with PLUMO-style access token injection
    """

    TOOL_DESCRIPTION = (
        "PlumoAI AI Agent allows this AI Digital Employee to securely interact with the "
        "PlumoAI platform Projects, Tables, Web Documents, coordinate AI Employees, workflows, "
        "and internal resources using natural language."
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
        self.llm_provider = llm_provider
        self.token = token
        self.user_id = user_id
        self.company_id = company_id
        self.agent_id = agent_id
        self.app_config = app_config or {}
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

    def _build_mcp_config(self) -> Dict[str, Any]:
        active = self._active_config()
        service_credential = self.app_config.get("service_credential") or {}
        connected_service_id = service_credential.get("connected_service_id")
        stdio_entrypoint = os.path.join(os.path.dirname(__file__), "plumoai-mcp", "start-stdio.js")
        stdio_cwd = os.path.dirname(stdio_entrypoint)

        logger.info(
            "PlumoAI MCP config build: active_config=%s service_credential_present=%s connected_service_id=%s app_config_keys=%s",
            json.dumps(active, default=str),
            bool(service_credential),
            connected_service_id,
            list(self.app_config.keys()),
        )
        active["env"]["PLUMO_API_BASE_URL"] = "http://plumoai-api:3010/api/v1"


        mcp_config = {
            "server_type": "stdio",
            "command": "node",
            "args": [stdio_entrypoint],
            # Run the Node process with CWD = plumoai-mcp so local node_modules and relative paths behave
            # the same as when you `cd` into that folder on the host.
            "cwd": stdio_cwd,
            "env": active.get("env", {}) or {},
            "url": None,
            "headers": {},
            "custom_description": self.app_config.get("custom_description", "") or self.TOOL_DESCRIPTION,
            "special_instructions": active.get("special_instructions", ""),
        }

        # If a connected credential exists, pass the connected_service_id through so MCPAgentTool
        # can use its normal refresh path.
        if connected_service_id:
            mcp_config["connected_service_id"] = connected_service_id

        return mcp_config

    async def initialize(self) -> None:
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

