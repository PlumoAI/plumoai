"""
Call Agent Tool
Handles call-related actions: initiate calls and connect them to the same AI agent to talk.
Uses call_service (connected service ID) from personal/shared config; credentials and
provider code come from the factory (strategy pattern: Twilio first, more providers later).
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, AsyncGenerator, Tuple
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

from backend.services.ai_agents.base_tool_agent import BaseToolAgent

# Callback handler routes for this app (must match backend routes in call_routes.py).
# Full webhook URL = CALL_WEBHOOK_BASE_URL + this route + query params.
CALLBACK_ROUTES = {
    "twilio": "/call/twilio/voice",
    "whatsapp": "/call/whatsapp/voice",
}


class AgentEvent:
    THOUGHT = "thought"
    PLAN = "plan"
    RESULT = "result"
    FINAL = "final"
    ERROR = "error"


def event(event_type: str, content: Any) -> Dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat(),
        "content": content,
    }


async def _save_agent_call(
    token: str,
    company_id: str,
    payload: Dict[str, Any],
) -> Optional[int]:
    """
    POST to company API to save agent call (insert).
    Uses COMPANY_URL from env. Returns agent_call_id on success, None otherwise.
    """
    company_url = os.environ.get("COMPANY_URL")
    if not company_url or not token or not company_id:
        return None
    url = f"{company_url.rstrip('/')}/aiagentchat/calls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "companyids": json.dumps([str(company_id)]),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                logger.warning("save_agent_call: status=%s body=%s", r.status_code, r.text)
                return None
            data = r.json()
            if isinstance(data, dict) and data.get("type") == "success":
                inner = data.get("data") or {}
                aid = inner.get("agent_call_id")
                return int(aid) if aid is not None else None
    except Exception as e:
        logger.warning("save_agent_call error: %s", e)
    return None


async def _prepare_voice_for_call(
    token: str,
    company_id: str,
    agent_id: str,
    base_url: str,
) -> Tuple[bool, Optional[str]]:
    """
    Create transcriber connection for the agent before placing the call (Twilio, etc.).
    Call this first, then initiate the call. Returns (True, None) on success, (False, error_message) on failure.
    """
    if not base_url or not token or not agent_id or not company_id:
        return False, "Missing token, agent_id, company_id, or base URL"
    url = f"{base_url.rstrip('/')}/call/prepare-voice"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                headers=headers,
                json={"agent_id": str(agent_id), "company_id": str(company_id)},
            )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("ok") is True:
                return True, None
            err = data.get("error") or r.text or f"HTTP {r.status_code}"
            return False, err
    except Exception as e:
        logger.warning("prepare_voice_for_call error: %s", e)
        return False, str(e)


def _extract_phone_from_query(query: str) -> Optional[str]:
    """Extract a phone number from natural language (E.164-ish: digits and optional leading +)."""
    if not query or not isinstance(query, str):
        return None
    # Match + followed by digits, or a sequence of digits (with spaces/dashes)
    m = re.search(r'\+\s*\d[\d\s\-]{7,}', query)
    if m:
        return re.sub(r'[\s\-]', '', m.group(0))
    m = re.search(r'\d{10,}', query)
    if m:
        return '+' + m.group(0)
    return None


class CallAgentTool(BaseToolAgent):
    """
    Call App Agent. Initiates calls via the configured provider (Twilio, etc.);
    when the callee answers, the call is connected to the same AI agent to talk.
    """

    TOOL_NAME = "Call"
    TOOL_DESCRIPTION = """The Call Tool initiates phone calls. When the other party answers, they are connected to this AI agent to talk.

USE THIS TOOL WHEN:
- User wants to call someone, make a call, or dial a number
- User asks to initiate a call, ring a contact, or place a call
- User says "call", "phone", "dial", "ring", "call me", "call them"
- User wants this AI agent to talk with the callee during the live call (e.g., introduce services, qualify, support, or sell)

