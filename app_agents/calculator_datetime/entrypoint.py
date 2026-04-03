from __future__ import annotations

from typing import Any, Dict, Optional

from .calculator_datetime_agent_tool import CalculatorDatetimeAgentTool


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
    Plugin entrypoint for the calculator_datetime tool.
    Fully self-contained: loads CalculatorDatetimeAgentTool from this plugin folder.
    """
    agent = CalculatorDatetimeAgentTool(
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

