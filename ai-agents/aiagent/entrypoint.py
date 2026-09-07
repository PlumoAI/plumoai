from __future__ import annotations

from typing import Any, Dict, Optional

from .aiagent_agent_tool import AIAgentTool


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
    """
    Plugin entrypoint for the aiagent tool.
    Fully self-contained: loads AIAgentTool from this plugin folder.
    """
    agent = AIAgentTool(
        llm_provider=llm_provider,
        agent_id=agent_id or "",
        token=token,
        company_id=company_id,
        user_id=user_id,
        app_config=app_config or {},
    )
    if hasattr(agent, "initialize"):
        await agent.initialize()
    return agent
