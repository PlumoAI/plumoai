from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

from llm_tools.mcp_agent_tool import MCPAgentTool

logger = logging.getLogger(__name__)


class PlumoAIMCPAgentTool(BaseToolAgent):
    """
    Thin BaseToolAgent wrapper around the MCPAgentTool instance produced by
    ToolAgentFactory._create_mcp_agent() for app_code="plumoai" -- reuses that
    method's existing connection/auth logic unchanged (including its
    plumoai-specific OAuth/request-token handling), only adapting the result
    to the plugin contract so this first-party app gets the same
    plugin.json-based param_options wiring as every other plugin (e.g.
    Cal.com), instead of the raw legacy app_type=="mcp" dispatch path.

    available_tools/available_resources/get_inner_tool_names are passed
    straight through to the delegate -- several call sites elsewhere
    (agent_chat_stream_runner.py, agent_v2/agent_brain.py) read these directly
    off the tool-agent instance, not only via a `_delegate` fallback.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return "PlumoAI AI Employees/CRM -- manage AI Employees, tables, pipelines, and records"

    @property
    def available_tools(self):
        return getattr(self._delegate, "available_tools", None) or []

    @property
    def available_resources(self):
        return getattr(self._delegate, "available_resources", None) or []

    def get_inner_tool_names(self) -> List[str]:
        fn = getattr(self._delegate, "get_inner_tool_names", None)
        return list(fn()) if callable(fn) else []

    async def cleanup(self) -> None:
        cleanup = getattr(self._delegate, "cleanup", None)
        if callable(cleanup):
            await cleanup()

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        conversation_history: Optional[Any] = None,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[Any] = None,
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        **_extra_tool_args: Any,
    ) -> AsyncGenerator[Dict, None]:
        merged_tool_args: Dict[str, Any] = dict(tool_args) if tool_args else {}
        if _extra_tool_args:
            merged_tool_args.update(_extra_tool_args)

        run_kwargs: Dict[str, Any] = {"query": user_query, "provided_data": provided_data}
        if merged_tool_args:
            run_kwargs["tool_args"] = merged_tool_args
        if tool_name:
            run_kwargs["tool_name"] = tool_name

        async for event in self._delegate.run(**run_kwargs):
            yield event



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
    from backend.services.tool_agent_factory import ToolAgentFactory

    delegate = await ToolAgentFactory._create_mcp_agent(
        app_code=app_code,
        app_config=app_config or {},
        llm_provider=llm_provider,
        token=token,
        user_id=user_id,
        company_id=company_id,
        agent_id=agent_id,
    )
    if delegate is None:
        return None
    return PlumoAIMCPAgentTool(delegate)


# ---------------------------------------------------------------------------
# build_param_options -- the single per-plugin dependent-options resolver
# declared for each action_or_trigger_id.param_key pair in plugin.json's
# param_options block (see backend/services/ai_agents/param_options_registry.py
# and registry.py's _validate_param_options). Dispatches primarily on
# param_key, since every plumoai param with dynamic options resolves the same
# way no matter which action it belongs to -- the underlying MCP tools are
# called directly (bypassing the LLM ReAct loop) via the same MCPAgentTool
# used for real chat calls.
#
# ID USAGE GUIDE (per the live plumoai MCP catalog): ai_employee_id -> table_id ->
# pipeline_id -> status_id. Param-name casing is inconsistent across tools:
# aiEmployeeId/tableId/pipelineId on the ai_employee_table_* utility tools vs.
# ai_employee_id/table_id/pipeline_id/status_id on record_list/record_create/
# record_update -- _first() below resolves either casing.
# ---------------------------------------------------------------------------


def _first(dependent_values: Dict[str, Any], *keys: str) -> Optional[Any]:
    for k in keys:
        v = dependent_values.get(k)
        if v:
            return v
    return None


async def _fetch(agent: Any, tool_name: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Direct MCP tool call bypassing the LLM decision loop, for read-only option lookups."""
    clean_args = {k: v for k, v in (args or {}).items() if v}
    try:
        raw = await agent._call_tool(tool_name, clean_args)
    except Exception as e:
        logger.warning("plumoai param_options: _call_tool(%s) failed: %s", tool_name, e)
        return []

    if not raw.get("success"):
        logger.warning("plumoai param_options: %s returned error: %s", tool_name, raw.get("error"))
        return []

    for block in raw.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if isinstance(data, list):
            return data
    return []


