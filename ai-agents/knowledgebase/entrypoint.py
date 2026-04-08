from __future__ import annotations

from typing import Any, Dict, Optional

from .knowledgebase_search_tool import KnowledgebaseSearchTool


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
    Plugin entrypoint for the knowledgebase tool.
    Loads KnowledgebaseSearchTool from this plugin folder (self-contained plugin).
    """
    kb_agent = KnowledgebaseSearchTool(
        token=token,
        company_id=company_id,
        agent_id=agent_id,
        llm_provider=llm_provider,
        company_sources=(app_config or {}).get("company_sources", []) or [],
        employee_sources=(app_config or {}).get("employee_sources", []) or [],
        is_company_enabled=bool((app_config or {}).get("is_company_enabled", False)),
        is_employee_enabled=bool((app_config or {}).get("is_employee_enabled", False)),
    )
    if hasattr(kb_agent, "initialize"):
        await kb_agent.initialize()
    return kb_agent

