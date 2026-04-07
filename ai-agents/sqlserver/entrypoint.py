from __future__ import annotations

from typing import Any, Dict, Optional

from .sqlserver_agent_tool import SQLServerAgentTool


async def create_tool_agent(
    *,
    app_code: str,
    app_config: Dict[str, Any],
    llm_provider: Any,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    # SQLServerAgentTool expects connection_string, permissions, agent_instructions, etc.
    # Those are typically injected into app_config by the core factory before calling plugins.
    connection_string = (app_config or {}).get("connection_string") or (app_config or {}).get("sqlserver_connection_string") or ""
    permissions = (app_config or {}).get("permissions") or "read-only"
    agent_instructions = (app_config or {}).get("agent_instructions") or None

    agent = SQLServerAgentTool(
        connection_string=connection_string,
        llm_provider=llm_provider,
        permissions=permissions,
        agent_instructions=agent_instructions,
    )
    if hasattr(agent, "initialize"):
        await agent.initialize()
    return agent

