from __future__ import annotations

from typing import Any, Dict, Optional

from .gmail_agent_tool import GmailAgentTool


def _redact_secrets_for_log(value: Any) -> Any:
    SENSITIVE_KEY_TOKENS = (
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "refresh_token",
        "authorization",
        "bearer",
        "private",
        "key",
        "client_secret",
        "service_credential",
        "credentials",
    )

    def _looks_sensitive_key(k: str) -> bool:
        kl = (k or "").lower()
        return any(t in kl for t in SENSITIVE_KEY_TOKENS)

    def _mask_str(s: str) -> str:
        ss = (s or "").strip()
        if not ss:
            return ss
        if len(ss) >= 24:
            return ss[:6] + "…" + ss[-4:]
        return "***"

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _looks_sensitive_key(k):
                out[k] = _mask_str(str(v)) if v is not None else None
            else:
                out[k] = _redact_secrets_for_log(v)
        return out
    if isinstance(value, list):
        return [_redact_secrets_for_log(v) for v in value[:50]]
    if isinstance(value, str):
        if len(value.strip()) >= 48:
            return _mask_str(value)
        return value
    return value


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
    try:
        import json
        import logging

        logger = logging.getLogger(__name__)
        logger.info(
            "Gmail plugin create_tool_agent: app_code=%s user_id=%s company_id=%s agent_id=%s app_config=%s",
            app_code,
            user_id,
            company_id,
            agent_id,
            json.dumps(_redact_secrets_for_log(app_config or {}), default=str),
        )
    except Exception:
        pass
    agent = GmailAgentTool(
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

