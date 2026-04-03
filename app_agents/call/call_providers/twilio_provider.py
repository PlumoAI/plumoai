"""
Twilio call provider strategy.
Uses Twilio REST API to make outbound calls and connect them to the AI agent via Stream.
Follows: https://www.twilio.com/docs/voice/tutorials/how-to-make-outbound-phone-calls

- Create call: POST /2010-04-01/Accounts/{AccountSid}/Calls.json
- Required: From (Twilio number), To (E.164), Url or Twiml (TwiML – when direct_stream_url set, use Twiml to skip webhook)
- Credentials: phoneNumber (countryCode, phoneNumber), accountSID, authToken.
"""
import logging
import os
import secrets
from typing import Dict, Any, Optional
import httpx

from .base import CallProviderStrategy, build_call_record

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class TwilioCallProvider(CallProviderStrategy):
    """Twilio implementation: create outbound call and point to webhook that streams to AI agent."""

    @property
    def provider_code(self) -> str:
        return "twilio"

    def _from_number(self, credentials: Dict[str, Any]) -> Optional[str]:
        """Build From number from credentials.phoneNumber (countryCode + phoneNumber)."""
        pn = credentials.get("phoneNumber")
        if isinstance(pn, dict):
            cc = (pn.get("countryCode") or "").strip()
            num = (pn.get("phoneNumber") or "").strip()
            if num:
                return f"{cc}{num}" if cc else num
        return credentials.get("from") or credentials.get("phoneNumber")

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
        account_sid = (credentials.get("accountSID") or credentials.get("account_sid") or "").strip()
        auth_token = (credentials.get("authToken") or credentials.get("auth_token") or "").strip()
        from_number = self._from_number(credentials)

        if not account_sid or not auth_token:
            return {"success": False, "error": "Twilio credentials missing accountSID or authToken", "call_record": None}
        if not from_number:
            return {"success": False, "error": "Twilio credentials missing phoneNumber (countryCode + phoneNumber)", "call_record": None}
        if not to_number or not to_number.strip():
            return {"success": False, "error": "Destination phone number is required", "call_record": None}
        if not direct_stream_url and not full_callback_url and (not webhook_base_url or not webhook_base_url.strip()):
            return {"success": False, "error": "Webhook or direct_stream_url not configured: set CALL_WEBHOOK_BASE_URL or pass full_callback_url or direct_stream_url", "call_record": None}

        to_number = to_number.strip()
        if not to_number.startswith("+"):
            to_number = "+" + to_number

        # When direct_stream_url: pass TwiML directly (skip webhook). Otherwise use webhook URL.
        use_direct_twiml = bool(direct_stream_url and direct_stream_url.strip())
        if use_direct_twiml:
            stream_url = direct_stream_url.strip()
            stream_url_escaped = stream_url.replace("&", "&amp;")
            track_attr = ""
            track = (os.environ.get("TWILIO_STREAM_TRACK") or "").strip().lower()
            if track in ("both_tracks", "outbound_track", "inbound_track"):
                track_attr = f' track="{track}"'
            say_block = ""
            if greet_message and str(greet_message).strip():
                greet_escaped = (
                    str(greet_message).strip()
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                    .replace("'", "&apos;")
                )
                voice = (os.environ.get("TWILIO_SAY_VOICE") or "alice").strip()
                language = (os.environ.get("TWILIO_SAY_LANGUAGE") or "en-GB").strip()
                say_block = f'<Say voice="{voice}" language="{language}">{greet_escaped}</Say>'
            connect_block = f'<Connect><Stream url="{stream_url_escaped}"{track_attr}/></Connect>'
            twiml_body = f'<?xml version="1.0" encoding="UTF-8"?><Response>{say_block}{connect_block}</Response>'
        base = (webhook_base_url or "").strip().rstrip("/")
        if not base:
            base = (os.environ.get("CALL_WEBHOOK_BASE_URL") or os.environ.get("BACKEND_URL") or os.environ.get("COMPANY_URL") or "").strip().rstrip("/")
        if not use_direct_twiml:
            if full_callback_url and full_callback_url.strip():
                twilio_webhook_url = full_callback_url.strip()
                base = full_callback_url.strip().split("/call/")[0].rstrip("/")
            else:
                twilio_webhook_url = f"{base}/call/twilio/voice-echo/{company_id}/{secrets.token_urlsafe(8)}"
      
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                status_qs = [f"agent_id={agent_id}"]
                if session_id:
                    status_qs.append(f"session_id={session_id}")
                status_callback_url = f"{base}/call/twilio/status/{company_id}"
                if status_qs:
                    status_callback_url += "?" + "&".join(status_qs)
                recording_qs = list(status_qs) + ["callback_kind=recording"]
                recording_callback_url = f"{base}/call/twilio/status/{company_id}"
                if recording_qs:
                    recording_callback_url += "?" + "&".join(recording_qs)
                post_data = {
                    "From": from_number,
                    "To": to_number,
                    "StatusCallback": status_callback_url,
                    "StatusCallbackMethod": "POST",
                    "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                    "Record": "true",
                    "RecordingStatusCallback": recording_callback_url,
                    "RecordingStatusCallbackMethod": "POST",
                    "RecordingStatusCallbackEvent": ["completed"],
                    "timeout": 100,
                }
                if use_direct_twiml:
                    post_data["Twiml"] = twiml_body
                    logger.info("Twilio: using direct TwiML (skip webhook), stream_url=%s", stream_url[:100] + "..." if len(stream_url) > 100 else stream_url)
                else:
                    post_data["Url"] = twilio_webhook_url
                # Log post data (truncate Twiml for readability)
                log_data = dict(post_data)
                if "Twiml" in log_data:
                    t = log_data["Twiml"]
                    log_data["Twiml"] = (t[:300] + "..." if len(t) > 300 else t)
                logger.info("Twilio POST data: %s", log_data)
                resp = await client.post(
                    f"{TWILIO_API_BASE}/Accounts/{account_sid}/Calls.json",
                    auth=(account_sid, auth_token),
                    data=post_data,
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    call_sid = data.get("sid")
                    logger.info("Twilio initiate call response: status=%s sid=%s status_call=%s",
                                resp.status_code, call_sid, data.get("status"))
                    print("=" * 60)
                    print("TWILIO INITIATE CALL RESPONSE")
                    print("  status_code:", resp.status_code)
                    print("  call_sid:", call_sid)
                    print("  Twilio response:", data)
                    print("=" * 60)
                    call_record = build_call_record(
                        provider="twilio",
                        external_id=call_sid,
                        agent_id=agent_id,
                        company_id=company_id,
                        session_id=session_id,
                        to_identifier=to_number,
                        from_identifier=from_number or "",
                        status="initiated",
                        metadata={"call_sid": call_sid},
                    )
                    return {
                        "success": True,
                        "message": "Call initiated; when the callee answers they will be connected to the AI agent.",
                        "call_sid": call_sid,
                        "provider": "twilio",
                        "call_record": call_record,
                    }
                logger.warning("Twilio initiate call failed: status=%s body=%s", resp.status_code, resp.text)
                print("TWILIO INITIATE CALL FAILED: status=%s body=%s" % (resp.status_code, resp.text))
                return {
                    "success": False,
                    "error": resp.text or f"Twilio API error {resp.status_code}",
                    "status_code": resp.status_code,
                    "call_record": None,
                }
        except httpx.RequestError as e:
            logger.exception("Twilio make_call request error")
            return {"success": False, "error": str(e), "call_record": None}
        except Exception as e:
            logger.exception("Twilio make_call error")
            return {"success": False, "error": str(e), "call_record": None}
