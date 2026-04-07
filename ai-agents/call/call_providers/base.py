"""
Call provider strategy base.
Implementations (Twilio, WhatsApp, etc.) handle making and connecting calls using provider-specific APIs.
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def build_call_record(
    provider: str,
    external_id: Optional[str] = None,
    agent_id: str = "",
    company_id: str = "",
    session_id: Optional[str] = None,
    to_identifier: str = "",
    from_identifier: str = "",
    status: str = "initiated",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a unified call record for persistence (Twilio, WhatsApp, future providers).
    Use this structure when saving call logs to DB or session.
    """
    return {
        "provider": provider,
        "external_id": external_id,
        "agent_id": agent_id,
        "company_id": company_id,
        "session_id": session_id,
        "to_identifier": to_identifier,
        "from_identifier": from_identifier,
        "status": status,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata or {},
    }


class CallProviderStrategy(ABC):
    """Strategy interface for call providers (Twilio, future providers)."""

    @property
    @abstractmethod
    def provider_code(self) -> str:
        """Provider identifier (e.g. 'twilio')."""
        pass

    @abstractmethod
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
        """
        Initiate an outbound call. When the callee answers, the call is connected
        to the same AI agent (voice) so the callee can talk to the agent.

        Args:
            to_number: Destination phone number (E.164 or with country code).
            credentials: Provider-specific credentials (e.g. accountSID, authToken, phoneNumber).
            agent_id: Agent ID for the voice agent to use when the call connects.
            company_id: Company ID.
            session_id: Optional session ID.
            webhook_base_url: Base URL of this backend (e.g. https://api.example.com). Used if full_callback_url not set.
            full_callback_url: Full URL of the app callback handler (base + route + query). Preferred when set.
            direct_stream_url: Optional wss:// URL for Media Stream. When set, TwiML with Stream is passed directly
                to avoid webhook; Twilio connects to this URL when call is answered.
            greet_message: Optional greeting text to play via TwiML <Say> before connecting to stream.

        Returns:
            Dict with success, message, call_sid (or external_id), provider, error,
            and call_record (unified data to save: provider, external_id, agent_id, company_id,
            session_id, to_identifier, from_identifier, status, started_at, metadata).
        """
        pass
