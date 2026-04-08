from __future__ import annotations

from typing import Any, Dict, Optional

from .chart_maker_agent_tool import ChartMakerAgentTool


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
    Tool entrypoint for the chartmaker AI Agent tool.
    Loads ChartMakerAgentTool from this tool folder (self-contained tool).
    """
    agent_instructions = (app_config or {}).get("custom_description") or (app_config or {}).get(
        "agent_instructions"
    )
    agent = ChartMakerAgentTool(
        llm_provider=llm_provider,
        main_agent=None,
        sql_agent=None,
        agent_instructions=agent_instructions,
    )
    if hasattr(agent, "initialize"):
        await agent.initialize()
    return agent

