"""
Call providers: strategy pattern for different telephony providers (Twilio, etc.).
"""
import logging
from typing import Dict, Optional, Type

from .base import CallProviderStrategy
from .twilio_provider import TwilioCallProvider
from .whatsapp_provider import WhatsAppCallProvider

logger = logging.getLogger(__name__)

_REGISTRY: Dict[str, Type[CallProviderStrategy]] = {
    "twilio": TwilioCallProvider,
    "whatsapp": WhatsAppCallProvider,
}


def get_call_provider(provider_code: str) -> Optional[CallProviderStrategy]:
    """Return a call provider strategy instance for the given provider code (e.g. twilio)."""
    if not provider_code:
        return None
    key = (provider_code or "").strip().lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        logger.warning("Unknown call provider: %s", provider_code)
        return None
    return cls()


def register_call_provider(provider_code: str, strategy_class: Type[CallProviderStrategy]) -> None:
    """Register a new call provider (for future providers)."""
    _REGISTRY[(provider_code or "").strip().lower()] = strategy_class
