"""
OpenRouter AI Provider
"""
import asyncio
import os
import logging
import traceback
import httpx
import json
from typing import Any, AsyncIterator, Dict, List, Optional
from .base_provider import AIProvider

logger = logging.getLogger(__name__)

# Create a shared HTTP client with connection pooling for better performance
_shared_client: Optional[httpx.AsyncClient] = None


def _normalize_min_output_tokens(val: Optional[int], *, minimum: int = 16) -> Optional[int]:
    """
    OpenRouter enforces a minimum output token count for some upstream providers.
    Keep behavior dynamic by only applying a lower-bound clamp when the caller
    explicitly supplies a value (including very small classifier calls).
    """
    if val is None:
        return None
    try:
        iv = int(val)
    except Exception:
        return minimum
    return minimum if iv < minimum else iv


def _credential_summary(api_key: Optional[str]) -> str:
    """Safe summary for logging: never log the full key."""
    if not api_key:
        return "present=False, len=0, source=env_or_arg"
    k = api_key.strip()
    prefix = (k[:8] + "...") if len(k) > 8 else "***"
    return f"present=True, len={len(k)}, prefix={prefix!r}"


def _mask_key_half(api_key: Optional[str]) -> str:
    """
    Mask an API key by showing only the first half.
    Example: 'sk-abc12345' -> 'sk-ab*****' (roughly).
    """
    if not api_key:
        return "(missing)"
    k = str(api_key).strip()
    if not k:
        return "(missing)"
    # Never reveal very short secrets; just report length.
    if len(k) < 8:
        return f"(masked,len={len(k)})"
    visible = max(4, len(k) // 2)
    return k[:visible] + ("*" * (len(k) - visible))


class OpenRouterProvider(AIProvider):
    """OpenRouter AI provider implementation"""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, enable_web_search: bool = False):
        env_key = os.getenv("OPENROUTER_API_KEY")
        self.api_key = api_key if (api_key and api_key.strip()) else (env_key if (env_key and env_key.strip()) else None)
        # Get model from env var or use default
        self.model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        self.enable_web_search = bool(enable_web_search)
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"
        # Reduced timeout for faster failure detection (15s connect, 60s read)
        self.timeout = httpx.Timeout(15.0, connect=10.0, read=60.0)
        logger.info("OpenRouter credential check: %s, model=%s, enable_web_search=%s", _credential_summary(self.api_key), self.model or "(default)", self.enable_web_search)
    
    def is_configured(self) -> bool:
        """Check if OpenRouter is configured"""
        if not self.api_key:
            return False
        # Check if it's still the placeholder
        if self.api_key == "your_api_key_here" or len(self.api_key) < 20:
            return False
        return True
    
    async def get_response(
        self,
        transcript: str,
        conversation_history: Optional[list] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        use_web_search: Optional[bool] = None,
        **kwargs: Any
    ) -> Optional[str]:
        """Get response from OpenRouter"""
        if not self.is_configured():
            return None

        try:
            # Guardrail: avoid overlong user content that can trip context limits
            user_text = transcript or ""
            if isinstance(user_text, str):
                max_len = 16000  # ~4k tokens rough
                if len(user_text) > max_len:
                    head = user_text[: max_len // 2]
                    tail = user_text[-max_len // 2 :]
                    user_text = head + "\n\n...[truncated middle for length]...\n\n" + tail
            # Build messages with system prompt
            # Use custom system prompt from knowledgebase if provided (same as chat API)
            # Otherwise use default voice agent prompt
            if system_prompt:
                # Use knowledgebase system prompt (same as chat API)
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt
                    }
                ]
            else:
                # Default voice agent prompt (for backward compatibility)
                messages = [
                    {
                        "role": "system",
                        "content": """You are a real-time voice AI agent. Your reasoning is SILENT and FAST.

CRITICAL RULES:
1. NEVER speak your reasoning out loud - reason internally only
2. Keep responses SHORT (1 sentence, max 2 sentences)
3. Make decisions FAST (reactive reasoning for simple queries)
4. Respond with ACTION, not explanation

Reasoning Process (INTERNAL ONLY):
- Observe: Understand user input and context
- Interpret: Extract intent (question? command? interruption?)
- Reason: Decide quickly - is this conversational? factual? command?
- Decide: Choose action - speak directly, acknowledge, or ask clarification
- Act: Provide SHORT spoken response

Response Guidelines:
- For questions: Answer directly, 1 sentence
- For commands: Acknowledge briefly, then act
- For interruptions: "Okay" or "Got it" - then stop
- For unclear input: Ask ONE short clarification question
- Use same language as user (Hindi, English, or mixed)
- Sound natural and conversational, not robotic
- NEVER explain your thinking process - just respond

Example:
User: "What's the weather?"
You: "It's sunny and 72 degrees." (NOT: "Let me think... I need to check the weather...")

User: "Wait, cancel that"
You: "Okay, cancelled." (NOT: "I understand you want to cancel...")

Remember: Reason silently, act fast, speak only the final decision."""
                    }
                ]
            
            # Add conversation history for context
            # conversation_history is already limited to contextWindowLength by the caller
            if conversation_history:
                for exchange in conversation_history:  # Use all provided exchanges (already limited)
                    if "user" in exchange:
                        messages.append({"role": "user", "content": exchange["user"]})
                    if "assistant" in exchange:
                        messages.append({"role": "assistant", "content": exchange["assistant"]})
            
            # Add current user message
            messages.append({
                "role": "user",
                "content": user_text
            })
            
            # Use provided parameters or defaults optimized for voice
            # Voice mode: shorter responses, faster generation
            voice_max_tokens = _normalize_min_output_tokens(max_tokens) if max_tokens is not None else 100
            voice_temperature = temperature if temperature is not None else 0.9
            
            # Reduce timeout for voice agent (faster failure detection)
            voice_timeout = httpx.Timeout(10.0, connect=5.0, read=30.0)  # Faster than default
            
            request_body = {
                "model": self.model,
                "messages": messages,
                "temperature": voice_temperature,
                "max_tokens": voice_max_tokens,  # Voice-optimized: shorter for speed
            }
            add_web = use_web_search if use_web_search is not None else self.enable_web_search
            if add_web:
                request_body["plugins"] = [{"id": "web"}]
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "PlumoAI Digital Employee",
                "Content-Type": "application/json",
            }
            ai_response = None
            _MAX_EMPTY_RETRIES = 2  # preserve previous behavior: 1 initial + 2 retries for empty content
            _MAX_429_RETRIES = 3    # user requirement: retry max 3 times on HTTP 429
            _RETRY_429_WAIT_S = 2.0

            for attempt in range(_MAX_EMPTY_RETRIES + 1):
                async with httpx.AsyncClient(timeout=voice_timeout) as client:
                    logger.info(
                        "OpenRouter request: model=%s, api_key=%s",
                        self.model,
                        _mask_key_half(self.api_key),
                    )

                    retry_429_count = 0
                    while True:
                        response = await client.post(
                            self.base_url,
                            headers=headers,
                            json=request_body,
                        )
                        if response.status_code == 429 and retry_429_count < _MAX_429_RETRIES:
                            retry_429_count += 1
                            logger.warning(
                                "⚠️ OpenRouter 429 rate limited — waiting %.0fs then retrying (%d/%d)...",
                                _RETRY_429_WAIT_S,
                                retry_429_count,
                                _MAX_429_RETRIES,
                            )
                            await asyncio.sleep(_RETRY_429_WAIT_S)
                            continue
                        response.raise_for_status()
                        data = response.json()
                        break

                if "choices" in data and len(data["choices"]) > 0:
                    msg = data["choices"][0].get("message") or {}
                    ai_response = msg.get("content")
                    citations = kwargs.get("citations")
                    if citations is not None and isinstance(citations, list) and add_web:
                        for a in (msg.get("annotations") or []):
                            if isinstance(a, dict) and a.get("type") == "url_citation":
                                uc = a.get("url_citation") or a
                                if isinstance(uc, dict) and uc.get("url"):
                                    citations.append({
                                        "url": uc.get("url"),
                                        "title": uc.get("title"),
                                        "content": uc.get("content"),
                                        "start_index": uc.get("start_index"),
                                        "end_index": uc.get("end_index"),
                                    })
                    if ai_response and ai_response.strip():
                        return ai_response.strip()
                if attempt < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "⚠️ OpenRouter returned empty response content, retrying (%d/%d)...",
                        attempt + 1,
                        _MAX_EMPTY_RETRIES,
                    )
            logger.warning(
                "⚠️ OpenRouter returned empty response content after %d attempts",
                _MAX_EMPTY_RETRIES + 1,
            )
            return None
                    
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ OpenRouter HTTP error: {e.response.status_code}")
            if e.response.status_code == 401:
                logger.error("   → Invalid API key or unauthorized")
                logger.warning("   OpenRouter credential used for this request: %s", _credential_summary(self.api_key))
            elif e.response.status_code == 429:
                logger.error("   → Rate limited - too many requests")
            else:
                try:
                    error_body = e.response.text
                    logger.error(f"   → Response: {error_body[:200]}")
                except:
                    pass
            return None
        except Exception as e:
            logger.error(f"❌ OpenRouter error: {str(e)}")
            return None
    
    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None
    ) -> AsyncIterator[str]:
        """
        Stream AI response from OpenRouter using Server-Sent Events
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens in response
            
        Yields:
            Response text chunks as they are generated
        """
        if not self.is_configured():
            return
        
        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": _normalize_min_output_tokens(max_tokens) if max_tokens is not None else 1000,
            "stream": True,  # Enable streaming
        }
        if self.enable_web_search:
            request_body["plugins"] = [{"id": "web"}]

        _MAX_429_RETRIES = 3  # user requirement: retry max 3 times on HTTP 429
        _RETRY_429_WAIT_S = 2.0

        for _stream_attempt in range(_MAX_429_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    logger.info(
                        "OpenRouter stream request: model=%s, api_key=%s",
                        self.model,
                        _mask_key_half(self.api_key),
                    )
                    async with client.stream(
                        "POST",
                        self.base_url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "HTTP-Referer": "http://localhost:8000",
                            "X-Title": "PlumoAI Digital Employee",
                            "Content-Type": "application/json",
                        },
                        json=request_body,
                    ) as response:
                        if response.status_code == 429:
                            if _stream_attempt < _MAX_429_RETRIES:
                                logger.warning(
                                    "⚠️ OpenRouter stream 429 rate limited — waiting %.0fs then retrying (%d/%d)...",
                                    _RETRY_429_WAIT_S,
                                    _stream_attempt + 1,
                                    _MAX_429_RETRIES,
                                )
                                await asyncio.sleep(_RETRY_429_WAIT_S)
                                continue
                        response.raise_for_status()

                        async for line in response.aiter_lines():
                            if not line or line.startswith(":"):
                                continue

                            if line.startswith("data: "):
                                data_str = line[6:]  # Remove "data: " prefix

                                if data_str == "[DONE]":
                                    break

                                try:
                                    data = json.loads(data_str)
                                    if "choices" in data and len(data["choices"]) > 0:
                                        delta = data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        # Yield content directly from AI model - no manual chunking
                                        if content:
                                            yield content  # This is the raw chunk from the AI model
                                except json.JSONDecodeError:
                                    continue
                                except Exception as e:
                                    logger.error(f"❌ Error parsing stream chunk: {str(e)}")
                                    continue
                return  # success — exit retry loop

            except httpx.HTTPStatusError as e:
                logger.error(f"❌ OpenRouter streaming HTTP error: {e.response.status_code}")
                if e.response.status_code == 429 and _stream_attempt < _MAX_429_RETRIES:
                    logger.warning(
                        "⚠️ OpenRouter stream 429 (HTTPStatusError) — waiting %.0fs then retrying (%d/%d)...",
                        _RETRY_429_WAIT_S,
                        _stream_attempt + 1,
                        _MAX_429_RETRIES,
                    )
                    await asyncio.sleep(_RETRY_429_WAIT_S)
                    continue
                if e.response.status_code == 401:
                    logger.warning("   OpenRouter credential used for this request: %s", _credential_summary(self.api_key))
                return
            except Exception as e:
                logger.error(f"❌ OpenRouter streaming error: {str(e)}")
                return