async def _options_from_workspaces(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = await _fetch(agent, "ai_employee_list", {})
    seen: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        wid = r.get("workspace_id")
        if wid and wid not in seen:
            seen[wid] = {"value": wid, "name": r.get("workspace") or str(wid), "description": ""}
    return list(seen.values())


async def _options_from_ai_employees(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    workspace_id = _first(dependent_values, "workspace_id")
    rows = await _fetch(agent, "ai_employee_list", {"workspace_id": workspace_id} if workspace_id else {})
    return [
        {
            "value": r.get("project_id"),
            "name": r.get("project_name") or str(r.get("project_id")),
            "description": r.get("project_description") or r.get("template_name") or "",
        }
        for r in rows
        if r.get("project_id")
    ]


async def _options_from_tables(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    ai_employee_id = _first(dependent_values, "ai_employee_id", "aiEmployeeId")
    if not ai_employee_id:
        return []
    rows = await _fetch(agent, "ai_employee_tables_list", {"aiEmployeeId": ai_employee_id})
    return [
        {"value": r.get("table_id"), "name": r.get("table_name") or str(r.get("table_id")), "description": ""}
        for r in rows
        if r.get("table_id")
    ]


async def _options_from_pipelines(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    ai_employee_id = _first(dependent_values, "ai_employee_id", "aiEmployeeId")
    table_id = _first(dependent_values, "table_id", "tableId")
    if not ai_employee_id or not table_id:
        return []
    rows = await _fetch(agent, "ai_employee_table_pipelines", {"aiEmployeeId": ai_employee_id, "tableId": table_id})
    return [
        {"value": r.get("pipeline_id"), "name": r.get("pipeline_name") or str(r.get("pipeline_id")), "description": ""}
        for r in rows
        if r.get("pipeline_id")
    ]


async def _options_from_statuses(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    ai_employee_id = _first(dependent_values, "ai_employee_id", "aiEmployeeId")
    table_id = _first(dependent_values, "table_id", "tableId")
    if not ai_employee_id or not table_id:
        return []

    pipeline_id = _first(dependent_values, "pipeline_id", "pipelineId")
    if pipeline_id:
        # ai_employee_table_pipeline_status_list action: pipeline already chosen.
        rows = await _fetch(
            agent,
            "ai_employee_table_pipeline_status_list",
            {"aiEmployeeId": ai_employee_id, "tableId": table_id, "pipelineId": pipeline_id},
        )
        return [
            {"value": r.get("status_id"), "name": r.get("status_name") or str(r.get("status_id")), "description": r.get("category_name") or ""}
            for r in rows
            if r.get("status_id")
        ]

    # record_create/record_update: no pipeline_id param on that action, so
    # resolve every pipeline on the table first and flatten their statuses.
    pipelines = await _fetch(agent, "ai_employee_table_pipelines", {"aiEmployeeId": ai_employee_id, "tableId": table_id})
    options: List[Dict[str, Any]] = []
    for p in pipelines:
        pid = p.get("pipeline_id")
        pname = p.get("pipeline_name") or ""
        if not pid:
            continue
        rows = await _fetch(
            agent,
            "ai_employee_table_pipeline_status_list",
            {"aiEmployeeId": ai_employee_id, "tableId": table_id, "pipelineId": pid},
        )
        multi = len(pipelines) > 1
        for r in rows:
            if not r.get("status_id"):
                continue
            category = r.get("category_name") or ""
            description = f"{category} · {pname}" if multi and pname else category
            options.append({"value": r.get("status_id"), "name": r.get("status_name") or str(r.get("status_id")), "description": description})
    return options


async def _options_from_table_fields(agent: Any, dependent_values: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Option value is field_id -- used by record_list/record_create/record_update/
    record_bulk_create, whose recordFieldValues/filter entries key on field_id."""
    ai_employee_id = _first(dependent_values, "ai_employee_id", "aiEmployeeId")
    table_id = _first(dependent_values, "table_id", "tableId")
    if not ai_employee_id or not table_id:
        return []
    rows = await _fetch(agent, "ai_employee_table_fields", {"ai_employee_id": ai_employee_id, "table_id": table_id})
    options: List[Dict[str, Any]] = []
    for r in rows:
        field_id = r.get("field_id") or r.get("proj_field_id")
        if not field_id:
            continue
        name = r.get("field_name") or r.get("field_key") or str(field_id)
        field_type = r.get("type") or ""
        required = " · required" if r.get("is_required") else ""
        options.append({"value": field_id, "name": name, "description": f"{field_type}{required}"})
    return options


_RESOLVERS = {
    "workspace_id": _options_from_workspaces,
    "aiEmployeeId": _options_from_ai_employees,
    "ai_employee_id": _options_from_ai_employees,
    "tableId": _options_from_tables,
    "table_id": _options_from_tables,
    "pipelineId": _options_from_pipelines,
    "pipeline_id": _options_from_pipelines,
    "status_id": _options_from_statuses,
    "recordFieldValues.field_id": _options_from_table_fields,
    "records.recordFieldValues.field_id": _options_from_table_fields,
    "filter.conditions.field_id": _options_from_table_fields,
}


async def build_param_options(
    *,
    action_or_trigger_id: str,
    param_key: str,
    dependent_values: Dict[str, Any],
    app_config: Dict[str, Any],
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    resolver = _RESOLVERS.get(param_key)
    if not resolver:
        return []

    from backend.services.tool_agent_factory import ToolAgentFactory
   
    active = app_config
    service_credential = app_config.get("service_credential") or {}
    connected_service_id = service_credential.get("connected_service_id")
    stdio_entrypoint = os.path.join(os.path.dirname(__file__), "plumoai-mcp", "start-stdio.js")
    stdio_cwd = os.path.dirname(stdio_entrypoint)

    # logger.info(
    #     "PlumoAI MCP config build: active_config=%s service_credential_present=%s connected_service_id=%s app_config_keys=%s",
    #     json.dumps(active, default=str),
    #     bool(service_credential),
    #     connected_service_id,
    #     list(app_config.keys()),
    # )


    agent = await ToolAgentFactory._create_mcp_agent(
        app_code="plumoai",
        app_config=app_config or {},
        llm_provider=None,
        token=token,
        user_id=user_id,
        company_id=company_id,
        agent_id=None,
    )
    if agent is None:
        return []
    try:
        return await resolver(agent, dependent_values or {})
    finally:
        cleanup = getattr(agent, "cleanup", None)
        if callable(cleanup):
            try:
                await cleanup()
            except Exception as e:
                logger.warning("plumoai param_options: agent cleanup failed: %s", e)