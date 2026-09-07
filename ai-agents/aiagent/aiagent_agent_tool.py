from __future__ import annotations

"""
AI Agent Tool (app_code: aiagent)

Makes a single, plain LLM call with the given prompt and returns the raw text
output. No credentials, no multi-step planning, no tool-specific parsing of the
result - just prompt in, model output out. Useful as a building block inside
workflows/multi-step plans where a step needs a free-form LLM answer.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 1000
DEFAULT_TEMPERATURE = 0.7


class AgentEvent:
    THOUGHT = "thought"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "content": content,
    }


class AIAgentTool(BaseToolAgent):
    """
    AI Agent: a single plain LLM call. Give it a prompt, get back the model's
    text response - nothing more.
    """

    TOOL_NAME = "AI Agent"
    APP_CODE = "aiagent"

    TOOL_DESCRIPTION = """AI Agent: makes one plain LLM call with the given prompt and returns the model's raw text output. No credentials, no side effects.

Input: a natural language prompt/question. Accepts JSON from planner (e.g. {"prompt":"..."}) or query-string (prompt=...).
Optional params: max_tokens (int), temperature (float).

Output: the model's response text.
"""

    def __init__(
        self,
        llm_provider: Any,
        agent_id: Optional[str] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        user_id: Optional[int] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id or ""
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config or {}

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def get_description(self) -> str:
        return self.get_tool_responsibility()

    def _extract_params(self, user_query: str, tool_args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Pull prompt/system_prompt/max_tokens/temperature from tool_args, JSON, or query-string. Falls back to raw user_query as the prompt."""
        args = dict(tool_args or {})
        s = (user_query or "").strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if v is not None:
                            args.setdefault(str(k).strip().lower(), v)
            except json.JSONDecodeError:
                pass
        elif "prompt=" in s or "system_prompt=" in s:
            for part in s.split("&"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    args.setdefault(k.strip().lower(), v.strip())

        prompt = str(args.get("prompt") or args.get("query") or args.get("input") or "").strip()
        if not prompt:
            prompt = s if s and not s.startswith("{") else ""

        max_tokens = args.get("max_tokens")
        try:
            max_tokens = int(max_tokens) if max_tokens is not None else DEFAULT_MAX_TOKENS
        except (TypeError, ValueError):
            max_tokens = DEFAULT_MAX_TOKENS

        temperature = args.get("temperature")
        try:
            temperature = float(temperature) if temperature is not None else DEFAULT_TEMPERATURE
        except (TypeError, ValueError):
            temperature = DEFAULT_TEMPERATURE

        system_prompt = args.get("system_prompt")
        system_prompt = str(system_prompt).strip() if system_prompt else None

        return {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict, None]:
        try:
            params = self._extract_params(user_query, tool_args)
            prompt = params["prompt"]
            effective_system_prompt = params["system_prompt"] or system_prompt

            if not prompt:
                out = {"success": False, "error": "No prompt provided", "result": None}
                yield event(AgentEvent.RESULT, out)
                yield event(AgentEvent.FINAL, {"success": False, "error": out["error"], "response": None})
                return

            if not self.llm_provider or not getattr(self.llm_provider, "get_response", None):
                out = {"success": False, "error": "LLM provider not configured", "result": None}
                yield event(AgentEvent.RESULT, out)
                yield event(AgentEvent.FINAL, {"success": False, "error": out["error"], "response": None})
                return

            yield event(AgentEvent.THOUGHT, "Calling LLM...")

            response = await self.llm_provider.get_response(
                transcript=prompt,
                system_prompt=effective_system_prompt,
                max_tokens=params["max_tokens"],
                temperature=params["temperature"],
            )

            if not response or not str(response).strip():
                out = {"success": False, "error": "No response from LLM", "result": None}
                yield event(AgentEvent.RESULT, out)
                yield event(AgentEvent.FINAL, {"success": False, "error": out["error"], "response": None})
                return

            response = response.strip()
            out = {"success": True, "prompt": prompt, "result": response, "response": response}
            yield event(AgentEvent.RESULT, out)
            yield event(AgentEvent.FINAL, {"success": True, "response": response, "result": out})

        except Exception as e:
            logger.exception("AI Agent tool failed")
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {"success": False, "error": str(e), "response": None})

    async def initialize(self) -> None:
        logger.debug("AI Agent tool initialized")

    async def cleanup(self) -> None:
        logger.debug("AI Agent tool cleaned up")


__all__ = ["AIAgentTool"]
