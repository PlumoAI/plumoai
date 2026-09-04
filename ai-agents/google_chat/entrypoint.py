from __future__ import annotations

from typing import Any, Dict, Optional

from .google_chat_functions import GoogleChatFunctions
from llm_tools.functions_wrapper_agent_tool import FunctionsWrapperAgentTool


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
    agent = FunctionsWrapperAgentTool(
        functions_class=GoogleChatFunctions,
        functions_config={
            "token": token,
            "user_id": user_id,
            "company_id": company_id,
            "agent_id": agent_id or "",
            "app_config": app_config or {},
        },
        llm_provider=llm_provider,
        token=token,
        user_id=user_id,
        company_id=company_id,
        agent_id=agent_id or "",
        app_config=app_config or {},
    )
    await agent.initialize()
    return agent
