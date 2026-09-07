from __future__ import annotations

from typing import Any, Dict, List, Optional

from .calcom_functions import CalcomFunctions, OOO_REASONS, SCHEDULE_DAYS, WEBHOOK_TRIGGERS
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
        functions_class=CalcomFunctions,
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
# and registry.py's _validate_param_options). Dispatches primarily on
# param_key, since most Cal.com params with dynamic options (event_type_id,
# schedule_id, team_id, booking_uid, webhook_id, ooo_id) resolve the same way
# no matter which action they belong to -- only mark_booking_absence's
# attendee_email genuinely needs an action-specific lookup (it depends on
# booking_uid, a param of that same action).
# ---------------------------------------------------------------------------


def _static_options(values: List[str]) -> List[Dict[str, Any]]:
    return [{"value": v, "name": v.replace("_", " ").title(), "description": ""} for v in values]


async def _options_from_event_types(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    # event_type_id is an int param on every tool that takes it (get_event_type,
    # update_event_type, create_booking, ...) -- keep the native int from Cal.com's
    # response rather than flattening it to a string.
    r = await fn.list_event_types()
    return [
        {"value": e.get("id"), "name": e.get("title") or str(e.get("id")), "description": e.get("slug") or ""}
        for e in (r.get("event_types") or [])
    ]


async def _options_from_schedules(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    r = await fn.list_schedules()
    return [
        {"value": s.get("id"), "name": s.get("name") or str(s.get("id")), "description": s.get("timeZone") or ""}
        for s in (r.get("schedules") or [])
    ]


async def _options_from_teams(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    r = await fn.list_teams()
    return [
        {"value": t.get("id"), "name": t.get("name") or str(t.get("id")), "description": t.get("slug") or ""}
        for t in (r.get("teams") or [])
    ]


async def _options_from_bookings(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    # booking_uid is a str param everywhere it's used -- Cal.com's own identifier
    # for a booking is a uid string, not a numeric id.
    r = await fn.list_bookings(limit=100)
    return [
        {"value": b.get("uid"), "name": b.get("title") or b.get("uid") or "", "description": b.get("start") or ""}
        for b in (r.get("bookings") or [])
        if b.get("uid")
    ]


async def _options_from_webhooks(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    # webhook_id is a str param on update_webhook/delete_webhook (Cal.com's webhook
    # path param is documented as a string) even though the "id" field in the
    # webhook object itself comes back as a number -- convert deliberately here to
    # match what those tools expect, not as a blanket stringify-everything rule.
    r = await fn.list_webhooks()
    return [
        {"value": str(w.get("id")), "name": w.get("subscriberUrl") or str(w.get("id")), "description": ", ".join(w.get("triggers") or [])}
        for w in (r.get("webhooks") or [])
    ]


async def _options_from_out_of_office(fn: CalcomFunctions) -> List[Dict[str, Any]]:
    r = await fn.list_out_of_office()
    return [
        {"value": o.get("id"), "name": o.get("notes") or f"{o.get('start')} - {o.get('end')}", "description": o.get("reason") or ""}
        for o in (r.get("out_of_office") or [])
    ]


async def _options_from_booking_attendees(fn: CalcomFunctions, booking_uid: str) -> List[Dict[str, Any]]:
    r = await fn.get_booking(booking_uid)
    attendees = (r.get("booking") or {}).get("attendees") or []
    return [{"value": a.get("email"), "name": a.get("name") or a.get("email") or "", "description": ""} for a in attendees if a.get("email")]


_STATIC_RESOLVERS = {
    "triggers": lambda: _static_options(list(WEBHOOK_TRIGGERS)),
    "week_start": lambda: _static_options(list(SCHEDULE_DAYS)),
    "reason": lambda: _static_options(list(OOO_REASONS)),
}

_API_RESOLVERS = {
    "event_type_id": _options_from_event_types,
    "schedule_id": _options_from_schedules,
    "default_schedule_id": _options_from_schedules,
    "team_id": _options_from_teams,
    "booking_uid": _options_from_bookings,
    "webhook_id": _options_from_webhooks,
    "ooo_id": _options_from_out_of_office,
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
    static_resolver = _STATIC_RESOLVERS.get(param_key)
    if static_resolver:
        return static_resolver()

    if action_or_trigger_id == "mark_booking_absence" and param_key == "attendee_email":
        booking_uid = dependent_values.get("booking_uid")
        if not booking_uid:
            return []
        fn = CalcomFunctions(token=token, user_id=user_id, company_id=company_id, app_config=app_config)
        await fn.initialize()
        try:
            return await _options_from_booking_attendees(fn, booking_uid)
        finally:
            await fn.cleanup()

    api_resolver = _API_RESOLVERS.get(param_key)
    if not api_resolver:
        return []

    fn = CalcomFunctions(token=token, user_id=user_id, company_id=company_id, app_config=app_config)
    await fn.initialize()
    try:
        return await api_resolver(fn)
    finally:
        await fn.cleanup()