OUTPUT:
- Initiates an outbound call via the configured provider (e.g. Twilio). The callee is connected to this AI agent when they answer."""

    @classmethod
    def get_tool_responsibility(cls) -> str:
        return cls.TOOL_DESCRIPTION

    def __init__(
        self,
        llm_provider: Any,
        agent_id: Optional[str] = None,
        token: Optional[str] = None,
        company_id: Optional[str] = None,
        app_config: Optional[Dict[str, Any]] = None,
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id or ""
        self.token = token
        self.company_id = company_id or ""
        self.app_config = app_config or {}
        self._call_provider = None

    async def initialize(self) -> None:
        provider_code = (self.app_config.get("_call_provider_code") or "").strip().lower()
        if provider_code:
            try:
                from .call_providers import get_call_provider

                self._call_provider = get_call_provider(provider_code)
            except ImportError:
                self._call_provider = None
        logger.debug("Call Agent Tool initialized (provider: %s)", provider_code)

    async def cleanup(self) -> None:
        logger.debug("Call Agent Tool cleaned up")

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        *,
        system_prompt: Optional[str] = None,
        agent_guidance: Optional[str] = None,
        greet_message: Optional[str] = None,
        purpose: Optional[str] = None,
        context_message_ids: Optional[list] = None,
        current_message_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict, None]:
        """
        Process call request: resolve destination number, then use provider strategy
        to make the call and connect the callee to this AI agent.
        When greet_message is provided (from main agent resolve during step execution), use it
        with no placeholders. Runner generates it via resolve_call_greet_message when call app has guidance.
        """
        try:
            yield event(AgentEvent.THOUGHT, "Processing call request...")
            credentials_data = self.app_config.get("_call_credentials_data") or {}
            credentials = credentials_data.get("credentials") or {}
            provider_code = self.app_config.get("_call_provider_code") or ""

            if not self._call_provider or not credentials:
                yield event(AgentEvent.RESULT, {
                    "success": False,
                    "response": "Call app is not configured: connect a call service (e.g. Twilio) and set call_service in the app config.",
                    "query": user_query,
                })
                yield event(AgentEvent.FINAL, {
                    "success": False,
                    "response": "Call app is not configured.",
                    "result": {"success": False},
                })
                return

            to_number = None
            if provided_data and isinstance(provided_data, list):
                for item in provided_data:
                    if isinstance(item, dict):
                        to_number = item.get("phone_number") or item.get("phoneNumber") or item.get("to") or item.get("number")
                        if to_number:
                            break
            if not to_number:
                to_number = _extract_phone_from_query(user_query)

            if not to_number or not str(to_number).strip():
                # Signal to the main agent that this step needs discovery before it can run
                # (e.g. find phone number via CRM, enrichment, or other tools).
                yield event(AgentEvent.RESULT, {
                    "success": False,
                    "response": "I need a phone number to call. I will try to find it from available tools before asking you.",
                    "query": user_query,
                })
                yield event(AgentEvent.FINAL, {
                    "success": False,
                    "response": "Phone number required.",
                    "need_discovery": True,
                    "missing_info": [
                        {
                            "parameter": "phone_number",
                            "reason": "No phone number could be resolved from the current query or provided data.",
                            "original_query": user_query,
                        }
                    ],
                    "result": {"success": False},
                })
                return

            # Greet message: use only the one passed from runner (main agent resolve; no placeholders). Sanitize to remove any placeholders.
            if greet_message and isinstance(greet_message, str):
                t = greet_message.strip()[:500]
                t = re.sub(r"\{\{[^}]*\}\}|\[[^\]]*\]|\{[a-zA-Z_][a-zA-Z0-9_]*\}|%\([^)]*\)s", "", t)
                greet_message = re.sub(r"\s+", " ", t).strip() or None
            else:
                greet_message = None

            # First create transcriber connection, then make call (Twilio, etc.)
            webhook_base_url = os.environ.get("CALL_WEBHOOK_BASE_URL") or os.environ.get("BACKEND_URL") or os.environ.get("COMPANY_URL")
            yield event(AgentEvent.THOUGHT, "Setting up transcriber for voice call...")
            prepare_ok, prepare_err = await _prepare_voice_for_call(
                token=self.token or "",
                company_id=self.company_id,
                agent_id=self.agent_id,
                base_url=webhook_base_url or "",
            )
            if not prepare_ok:
                yield event(AgentEvent.RESULT, {
                    "success": False,
                    "response": prepare_err or "Voice pipeline not ready (transcriber setup failed).",
                    "query": user_query,
                })
                yield event(AgentEvent.FINAL, {
                    "success": False,
                    "response": prepare_err or "Transcriber setup failed.",
                    "result": {"success": False},
                })
                return

            yield event(AgentEvent.PLAN, "Initiating call via %s and connecting to this AI agent when they answer." % (provider_code or "call provider"))
            # Build stream URL with agent_id, session_id, greet_message for direct TwiML (skip webhook)
            query_parts = [f"agent_id={self.agent_id}"]
            if session_id:
                query_parts.append(f"session_id={session_id}")
            if greet_message:
                query_parts.append(f"greet_message={quote_plus(greet_message)}")
            query_string = "&".join(query_parts)
            direct_stream_url = None
            full_callback_url = None
            base = (webhook_base_url or "").strip().rstrip("/")
            if not base:
                base = (os.environ.get("CALL_WEBHOOK_BASE_URL") or os.environ.get("BACKEND_URL") or os.environ.get("COMPANY_URL") or "").strip().rstrip("/")
            if base and self.company_id and provider_code == "twilio":
                # Direct TwiML: wss stream URL with params (skip webhook)
                wss_base = base.replace("https://", "wss://").replace("http://", "ws://")
                stream_path = f"/ws/twilio-stream/{self.company_id}"
                direct_stream_url = f"{wss_base}{stream_path}"
                if query_string:
                    direct_stream_url += "?" + query_string
                logger.info("Call direct stream URL (skip webhook): %s", direct_stream_url[:120] + "..." if len(direct_stream_url) > 120 else direct_stream_url)
            if not direct_stream_url:
                twilio_callback_base = os.environ.get("TWILIO_VOICE_CALLBACK_URL", "").strip() or None
                if provider_code == "twilio" and twilio_callback_base:
                    full_callback_url = twilio_callback_base.rstrip("/") + "?" + query_string + "&company_id=" + str(self.company_id)
                    logger.info("Call callback URL (Twilio, from TWILIO_VOICE_CALLBACK_URL): %s", full_callback_url)
                elif base and self.company_id:
                    callback_path = f"/call/twilio/voice-echo/{self.company_id}"
                    full_callback_url = base + callback_path + ("?" + query_string if query_string else "")
                    logger.info("Call callback URL (webhook fallback): %s", full_callback_url)
                elif base:
                    full_callback_url = base + "/call/twilio/voice-echo" + "?" + query_string + "&company_id=" + str(self.company_id)
                    logger.info("Call callback URL (webhook fallback): %s", full_callback_url)
                else:
                    logger.warning("CALL_WEBHOOK_BASE_URL (and BACKEND_URL/COMPANY_URL) not set - Twilio callback will fail")
            out = await self._call_provider.make_call(
                to_number=str(to_number).strip(),
                credentials=credentials,
                agent_id=self.agent_id,
                company_id=self.company_id,
                session_id=session_id,
                webhook_base_url=webhook_base_url,
                full_callback_url=full_callback_url,
                direct_stream_url=direct_stream_url,
                greet_message=greet_message,
            )
            logger.info("Initiate call response: success=%s call_sid=%s provider=%s",
                        out.get("success"), out.get("call_sid"), out.get("provider"))
            print("=" * 60)
            print("INITIATE CALL RESPONSE")
            print("  success:", out.get("success"))
            print("  call_sid:", out.get("call_sid"))
            print("  provider:", out.get("provider"))
            print("  message:", out.get("message"))
            print("  error:", out.get("error"))
            print("  full response:", out)
            print("=" * 60)

            success = out.get("success", False)
            response = out.get("message") or out.get("error") or ("Call initiated." if success else "Call failed.")
            call_record = out.get("call_record")
            # Save call record to company API (insert) when call was initiated
            if success and out.get("call_sid") and self.token and self.company_id:
                try:
                    from_num = (call_record or {}).get("from_identifier") or ""
                    to_num = str(to_number).strip() if to_number else ""
                    # Prefer call serviceId, then connected service/account
                    connected_fid = self.app_config.get("_call_service_id") or 0
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    app_id = (self.app_config or {}).get("app_id") or (self.app_config or {}).get("app_fid") or (self.app_config or {}).get("id")
                    metadata = {"source": out.get("provider") or "twilio"}
                    if app_id is not None:
                        metadata["appId"] = app_id
                    if greet_message:
                        metadata["greet_message"] = greet_message
                    if context_message_ids is not None and isinstance(context_message_ids, (list, tuple)):
                        metadata["context_message_ids"] = [str(m) for m in context_message_ids if m is not None]
                    if current_message_id is not None and str(current_message_id).strip():
                        metadata["current_message_id"] = str(current_message_id).strip()
                    save_payload = {
                        "project_fid": int(self.agent_id) if str(self.agent_id).isdigit() else 0,
                        "connected_account_fid": int(connected_fid) if connected_fid else 0,
                        "external_call_id": out.get("call_sid"),
                        "to_number": to_num,
                        "from_number": from_num,
                        "direction": "outbound",
                        "status": "queued",
                        "start_time": now,
                        "call_session_id": session_id or "",
                        "metadata": metadata,
                        "purpose": (purpose or "").strip() or None,
                    }
                    if save_payload.get("purpose") is None:
                        save_payload.pop("purpose", None)
                    saved_id = await _save_agent_call(token=self.token, company_id=self.company_id, payload=save_payload)
                    if saved_id:
                        logger.info("Saved agent call on initiate: agent_call_id=%s", saved_id)
                        if greet_message:
                            logger.info("[CALL] Greet message (on initiate): %s", (greet_message[:200] + "..." if len(greet_message) > 200 else greet_message))
                except Exception as e:
                    logger.warning("Save agent call on initiate failed: %s", e)
            result = {
                "success": success,
                "response": response,
                "query": user_query,
                "session_id": session_id,
                "call_sid": out.get("call_sid"),
                "provider": out.get("provider"),
                "call_record": call_record,
            }
            yield event(AgentEvent.RESULT, result)
            yield event(AgentEvent.FINAL, {
                "success": success,
                "response": response,
                "result": result,
            })
        except Exception as e:
            logger.exception("Call agent failed")
            yield event(AgentEvent.ERROR, {"error": str(e)})
            yield event(AgentEvent.FINAL, {
                "success": False,
                "error": str(e),
                "response": None,
            })
