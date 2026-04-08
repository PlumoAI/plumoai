from __future__ import annotations

from typing import Any, Dict, Optional

from .ai_writer_agent_tool import AIWriterAgentTool


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
    Tool entrypoint for the AI Writer AI Agent tool.
    Loads AIWriterAgentTool from this tool folder (self-contained tool),
    without depending on legacy llm_tools paths.
    """
    agent = AIWriterAgentTool(
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

