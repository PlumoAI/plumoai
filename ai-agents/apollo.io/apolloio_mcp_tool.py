from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from llm_tools.mcp_agent_tool import MCPAgentTool
from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent

logger = logging.getLogger(__name__)


class ApolloIOMCPTool(ConnectedServiceToolAgent):
    """
    BaseToolAgent-compatible wrapper around MCPAgentTool for the PlumoAI MCP server.

    Supports:
    - HTTP MCP: https://mcp.plumoai.com/mcp
    - stdio MCP: command/args/env config, with PLUMO-style access token injection
    """

    TOOL_DESCRIPTION = (
        "Apollo AI Agent allows this AI Digital Employee to securely connect with Apollo and interact with contacts, accounts, sequences, enrichment, and analytics using natural language. "
        "The agent can read and write data strictly based on the permissions granted to the connected Apollo credentials while remaining compliant with platform policies."
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
        OR from top-level app_config keys (Calendly plugin path where service_credential is not injected).
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

    @staticmethod
    def _ensure_uv_installed() -> Optional[str]:
        """
        Return the path to the `uv` binary, installing it via pip at runtime if needed.
        Returns None only if both detection and install fail.
        """
        uv = shutil.which("uv")
        if uv:
            return uv
        logger.info("Apollo MCP: 'uv' not on PATH — installing via pip...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "uv"],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Apollo MCP: 'uv' installed successfully.")
        except subprocess.CalledProcessError as exc:
            logger.warning("Apollo MCP: pip install uv failed: %s", exc.stderr or exc)
            return None
        # After pip install, uv may be on PATH now or reachable via python -m uv
        return shutil.which("uv") or None

    @staticmethod
    def _resolve_stdio_command(server_path: str) -> tuple[str, List[str]]:
        """
        Always use `uv run mcp run <server.py>`.
        Installs `uv` via pip at runtime if it is not already on PATH.
        Falls back to `python -m uv run mcp run` if the binary still can't be found
        after install (e.g. pip bin dir not on PATH yet in this process).
        """
        uv = ApolloIOMCPTool._ensure_uv_installed()
        if uv:
            return uv, ["run", "mcp", "run", server_path]
        # pip install succeeded but binary not on PATH yet — invoke via python -m uv
        return sys.executable, ["-m", "uv", "run", "mcp", "run", server_path]

    def _build_mcp_config(self) -> Dict[str, Any]:
        active = self._active_config()
        cs_id = self._effective_connected_service_id()
        service_credential = self.app_config.get("service_credential") or {}
        stdio_entrypoint = os.path.join(os.path.dirname(__file__), "apollo.io-mcp", "server.py")
        stdio_cwd = os.path.dirname(stdio_entrypoint)
        command, args = self._resolve_stdio_command(stdio_entrypoint)

        logger.info(
            "Apollo MCP config build: command=%s service_credential_present=%s connected_service_id=%s",
            command,
            bool(service_credential),
            cs_id,
        )

        mcp_config = {
            "server_type": "stdio",
            "command": command,
            "args": args,
            "cwd": stdio_cwd,
            "env": active.get("env", {}) or {},
            "url": None,
            "headers": {},
            "custom_description": self.app_config.get("custom_description", "") or self.TOOL_DESCRIPTION,
            "special_instructions": active.get("special_instructions", ""),
        }

        if cs_id:
            mcp_config["connected_service_id"] = cs_id

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

