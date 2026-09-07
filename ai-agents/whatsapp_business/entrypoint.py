from __future__ import annotations

from typing import Any, Dict, List, Optional

from .whatsapp_business_functions import WhatsAppBusinessFunctions
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
        functions_class=WhatsAppBusinessFunctions,
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


# ---------------------------------------------------------------------------
# build_param_options -- the single per-plugin dependent-options resolver
# declared for each action_or_trigger_id.param_key pair in plugin.json's
# param_options block (see backend/services/ai_agents/param_options_registry.py
# and registry.py's _validate_param_options). Dispatched by the
# (action_or_trigger_id, param_key) pair rather than by param_key alone --
# unlike PlumoAI/Cal.com's mostly-unambiguous keys, WhatsApp Business reuses
# "name" and "language_code" for unrelated free-text params elsewhere
# (send_location_message's location label, create_message_template's new
# template's own name/locale), so a bare param_key dispatch would wrongly
# attach template options to those.
# ---------------------------------------------------------------------------


async def _list_templates(fn: WhatsAppBusinessFunctions, *, name: Optional[str] = None) -> List[Dict[str, Any]]:
    r = await fn.list_message_templates(name=name, limit=200)
    return r.get("templates") or []


async def _options_from_template_names(fn: WhatsAppBusinessFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for t in await _list_templates(fn):
        name = t.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"value": name, "name": name, "description": t.get("category") or ""})
    return out


async def _options_from_template_ids(fn: WhatsAppBusinessFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in await _list_templates(fn):
        template_id = t.get("id")
        if not template_id:
            continue
        description = " · ".join(p for p in (t.get("language") or "", t.get("status") or "") if p)
        out.append({"value": template_id, "name": t.get("name") or str(template_id), "description": description})
    return out


async def _options_from_template_languages(fn: WhatsAppBusinessFunctions, dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    template_name = dv.get("template_name") or dv.get("name")
    if not template_name:
        return []
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for t in await _list_templates(fn, name=template_name):
        lang = t.get("language")
        if not lang or lang in seen:
            continue
        seen.add(lang)
        out.append({"value": lang, "name": lang, "description": t.get("status") or ""})
    return out


_RESOLVERS = {
    ("send_template_message", "template_name"): _options_from_template_names,
    ("send_template_message", "language_code"): _options_from_template_languages,
    ("get_message_template", "template_id"): _options_from_template_ids,
    ("update_message_template", "template_id"): _options_from_template_ids,
    ("delete_message_template", "name"): _options_from_template_names,
    ("delete_message_template", "template_id"): _options_from_template_ids,
    ("list_message_templates", "name"): _options_from_template_names,
    ("action_by_query", "template_name"): _options_from_template_names,
    ("action_by_query", "language_code"): _options_from_template_languages,
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
    resolver = _RESOLVERS.get((action_or_trigger_id, param_key))
    if not resolver:
        return []

    fn = WhatsAppBusinessFunctions(token=token, user_id=user_id, company_id=company_id, app_config=app_config)
    await fn.initialize()
    try:
        return await resolver(fn, dependent_values or {})
    finally:
        await fn.cleanup()
