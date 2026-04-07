"""
WhatsApp (Meta Cloud API) call provider strategy.
Uses WhatsApp Business API voice calling: initiate call and connect to AI agent when answered.
Credentials: access_token, phone_number_id (WhatsApp Business phone number ID from Meta).
"""
import logging
from typing import Dict, Any, Optional
import httpx

from .base import CallProviderStrategy, build_call_record

logger = logging.getLogger(__name__)

META_GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"


class WhatsAppCallProvider(CallProviderStrategy):
    """WhatsApp Cloud API implementation: initiate voice call and point to webhook that streams to AI agent."""

    @property
    def provider_code(self) -> str:
        return "whatsapp"

    def _to_wa_id(self, to_number: str) -> str:
        """Normalize destination to WhatsApp ID (digits only, no +)."""
        s = (to_number or "").strip()
        if s.startswith("+"):
            s = s[1:]
        return "".join(c for c in s if c.isdigit())

    async def make_call(
        self,
        to_number: str,
        credentials: Dict[str, Any],
        agent_id: str,
        company_id: str,
        session_id: Optional[str] = None,
        webhook_base_url: Optional[str] = None,
        full_callback_url: Optional[str] = None,
        direct_stream_url: Optional[str] = None,
        greet_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        access_token = (credentials.get("access_token") or credentials.get("accessToken") or "").strip()
        phone_number_id = credentials.get("phone_number_id") or credentials.get("phoneNumberId")
        if phone_number_id is not None:
            phone_number_id = str(phone_number_id).strip()
        else:
            phone_number_id = ""

        if not access_token:
            return {"success": False, "error": "WhatsApp credentials missing access_token", "call_record": None}
        if not phone_number_id:
            return {"success": False, "error": "WhatsApp credentials missing phone_number_id", "call_record": None}
        if not to_number or not to_number.strip():
            return {"success": False, "error": "Destination phone number is required", "call_record": None}
        if not full_callback_url and (not webhook_base_url or not webhook_base_url.strip()):
            return {"success": False, "error": "Webhook not configured: set CALL_WEBHOOK_BASE_URL or pass full_callback_url", "call_record": None}

        to_wa_id = self._to_wa_id(to_number)
        if not to_wa_id:
            return {"success": False, "error": "Invalid destination number", "call_record": None}

        if full_callback_url and full_callback_url.strip():
            voice_url = full_callback_url.strip()
        else:
            base = webhook_base_url.rstrip("/")
            voice_url = f"{base}/call/whatsapp/voice"
            params = f"agent_id={agent_id}&company_id={company_id}"
            if session_id:
                params += f"&session_id={session_id}"
            voice_url += "?" + params

        call_record = build_call_record(
            provider="whatsapp",
            external_id=None,
            agent_id=agent_id,
            company_id=company_id,
            session_id=session_id,
            to_identifier=to_wa_id,
            from_identifier=phone_number_id,
            status="initiated",
            metadata={"voice_url": voice_url},
        )

        try:
            version = credentials.get("graph_version") or DEFAULT_GRAPH_VERSION
            url = f"{META_GRAPH_BASE}/{version}/{phone_number_id}/calls"
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            payload = {
                "to": to_wa_id,
                "answer_url": voice_url,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    call_id = data.get("id") or data.get("call_id") or data.get("call_sid")
                    if call_id:
                        call_record["external_id"] = call_id
                        call_record["metadata"]["call_id"] = call_id
                    return {
                        "success": True,
                        "message": "WhatsApp call initiated; when the callee answers they will be connected to the AI agent.",
                        "call_sid": call_id,
                        "provider": "whatsapp",
                        "call_record": call_record,
                    }
                return {
                    "success": False,
                    "error": resp.text or f"WhatsApp API error {resp.status_code}",
                    "status_code": resp.status_code,
                    "call_record": call_record,
                }
        except httpx.RequestError as e:
            logger.exception("WhatsApp make_call request error")
            call_record["status"] = "failed"
            call_record["metadata"]["error"] = str(e)
            return {"success": False, "error": str(e), "call_record": call_record}
        except Exception as e:
            logger.exception("WhatsApp make_call error")
            call_record["status"] = "failed"
            call_record["metadata"]["error"] = str(e)
            return {"success": False, "error": str(e), "call_record": call_record}
