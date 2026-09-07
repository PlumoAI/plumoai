from __future__ import annotations

from typing import Any, Dict, List, Optional

from .sendgrid_functions import SendGridFunctions
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
        functions_class=SendGridFunctions,
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
# and registry.py's _validate_param_options).
# ---------------------------------------------------------------------------

_SUPPRESSION_TYPES = ["bounces", "blocks", "invalid_emails", "spam_reports", "global_unsubscribes"]
_TEMPLATE_GENERATIONS = ["dynamic", "legacy"]
_STATS_AGGREGATIONS = ["day", "week", "month"]


async def _options_from_templates(fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = await fn.list_templates()
    out: List[Dict[str, Any]] = []
    for t in r.get("templates") or []:
        template_id = t.get("id")
        if not template_id:
            continue
        out.append({"value": template_id, "name": t.get("name") or template_id, "description": t.get("generation") or ""})
    return out


async def _options_from_verified_senders(fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = await fn.list_verified_senders()
    out: List[Dict[str, Any]] = []
    for s in r.get("senders") or []:
        sender_id = s.get("id")
        if sender_id is None:
            continue
        out.append({"value": sender_id, "name": s.get("from_email") or str(sender_id), "description": s.get("nickname") or ""})
    return out


async def _options_from_sender_emails(fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = await fn.list_verified_senders()
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for s in r.get("senders") or []:
        email = s.get("from_email")
        if not email or email in seen:
            continue
        seen.add(email)
        out.append({"value": email, "name": email, "description": s.get("nickname") or s.get("from_name") or ""})
    return out


async def _options_from_contact_lists(fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = await fn.list_contact_lists()
    out: List[Dict[str, Any]] = []
    for l in r.get("lists") or []:
        list_id = l.get("id")
        if not list_id:
            continue
        out.append({"value": list_id, "name": l.get("name") or list_id, "description": f"{l.get('contact_count', 0)} contact(s)"})
    return out


async def _options_from_suppression_types(_fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"value": t, "name": t, "description": ""} for t in _SUPPRESSION_TYPES]


async def _options_from_template_generations(_fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"value": g, "name": g, "description": ""} for g in _TEMPLATE_GENERATIONS]


async def _options_from_stats_aggregations(_fn: SendGridFunctions, _dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"value": a, "name": a, "description": ""} for a in _STATS_AGGREGATIONS]


async def _options_from_suppressed_emails(fn: SendGridFunctions, dv: Dict[str, Any]) -> List[Dict[str, Any]]:
    suppression_type = dv.get("suppression_type")
    if not suppression_type:
        return []
    r = await fn.list_suppressions(suppression_type=suppression_type)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in r.get("entries") or []:
        email = entry.get("email") if isinstance(entry, dict) else None
        if not email or email in seen:
            continue
        seen.add(email)
        out.append({"value": email, "name": email, "description": entry.get("reason") or ""})
    return out


_RESOLVERS = {
    ("get_template", "template_id"): _options_from_templates,
    ("delete_template", "template_id"): _options_from_templates,
    ("create_template_version", "template_id"): _options_from_templates,
    ("send_template_email", "template_id"): _options_from_templates,
    ("list_templates", "generation"): _options_from_template_generations,
    ("create_template", "generation"): _options_from_template_generations,
    ("delete_verified_sender", "sender_id"): _options_from_verified_senders,
    ("send_email", "from_email"): _options_from_sender_emails,
    ("send_template_email", "from_email"): _options_from_sender_emails,
    ("delete_contact_list", "list_id"): _options_from_contact_lists,
    ("upsert_contacts", "list_ids"): _options_from_contact_lists,
    ("list_suppressions", "suppression_type"): _options_from_suppression_types,
    ("delete_suppression", "suppression_type"): _options_from_suppression_types,
    ("delete_suppression", "email"): _options_from_suppressed_emails,
    ("get_email_stats", "aggregated_by"): _options_from_stats_aggregations,
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

    fn = SendGridFunctions(token=token, user_id=user_id, company_id=company_id, app_config=app_config)
    await fn.initialize()
    try:
        return await resolver(fn, dependent_values or {})
    finally:
        await fn.cleanup()
