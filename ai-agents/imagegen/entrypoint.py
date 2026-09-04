"""
Imagegen plugin entrypoint.

Reads imageAiService from app_config, fetches the connected service credential,
determines whether the service is OpenAI or OpenRouter, builds the appropriate
LLM provider (already holding the right API key), then injects it into
ImageGenAgentTool.

Config shape expected in app_config:
  {
    "image_model":   "gpt-image-1",      # model id forwarded to the provider
    "imageAiService": 23                 # connected_service_id for the AI service
  }
  Optional extras forwarded to the tool:
    "size":    "1024x1024"
    "quality": "standard"
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Provider codes that map to OpenAI vs OpenRouter.
# Fully dynamic: if provider_code contains "openai" → OpenAI, "openrouter" → OpenRouter.
_OPENAI_PROVIDER_CODES = frozenset({"openai", "open_ai", "open-ai"})
_OPENROUTER_PROVIDER_CODES = frozenset({"openrouter", "open_router", "open-router"})


def _extract_api_key(credentials: Dict[str, Any]) -> Optional[str]:
    """
    Extract the API key from a credential dict using common key naming conventions.
    Tries the most specific names first (api_key) before generic ones (token, key).
    """
    for key_name in (
        "api_key", "apiKey", "apikey",
        "access_token", "accessToken",
        "token", "key", "secret",
    ):
        val = credentials.get(key_name)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_active_config(app_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resolve the active config dict, unwrapping common nesting patterns.
    Falls back to app_config itself when no nested key is present.
    """
    for key in ("app_config", "shared_config", "personal_config"):
        nested = app_config.get(key)
        if nested:
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except Exception:
                    nested = {}
            if isinstance(nested, dict):
                # Handle double-nesting: shared_config -> personal_config
                inner = nested.get("personal_config")
                if inner:
                    if isinstance(inner, str):
                        try:
                            inner = json.loads(inner)
                        except Exception:
                            inner = {}
                    if isinstance(inner, dict):
                        return inner
                return nested
    return app_config


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
    Plugin entrypoint for the Image Generation tool.

    1. Resolve active config (handles nested personal/shared config).
    2. Extract imageAiService (connected_service_id for the AI service).
    3. Use service_credential if already injected; otherwise fetch directly.
    4. Determine provider type from provider_code.
    5. Build OpenAIProvider or OpenRouterProvider with the service API key.
    6. Instantiate and return ImageGenAgentTool.
    """
    from .imagegen_agent_tool import ImageGenAgentTool

    active_cfg = _resolve_active_config(app_config)

    # --- resolve image_model -------------------------------------------------
    image_model: Optional[str] = (
        active_cfg.get("image_model")
        or app_config.get("image_model")
        or None
    )

    # --- resolve imageAiService id -------------------------------------------
    raw_service_id = (
        active_cfg.get("imageAiService")
        or active_cfg.get("image_ai_service")
        or app_config.get("imageAiService")
        or app_config.get("image_ai_service")
    )
    if raw_service_id is None:
        logger.error(
            "imagegen entrypoint: 'imageAiService' not found in app_config. "
            "Cannot resolve AI service credentials."
        )
        return None

    try:
        service_id = int(raw_service_id)
    except (TypeError, ValueError):
        logger.error(
            "imagegen entrypoint: 'imageAiService' must be a numeric connected_service_id, got %r",
            raw_service_id,
        )
        return None

    # --- fetch credentials ---------------------------------------------------
    # service_credential may already be injected by the generic block in
    # ToolAgentFactory when the app_config had a standard connected_service_id.
    # For imagegen the field is named imageAiService, so it is usually NOT
    # pre-injected — fetch it here.
    credentials_data = app_config.get("service_credential")
    if credentials_data and isinstance(credentials_data, dict):
        # Verify it belongs to the right service (the generic block may have
        # injected credentials for a different service_id).
        injected_id = credentials_data.get("connected_service_id") or credentials_data.get("connected_account_id")
        if injected_id and int(injected_id) != service_id:
            logger.debug(
                "imagegen entrypoint: pre-injected service_credential is for service %s, "
                "not imageAiService %s — fetching correct credential",
                injected_id, service_id,
            )
            credentials_data = None

    if not credentials_data:
        from services.connected_account_service import get_connected_account_credentials
        logger.info(
            "imagegen entrypoint: fetching credentials for imageAiService=%s user_id=%s",
            service_id, user_id,
        )
        try:
            credentials_data = await get_connected_account_credentials(
                token=token,
                user_id=user_id,
                connected_service_id=service_id,
            )
        except Exception as exc:
            logger.error(
                "imagegen entrypoint: credential fetch failed for service_id=%s: %s",
                service_id, exc,
            )
            return None

    if not credentials_data or not isinstance(credentials_data, dict):
        logger.error(
            "imagegen entrypoint: no credentials returned for imageAiService=%s", service_id
        )
        return None

    provider_code = (credentials_data.get("provider_code") or "").strip().lower()
    credentials = credentials_data.get("credentials") or {}
    if isinstance(credentials, str):
        try:
            credentials = json.loads(credentials)
        except Exception:
            credentials = {}

    api_key = _extract_api_key(credentials)
    if not api_key:
        logger.error(
            "imagegen entrypoint: could not extract API key from credentials "
            "(provider_code=%s, credential_keys=%s)",
            provider_code, list(credentials.keys()),
        )
        return None

    # --- build the image-capable provider ------------------------------------
    image_provider: Any = None

    if any(code in provider_code for code in _OPENAI_PROVIDER_CODES) or provider_code == "openai":
        from ai_providers.openai_provider import OpenAIProvider
        image_provider = OpenAIProvider(
            api_key=api_key,
            model=image_model or "dall-e-3",
        )
        logger.info(
            "imagegen entrypoint: using OpenAIProvider model=%s service_id=%s",
            image_model or "dall-e-3", service_id,
        )
    elif any(code in provider_code for code in _OPENROUTER_PROVIDER_CODES) or provider_code == "openrouter":
        from ai_providers.openrouter_provider import OpenRouterProvider
        image_provider = OpenRouterProvider(
            api_key=api_key,
            model=image_model or "openai/dall-e-3",
        )
        logger.info(
            "imagegen entrypoint: using OpenRouterProvider model=%s service_id=%s",
            image_model or "openai/dall-e-3", service_id,
        )
    else:
        logger.error(
            "imagegen entrypoint: unknown provider_code=%r for imageAiService=%s. "
            "Expected 'openai' or 'openrouter'.",
            provider_code, service_id,
        )
        return None

    # --- build the tool ------------------------------------------------------
    tool_app_config = dict(app_config)
    tool_app_config["image_model"] = image_model
    if active_cfg.get("size"):
        tool_app_config["size"] = active_cfg["size"]
    if active_cfg.get("quality"):
        tool_app_config["quality"] = active_cfg["quality"]

    agent = ImageGenAgentTool(
        llm_provider=image_provider,
        app_config=tool_app_config,
        agent_id=agent_id or "",
        token=token,
        company_id=company_id,
        user_id=user_id,
        # The main agent chat LLM is used to synthesise a rich image prompt
        # from all available context before calling the image API.  This is the
        # same "prompt engineer" pattern that ChatGPT uses before calling DALL-E.
        chat_llm=llm_provider,
    )
    await agent.initialize()
    return agent
