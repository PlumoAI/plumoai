"""
WhatsApp Business functions class for functions_wrapper plugin.

Each public @tool method is one WhatsApp Business Cloud API (Meta Graph API)
action exposed to the LLM (mirrors the structure of gmail_functions.py /
discord_functions.py). Private helpers handle HTTP, token refresh, and
response formatting.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"]:
  - credentials: {"access_token": ..., "refresh_token": ...}
  - metadata: {"waba_id": ..., "business_id": ..., "phone_number_id": ...}

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider —
all natural-language-to-tool-call reasoning (which action to call, how to fill
its parameters) is done by the outer ReAct brain (MCPAgentTool), the same way
it is done for every other functions_wrapper tool such as GmailFunctions.
`action_by_query` is a heuristic (non-LLM) catch-all for callers that hand off
a single free-text instruction instead of picking a specific tool.

https://developers.facebook.com/docs/whatsapp/cloud-api/reference
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v21.0"


class WhatsAppBusinessFunctions(ConnectedServiceToolAgent):
    """
    WhatsApp Business Cloud API tool functions. Each @tool method is one
    WhatsApp Business capability (messaging, media, templates, phone numbers,
    business profile, WABA, QR code links, analytics, and blocking).

    Access token is read from credentials["access_token"] (refreshed via the
    ConnectedServiceCredentialMixin on 401 using credentials["refresh_token"]
    through the platform's servicesCredentials/refresh endpoint).

    waba_id / business_id / phone_number_id are read from the connected
    service's metadata (service_credential["metadata"]) and used as sensible
    defaults so the LLM does not have to repeat them on every call, while
    still allowing an explicit override per-call.
    """

    TOOL_DESCRIPTION = (
        "WhatsApp Business: send text/media/template/interactive/location/contact messages, "
        "manage media, message templates, phone numbers, business profile, WABA subscriptions, "
        "QR code message links, blocked users, and analytics via the Meta Graph API."
    )

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        user_id: Optional[int] = None,
        company_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        app_config: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> None:
        super().__init__(
            token=token,
            company_id=company_id,
            user_id=user_id,
            app_config=app_config,
        )
        self.agent_id = agent_id or ""
        self._httpx_client: Optional[httpx.AsyncClient] = None
        self._current_query: str = ""
        self._step_results: List[Dict] = []

        metadata = self.service_credential.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata) if metadata else {}
            except json.JSONDecodeError:
                metadata = {}
        self._metadata: Dict[str, Any] = metadata if isinstance(metadata, dict) else {}

        self.waba_id: Optional[str] = self._str_or_none(self._metadata.get("waba_id"))
        self.business_id: Optional[str] = self._str_or_none(self._metadata.get("business_id"))
        self.phone_number_id: Optional[str] = self._str_or_none(self._metadata.get("phone_number_id"))

        self.graph_version = (
            self._metadata.get("graph_version")
            or self.credentials.get("graph_version")
            or os.getenv("WHATSAPP_GRAPH_API_VERSION")
            or DEFAULT_GRAPH_VERSION
        )

    @staticmethod
    def _str_or_none(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError(
            "WhatsAppBusinessFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool"
        )

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("WhatsAppBusinessFunctions: no access_token in credentials")
        if not self.phone_number_id:
            logger.warning("WhatsAppBusinessFunctions: no phone_number_id in connected service metadata")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("WhatsAppBusinessFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    async def _refresh_access_token(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def _wa_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        files: Optional[Any] = None,
        data: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Optional[Dict]:
        url = f"{GRAPH_API_BASE}/{self.graph_version}{path}" if path.startswith("/") else f"{GRAPH_API_BASE}/{self.graph_version}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())

        if files is not None or data is not None:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            r = await self._httpx_client.request(method, url, params=params, files=files, data=data, headers=headers)
        elif json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)

        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._wa_request(
                method, path, json_body=json_body, params=params, files=files, data=data, retry_401=False
            )

        if r.status_code >= 400:
            try:
                err = r.json()
                message = (err.get("error") or {}).get("message") or r.text
            except Exception:
                message = r.text
            logger.warning("WhatsApp API %s %s -> %s %s", method, path, r.status_code, (message or "")[:500])
            return {"_error": True, "status_code": r.status_code, "message": (message or "")[:500]}

        if r.status_code == 204 or not r.content:
            return {}

        try:
            data_out = r.json()
            snippet = json.dumps(data_out, default=str)
            if len(snippet) > 2000:
                snippet = snippet[:2000] + "... (truncated)"
            logger.info("WhatsApp API %s %s -> %s: %s", method, path, r.status_code, snippet)
            return data_out
        except Exception:
            return {}

    @staticmethod
    def _err(data: Optional[Dict], msg: str) -> Optional[Dict]:
        if data is None:
            return {"success": False, "response": msg}
        if isinstance(data, dict) and data.get("_error"):
            return {"success": False, "response": f"{msg}: {data.get('message')} (HTTP {data.get('status_code')})"}
        return None

    @staticmethod
    def _ok(response: str, **extra) -> Dict:
        return {"success": True, "response": response, **extra}

    @staticmethod
    def _strip_none(d: Dict) -> Dict:
        return {k: v for k, v in d.items() if v is not None}

    def _require_waba_id(self, waba_id: Optional[str]) -> Optional[str]:
        wid = waba_id or self.waba_id
        return wid

    @staticmethod
    def _to_wa_number(to: str) -> str:
        s = (to or "").strip()
        if s.startswith("+"):
            s = s[1:]
        return "".join(c for c in s if c.isdigit()) or s

    def _envelope(self, to: str, reply_to_message_id: Optional[str] = None) -> Dict[str, Any]:
        env: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._to_wa_number(to),
        }
        if reply_to_message_id:
            env["context"] = {"message_id": reply_to_message_id}
        return env

    async def _send(self, payload: Dict[str, Any]) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("POST", f"/{pnid}/messages", json_body=payload)
        return self._err(data, "Failed to send message") or self._ok(
            f"Message sent to {payload.get('to')} (id={((data or {}).get('messages') or [{}])[0].get('id')}).",
            result=data,
        )

    # ==================================================================
    # MESSAGES — sending
    # ==================================================================

    @tool(
        description="Send a plain text WhatsApp message to a phone number (must be within a customer-initiated 24h session, or use send_template_message otherwise).",
        params={
            "to": "Destination phone number in E.164 format, e.g. '+14155551234'.",
            "body": "Text content of the message (up to 4096 chars).",
            "preview_url": "Whether to render a link preview for URLs in the body. Default false.",
            "reply_to_message_id": "WhatsApp message ID to reply to (shows as a quoted reply).",
        },
    )
    async def send_text_message(
        self, to: str, body: str, preview_url: bool = False,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        payload = self._envelope(to, reply_to_message_id)
        payload["type"] = "text"
        payload["text"] = {"body": body, "preview_url": preview_url}
        return await self._send(payload)

    @tool(
        description=(
            "Send a pre-approved WhatsApp message template (required to start a new conversation "
            "or message a user outside the 24h customer-service window)."
        ),
        params={
            "to": "Destination phone number in E.164 format.",
            "template_name": "Name of the approved message template.",
            "language_code": "Template language/locale code, e.g. 'en_US'.",
            "components": (
                "Optional list of template component objects matching the WhatsApp template components "
                "spec, e.g. [{'type':'body','parameters':[{'type':'text','text':'John'}]}, "
                "{'type':'button','sub_type':'url','index':'0','parameters':[{'type':'text','text':'abc123'}]}]."
            ),
        },
    )
    async def send_template_message(
        self, to: str, template_name: str, language_code: str = "en_US",
        components: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        template: Dict[str, Any] = {"name": template_name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        payload = self._envelope(to)
        payload["type"] = "template"
        payload["template"] = template
        return await self._send(payload)

    @tool(
        description="Send an image message via a public URL or a previously uploaded media_id.",
        params={
            "to": "Destination phone number in E.164 format.",
            "image_url": "Publicly reachable URL of the image. Provide this or media_id.",
            "media_id": "ID of an image previously uploaded via upload_media. Provide this or image_url.",
            "caption": "Optional caption text for the image.",
            "reply_to_message_id": "WhatsApp message ID to reply to.",
        },
    )
    async def send_image_message(
        self, to: str, image_url: Optional[str] = None, media_id: Optional[str] = None,
        caption: Optional[str] = None, reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        if not image_url and not media_id:
            return {"success": False, "response": "Provide either image_url or media_id."}
        image: Dict[str, Any] = {"link": image_url} if image_url else {"id": media_id}
        if caption:
            image["caption"] = caption
        payload = self._envelope(to, reply_to_message_id)
        payload["type"] = "image"
        payload["image"] = image
        return await self._send(payload)

    @tool(
        description="Send a video message via a public URL or a previously uploaded media_id.",
        params={
            "to": "Destination phone number in E.164 format.",
            "video_url": "Publicly reachable URL of the video. Provide this or media_id.",
            "media_id": "ID of a video previously uploaded via upload_media. Provide this or video_url.",
            "caption": "Optional caption text for the video.",
            "reply_to_message_id": "WhatsApp message ID to reply to.",
        },
    )
    async def send_video_message(
        self, to: str, video_url: Optional[str] = None, media_id: Optional[str] = None,
        caption: Optional[str] = None, reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        if not video_url and not media_id:
            return {"success": False, "response": "Provide either video_url or media_id."}
        video: Dict[str, Any] = {"link": video_url} if video_url else {"id": media_id}
        if caption:
            video["caption"] = caption
        payload = self._envelope(to, reply_to_message_id)
        payload["type"] = "video"
        payload["video"] = video
        return await self._send(payload)

    @tool(
        description="Send an audio/voice message via a public URL or a previously uploaded media_id.",
        params={
            "to": "Destination phone number in E.164 format.",
            "audio_url": "Publicly reachable URL of the audio file. Provide this or media_id.",
            "media_id": "ID of an audio file previously uploaded via upload_media. Provide this or audio_url.",
            "reply_to_message_id": "WhatsApp message ID to reply to.",
        },
    )
    async def send_audio_message(
        self, to: str, audio_url: Optional[str] = None, media_id: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        if not audio_url and not media_id:
            return {"success": False, "response": "Provide either audio_url or media_id."}
        audio: Dict[str, Any] = {"link": audio_url} if audio_url else {"id": media_id}
        payload = self._envelope(to, reply_to_message_id)
        payload["type"] = "audio"
        payload["audio"] = audio
        return await self._send(payload)

    @tool(
        description="Send a document (PDF, DOCX, etc.) via a public URL or a previously uploaded media_id.",
        params={
            "to": "Destination phone number in E.164 format.",
            "document_url": "Publicly reachable URL of the document. Provide this or media_id.",
            "media_id": "ID of a document previously uploaded via upload_media. Provide this or document_url.",
            "caption": "Optional caption text for the document.",
            "filename": "Filename to display to the recipient, e.g. 'invoice.pdf'.",
            "reply_to_message_id": "WhatsApp message ID to reply to.",
        },
    )
    async def send_document_message(
        self, to: str, document_url: Optional[str] = None, media_id: Optional[str] = None,
        caption: Optional[str] = None, filename: Optional[str] = None,
        reply_to_message_id: Optional[str] = None,
    ) -> Dict:
        if not document_url and not media_id:
            return {"success": False, "response": "Provide either document_url or media_id."}
        document: Dict[str, Any] = {"link": document_url} if document_url else {"id": media_id}
        if caption:
            document["caption"] = caption
        if filename:
            document["filename"] = filename
        payload = self._envelope(to, reply_to_message_id)
        payload["type"] = "document"
        payload["document"] = document
        return await self._send(payload)

    @tool(
        description="Send a sticker (static WebP or animated) via a public URL or a previously uploaded media_id.",
        params={
            "to": "Destination phone number in E.164 format.",
            "sticker_url": "Publicly reachable URL of the WebP sticker. Provide this or media_id.",
            "media_id": "ID of a sticker previously uploaded via upload_media. Provide this or sticker_url.",
        },
    )
    async def send_sticker_message(
        self, to: str, sticker_url: Optional[str] = None, media_id: Optional[str] = None,
    ) -> Dict:
        if not sticker_url and not media_id:
            return {"success": False, "response": "Provide either sticker_url or media_id."}
        sticker: Dict[str, Any] = {"link": sticker_url} if sticker_url else {"id": media_id}
        payload = self._envelope(to)
        payload["type"] = "sticker"
        payload["sticker"] = sticker
        return await self._send(payload)

    @tool(
        description="Send a location pin message.",
        params={
            "to": "Destination phone number in E.164 format.",
            "latitude": "Latitude of the location.",
            "longitude": "Longitude of the location.",
            "name": "Optional name/label for the location, e.g. 'HQ Office'.",
            "address": "Optional street address for the location.",
        },
    )
    async def send_location_message(
        self, to: str, latitude: float, longitude: float,
        name: Optional[str] = None, address: Optional[str] = None,
    ) -> Dict:
        location = self._strip_none({"latitude": latitude, "longitude": longitude, "name": name, "address": address})
        payload = self._envelope(to)
        payload["type"] = "location"
        payload["location"] = location
        return await self._send(payload)

    @tool(
        description=(
            "Send one or more contact cards. Each contact object follows the WhatsApp contacts "
            "spec, e.g. [{'name':{'formatted_name':'Jane Doe','first_name':'Jane'},"
            "'phones':[{'phone':'+14155551234','type':'CELL'}]}]."
        ),
        params={
            "to": "Destination phone number in E.164 format.",
            "contacts": "List of contact objects to send.",
        },
    )
    async def send_contact_message(
        self, to: str, contacts: List[Dict[str, Any]],
    ) -> Dict:
        payload = self._envelope(to)
        payload["type"] = "contacts"
        payload["contacts"] = contacts
        return await self._send(payload)

    @tool(
        description="Send an interactive message with up to 3 quick-reply buttons.",
        params={
            "to": "Destination phone number in E.164 format.",
            "body_text": "Main message text shown above the buttons.",
            "buttons": "List of up to 3 button objects: [{'id':'yes','title':'Yes'}, {'id':'no','title':'No'}].",
            "header_text": "Optional header text shown above the body.",
            "footer_text": "Optional footer text shown below the buttons.",
        },
    )
    async def send_interactive_button_message(
        self, to: str, body_text: str, buttons: List[Dict[str, str]],
        header_text: Optional[str] = None, footer_text: Optional[str] = None,
    ) -> Dict:
        if not buttons or len(buttons) > 3:
            return {"success": False, "response": "Provide 1–3 buttons."}
        interactive: Dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b.get("id"), "title": b.get("title")}}
                    for b in buttons
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        payload = self._envelope(to)
        payload["type"] = "interactive"
        payload["interactive"] = interactive
        return await self._send(payload)

    @tool(
        description=(
            "Send an interactive list message (a single button that opens a scrollable list of "
            "selectable options, grouped into sections)."
        ),
        params={
            "to": "Destination phone number in E.164 format.",
            "body_text": "Main message text shown above the list button.",
            "button_text": "Text on the button that opens the list, e.g. 'Choose an option'.",
            "sections": (
                "List of section objects matching the WhatsApp interactive list spec, e.g. "
                "[{'title':'Support','rows':[{'id':'billing','title':'Billing','description':'Billing questions'}]}]."
            ),
            "header_text": "Optional header text shown above the body.",
            "footer_text": "Optional footer text shown below the body.",
        },
    )
    async def send_interactive_list_message(
        self, to: str, body_text: str, button_text: str, sections: List[Dict[str, Any]],
        header_text: Optional[str] = None, footer_text: Optional[str] = None,
    ) -> Dict:
        interactive: Dict[str, Any] = {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_text, "sections": sections},
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        payload = self._envelope(to)
        payload["type"] = "interactive"
        payload["interactive"] = interactive
        return await self._send(payload)

    @tool(
        description="React to a previously received/sent message with an emoji.",
        params={
            "to": "Destination phone number in E.164 format (the chat to react in).",
            "message_id": "WhatsApp message ID to react to.",
            "emoji": "A single emoji character, e.g. '👍'. Pass an empty string to remove a reaction.",
        },
    )
    async def send_reaction(
        self, to: str, message_id: str, emoji: str = "",
    ) -> Dict:
        payload = self._envelope(to)
        payload["type"] = "reaction"
        payload["reaction"] = {"message_id": message_id, "emoji": emoji}
        return await self._send(payload)

    @tool(
        description="Mark an inbound message as read (shows blue double-check to the sender), optionally with the typing indicator shown while composing a reply.",
        params={
            "message_id": "WhatsApp message ID to mark as read.",
            "show_typing_indicator": "Whether to also show the typing indicator to the sender. Default false.",
        },
    )
    async def mark_message_as_read(
        self, message_id: str, show_typing_indicator: bool = False,
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body: Dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if show_typing_indicator:
            body["typing_indicator"] = {"type": "text"}
        data = await self._wa_request("POST", f"/{pnid}/messages", json_body=body)
        return self._err(data, "Failed to mark message as read") or self._ok("Message marked as read.", result=data)

    # ==================================================================
    # MEDIA
    # ==================================================================

    @tool(
        description="Upload media (image, video, audio, document, or sticker) from a public URL for later reuse via media_id in send_* tools.",
        params={
            "media_url": "Publicly reachable URL to download the media from.",
            "mime_type": "MIME type of the media, e.g. 'image/jpeg', 'application/pdf'.",
        },
    )
    async def upload_media(
        self, media_url: str, mime_type: str,
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        try:
            async with httpx.AsyncClient(timeout=60.0) as hc:
                dl = await hc.get(media_url)
            dl.raise_for_status()
            content = dl.content
        except Exception as e:
            return {"success": False, "response": f"Failed to download media_url: {e}"}
        files = {"file": ("upload", content, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}
        result = await self._wa_request("POST", f"/{pnid}/media", files=files, data=data)
        return self._err(result, "Failed to upload media") or self._ok(
            f"Media uploaded (id={(result or {}).get('id')}).", media=result
        )

    @tool(
        description="Get the temporary download URL and metadata for a previously uploaded or received media_id.",
        params={"media_id": "The ID of the media object."},
    )
    async def get_media_url(self, media_id: str) -> Dict:
        data = await self._wa_request("GET", f"/{media_id}")
        return self._err(data, "Failed to get media URL") or self._ok("Media info retrieved.", media=data)

    @tool(
        description="Download the bytes of a media object (base64-encoded) and its mime type.",
        params={"media_id": "The ID of the media object to download."},
    )
    async def download_media(self, media_id: str) -> Dict:
        info = await self._wa_request("GET", f"/{media_id}")
        err = self._err(info, "Failed to get media URL")
        if err:
            return err
        url = (info or {}).get("url")
        if not url:
            return {"success": False, "response": "Media object has no download URL."}
        try:
            async with httpx.AsyncClient(timeout=60.0) as hc:
                r = await hc.get(url, headers={"Authorization": f"Bearer {self.access_token}"})
            r.raise_for_status()
        except Exception as e:
            return {"success": False, "response": f"Failed to download media content: {e}"}
        b64 = base64.b64encode(r.content).decode()
        mime_type = (info or {}).get("mime_type") or r.headers.get("content-type", "application/octet-stream")
        return self._ok(
            f"Media downloaded ({len(r.content)} bytes).",
            media_id=media_id, mime_type=mime_type, size=len(r.content), content_base64=b64,
        )

    @tool(
        description="Delete an uploaded media object by ID.",
        params={"media_id": "The ID of the media object to delete."},
    )
    async def delete_media(self, media_id: str) -> Dict:
        data = await self._wa_request("DELETE", f"/{media_id}")
        return self._err(data, "Failed to delete media") or self._ok(f"Media {media_id} deleted.")

    # ==================================================================
    # MESSAGE TEMPLATES
    # ==================================================================

    @tool(
        description="List message templates for the WhatsApp Business Account (WABA), optionally filtered by name or status.",
        params={
            "name": "Filter templates by exact name.",
            "status": "Filter by status: APPROVED, PENDING, REJECTED, PAUSED, DISABLED.",
            "limit": "Max templates to return (default 25).",
            "waba_id": "WABA ID. Defaults to this connection's configured waba_id.",
        },
    )
    async def list_message_templates(
        self, name: Optional[str] = None, status: Optional[str] = None,
        limit: int = 25, waba_id: Optional[str] = None,
    ) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 25), 200))}
        if name:
            params["name"] = name
        if status:
            params["status"] = status
        data = await self._wa_request("GET", f"/{wid}/message_templates", params=params)
        templates = (data or {}).get("data") or []
        return self._err(data, "Failed to list message templates") or self._ok(
            f"Found {len(templates)} template(s).", templates=templates, count=len(templates)
        )

    @tool(
        description="Get details about a specific message template by its template ID.",
        params={"template_id": "The ID of the message template."},
    )
    async def get_message_template(self, template_id: str) -> Dict:
        data = await self._wa_request("GET", f"/{template_id}")
        return self._err(data, "Failed to get message template") or self._ok(
            f"Template '{(data or {}).get('name')}'.", template=data
        )

    @tool(
        description=(
            "Create a new message template for review/approval by Meta. Components follow the "
            "WhatsApp template components spec, e.g. [{'type':'BODY','text':'Hello {{1}}, your "
            "order {{2}} has shipped.'}, {'type':'BUTTONS','buttons':[{'type':'QUICK_REPLY','text':'Track order'}]}]."
        ),
        params={
            "name": "Template name (lowercase, underscores, unique per WABA/language).",
            "category": "Template category: AUTHENTICATION, MARKETING, or UTILITY.",
            "language_code": "Template language/locale code, e.g. 'en_US'.",
            "components": "List of component objects (HEADER, BODY, FOOTER, BUTTONS).",
            "waba_id": "WABA ID. Defaults to this connection's configured waba_id.",
        },
    )
    async def create_message_template(
        self, name: str, category: str, language_code: str,
        components: List[Dict[str, Any]], waba_id: Optional[str] = None,
    ) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        body = {"name": name, "category": category, "language": language_code, "components": components}
        data = await self._wa_request("POST", f"/{wid}/message_templates", json_body=body)
        return self._err(data, "Failed to create message template") or self._ok(
            f"Template '{name}' submitted for review (id={(data or {}).get('id')}).", template=data
        )

    @tool(
        description="Update the category and/or components of an existing message template (re-submits it for review).",
        params={
            "template_id": "The ID of the message template to update.",
            "category": "New category: AUTHENTICATION, MARKETING, or UTILITY.",
            "components": "New list of component objects (HEADER, BODY, FOOTER, BUTTONS).",
        },
    )
    async def update_message_template(
        self, template_id: str, category: Optional[str] = None,
        components: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        body = self._strip_none({"category": category, "components": components})
        if not body:
            return {"success": False, "response": "Provide category and/or components to update."}
        data = await self._wa_request("POST", f"/{template_id}", json_body=body)
        return self._err(data, "Failed to update message template") or self._ok("Template updated.", result=data)

    @tool(
        description="Delete a message template by name (deletes all language variants) or by a specific template ID.",
        params={
            "name": "Name of the template to delete (deletes all its language variants). Provide this or template_id.",
            "template_id": "Specific template ID to delete a single language variant. Provide this or name.",
            "waba_id": "WABA ID (required when deleting by name). Defaults to this connection's configured waba_id.",
        },
    )
    async def delete_message_template(
        self, name: Optional[str] = None, template_id: Optional[str] = None, waba_id: Optional[str] = None,
    ) -> Dict:
        if not name and not template_id:
            return {"success": False, "response": "Provide either name or template_id."}
        if name:
            wid = self._require_waba_id(waba_id)
            if not wid:
                return {"success": False, "response": "No waba_id available."}
            params: Dict[str, Any] = {"name": name}
            if template_id:
                params["hsm_id"] = template_id
            data = await self._wa_request("DELETE", f"/{wid}/message_templates", params=params)
        else:
            data = await self._wa_request("DELETE", f"/{template_id}")
        return self._err(data, "Failed to delete message template") or self._ok("Template deleted.")

    # ==================================================================
    # PHONE NUMBERS
    # ==================================================================

    @tool(
        description="Get details about this connection's WhatsApp Business phone number (display name, quality rating, verified name, status).",
        params={},
    )
    async def get_phone_number_info(self) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        params = {"fields": "display_phone_number,verified_name,quality_rating,code_verification_status,platform_type,throughput"}
        data = await self._wa_request("GET", f"/{pnid}", params=params)
        return self._err(data, "Failed to get phone number info") or self._ok(
            f"Phone number '{(data or {}).get('display_phone_number')}'.", phone_number=data
        )

    @tool(
        description="List all phone numbers registered under a WhatsApp Business Account (WABA).",
        params={"waba_id": "WABA ID. Defaults to this connection's configured waba_id."},
    )
    async def list_phone_numbers(self, waba_id: Optional[str] = None) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        data = await self._wa_request("GET", f"/{wid}/phone_numbers")
        numbers = (data or {}).get("data") or []
        return self._err(data, "Failed to list phone numbers") or self._ok(
            f"Found {len(numbers)} phone number(s).", phone_numbers=numbers, count=len(numbers)
        )

    @tool(
        description="Register this connection's phone number for use with Cloud API (required once per phone number before sending messages).",
        params={"pin": "6-digit two-step verification PIN for the phone number."},
    )
    async def register_phone_number(self, pin: str) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body = {"messaging_product": "whatsapp", "pin": pin}
        data = await self._wa_request("POST", f"/{pnid}/register", json_body=body)
        return self._err(data, "Failed to register phone number") or self._ok("Phone number registered.", result=data)

    @tool(
        description="Deregister this connection's phone number from Cloud API (stops it from sending/receiving messages until re-registered).",
        params={},
    )
    async def deregister_phone_number(self) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("POST", f"/{pnid}/deregister", json_body={})
        return self._err(data, "Failed to deregister phone number") or self._ok("Phone number deregistered.")

    @tool(
        description="Request an SMS or voice verification code to complete this connection's phone number registration.",
        params={
            "code_method": "'SMS' or 'VOICE'.",
            "language": "Language code for the voice call, e.g. 'en_US'. Only used for VOICE.",
        },
    )
    async def request_verification_code(
        self, code_method: str = "SMS", language: str = "en_US",
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body = {"code_method": code_method, "language": language}
        data = await self._wa_request("POST", f"/{pnid}/request_code", json_body=body)
        return self._err(data, "Failed to request verification code") or self._ok(
            f"Verification code requested via {code_method}.", result=data
        )

    @tool(
        description="Verify this connection's phone number using the code received via request_verification_code.",
        params={"code": "The verification code received."},
    )
    async def verify_code(self, code: str) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("POST", f"/{pnid}/verify_code", json_body={"code": code})
        return self._err(data, "Failed to verify code") or self._ok("Phone number verified.", result=data)

    # ==================================================================
    # BUSINESS PROFILE
    # ==================================================================

    @tool(
        description="Get this connection's WhatsApp Business profile (about text, address, description, email, profile picture, industry, websites).",
        params={},
    )
    async def get_business_profile(self) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        params = {"fields": "about,address,description,email,profile_picture_url,websites,vertical"}
        data = await self._wa_request("GET", f"/{pnid}/whatsapp_business_profile", params=params)
        profile = ((data or {}).get("data") or [{}])[0] if isinstance((data or {}).get("data"), list) else data
        return self._err(data, "Failed to get business profile") or self._ok("Business profile retrieved.", profile=profile)

    @tool(
        description="Update this connection's WhatsApp Business profile fields.",
        params={
            "about": "Short 'About' status text (up to 139 chars).",
            "address": "Business address.",
            "description": "Business description.",
            "email": "Business contact email.",
            "profile_picture_url": "Publicly reachable URL of a new profile picture.",
            "vertical": "Business industry, e.g. 'RETAIL', 'HOTEL', 'PROF_SERVICES'.",
            "websites": "List of up to 2 website URLs.",
        },
    )
    async def update_business_profile(
        self, about: Optional[str] = None, address: Optional[str] = None,
        description: Optional[str] = None, email: Optional[str] = None,
        profile_picture_url: Optional[str] = None, vertical: Optional[str] = None,
        websites: Optional[List[str]] = None,
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body = self._strip_none({
            "about": about, "address": address, "description": description, "email": email,
            "profile_picture_url": profile_picture_url, "vertical": vertical, "websites": websites,
        })
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        body["messaging_product"] = "whatsapp"
        data = await self._wa_request("POST", f"/{pnid}/whatsapp_business_profile", json_body=body)
        return self._err(data, "Failed to update business profile") or self._ok("Business profile updated.", result=data)

    # ==================================================================
    # WHATSAPP BUSINESS ACCOUNT (WABA)
    # ==================================================================

    @tool(
        description="Get details about the WhatsApp Business Account (WABA) itself (name, currency, timezone, message template namespace).",
        params={"waba_id": "WABA ID. Defaults to this connection's configured waba_id."},
    )
    async def get_waba_info(self, waba_id: Optional[str] = None) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        params = {"fields": "name,currency,timezone_id,message_template_namespace,business_verification_status"}
        data = await self._wa_request("GET", f"/{wid}", params=params)
        return self._err(data, "Failed to get WABA info") or self._ok(f"WABA '{(data or {}).get('name')}'.", waba=data)

    @tool(
        description="List the Meta apps subscribed to receive webhook events for this WABA.",
        params={"waba_id": "WABA ID. Defaults to this connection's configured waba_id."},
    )
    async def list_subscribed_apps(self, waba_id: Optional[str] = None) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        data = await self._wa_request("GET", f"/{wid}/subscribed_apps")
        apps = (data or {}).get("data") or []
        return self._err(data, "Failed to list subscribed apps") or self._ok(
            f"Found {len(apps)} subscribed app(s).", apps=apps, count=len(apps)
        )

    @tool(
        description="Subscribe the app associated with this connection's access token to receive webhook events for this WABA.",
        params={"waba_id": "WABA ID. Defaults to this connection's configured waba_id."},
    )
    async def subscribe_waba_app(self, waba_id: Optional[str] = None) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        data = await self._wa_request("POST", f"/{wid}/subscribed_apps", json_body={})
        return self._err(data, "Failed to subscribe app") or self._ok("App subscribed to WABA webhooks.", result=data)

    @tool(
        description="Unsubscribe the app associated with this connection's access token from webhook events for this WABA.",
        params={"waba_id": "WABA ID. Defaults to this connection's configured waba_id."},
    )
    async def unsubscribe_waba_app(self, waba_id: Optional[str] = None) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        data = await self._wa_request("DELETE", f"/{wid}/subscribed_apps")
        return self._err(data, "Failed to unsubscribe app") or self._ok("App unsubscribed from WABA webhooks.")

    # ==================================================================
    # QR CODE MESSAGE LINKS
    # ==================================================================

    @tool(
        description="Create a shareable wa.me link (optionally with a QR code image) that opens a chat with a prefilled message.",
        params={
            "prefilled_message": "Text that will be prefilled in the chat when the link/QR code is opened.",
            "generate_qr_image": "Image format to also generate a QR code image: 'PNG' or 'SVG'. Omit for none.",
        },
    )
    async def create_qr_code_message_link(
        self, prefilled_message: str, generate_qr_image: Optional[str] = None,
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        params: Dict[str, Any] = {"prefilled_message": prefilled_message}
        if generate_qr_image:
            params["generate_qr_image"] = generate_qr_image
        data = await self._wa_request("POST", f"/{pnid}/message_qrdls", params=params)
        return self._err(data, "Failed to create QR code message link") or self._ok(
            "QR code message link created.", link=data
        )

    @tool(
        description="List all QR code message links for this connection's phone number.",
        params={},
    )
    async def list_qr_code_message_links(self) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("GET", f"/{pnid}/message_qrdls")
        links = (data or {}).get("data") or []
        return self._err(data, "Failed to list QR code message links") or self._ok(
            f"Found {len(links)} QR code link(s).", links=links, count=len(links)
        )

    @tool(
        description="Get a specific QR code message link by its code.",
        params={"code": "The QR code link's short code (returned by create_qr_code_message_link)."},
    )
    async def get_qr_code_message_link(self, code: str) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("GET", f"/{pnid}/message_qrdls/{code}")
        return self._err(data, "Failed to get QR code message link") or self._ok("QR code link retrieved.", link=data)

    @tool(
        description="Update the prefilled message of an existing QR code message link.",
        params={
            "code": "The QR code link's short code.",
            "prefilled_message": "New prefilled message text.",
        },
    )
    async def update_qr_code_message_link(
        self, code: str, prefilled_message: str,
    ) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        params = {"prefilled_message": prefilled_message}
        data = await self._wa_request("POST", f"/{pnid}/message_qrdls/{code}", params=params)
        return self._err(data, "Failed to update QR code message link") or self._ok("QR code link updated.", link=data)

    @tool(
        description="Delete a QR code message link by its code.",
        params={"code": "The QR code link's short code to delete."},
    )
    async def delete_qr_code_message_link(self, code: str) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("DELETE", f"/{pnid}/message_qrdls/{code}")
        return self._err(data, "Failed to delete QR code message link") or self._ok(f"QR code link '{code}' deleted.")

    # ==================================================================
    # BLOCKED USERS
    # ==================================================================

    @tool(
        description="Block one or more phone numbers from messaging this WhatsApp Business phone number.",
        params={"phone_numbers": "List of phone numbers (E.164 format) to block."},
    )
    async def block_users(self, phone_numbers: List[str]) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body = {
            "messaging_product": "whatsapp",
            "block_users": [{"user": self._to_wa_number(p)} for p in phone_numbers],
        }
        data = await self._wa_request("POST", f"/{pnid}/block_users", json_body=body)
        return self._err(data, "Failed to block users") or self._ok(f"Blocked {len(phone_numbers)} user(s).", result=data)

    @tool(
        description="Unblock one or more previously blocked phone numbers.",
        params={"phone_numbers": "List of phone numbers (E.164 format) to unblock."},
    )
    async def unblock_users(self, phone_numbers: List[str]) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        body = {
            "messaging_product": "whatsapp",
            "block_users": [{"user": self._to_wa_number(p)} for p in phone_numbers],
        }
        data = await self._wa_request("DELETE", f"/{pnid}/block_users", json_body=body)
        return self._err(data, "Failed to unblock users") or self._ok(f"Unblocked {len(phone_numbers)} user(s).", result=data)

    @tool(
        description="List phone numbers currently blocked on this WhatsApp Business phone number.",
        params={},
    )
    async def list_blocked_users(self) -> Dict:
        pnid = self.phone_number_id
        if not pnid:
            return {"success": False, "response": "No phone_number_id configured on this connection."}
        data = await self._wa_request("GET", f"/{pnid}/block_users")
        blocked = (data or {}).get("data") or []
        return self._err(data, "Failed to list blocked users") or self._ok(
            f"Found {len(blocked)} blocked user(s).", blocked_users=blocked, count=len(blocked)
        )

    # ==================================================================
    # ANALYTICS
    # ==================================================================

    @tool(
        description="Get message volume analytics for the WABA over a time range (sent/delivered counts by direction and message type).",
        params={
            "start": "Start of range as a Unix timestamp (seconds).",
            "end": "End of range as a Unix timestamp (seconds).",
            "granularity": "Bucket size: 'HALF_HOUR', 'DAY', or 'MONTH'.",
            "waba_id": "WABA ID. Defaults to this connection's configured waba_id.",
        },
    )
    async def get_message_analytics(
        self, start: int, end: int, granularity: str = "DAY", waba_id: Optional[str] = None,
    ) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        fields = f"analytics.start({start}).end({end}).granularity({granularity})"
        data = await self._wa_request("GET", f"/{wid}", params={"fields": fields})
        return self._err(data, "Failed to get message analytics") or self._ok(
            "Message analytics retrieved.", analytics=(data or {}).get("analytics")
        )

    @tool(
        description="Get conversation-based billing analytics for the WABA over a time range (conversation counts and categories).",
        params={
            "start": "Start of range as a Unix timestamp (seconds).",
            "end": "End of range as a Unix timestamp (seconds).",
            "granularity": "Bucket size: 'HALF_HOUR', 'DAILY', or 'MONTHLY'.",
            "phone_number_ids": "Optional list of phone number IDs to filter to. Defaults to all numbers on the WABA.",
            "waba_id": "WABA ID. Defaults to this connection's configured waba_id.",
        },
    )
    async def get_conversation_analytics(
        self, start: int, end: int, granularity: str = "DAILY",
        phone_number_ids: Optional[List[str]] = None, waba_id: Optional[str] = None,
    ) -> Dict:
        wid = self._require_waba_id(waba_id)
        if not wid:
            return {"success": False, "response": "No waba_id available."}
        fields = f"conversation_analytics.start({start}).end({end}).granularity({granularity})"
        if phone_number_ids:
            ids = ",".join(f"'{p}'" for p in phone_number_ids)
            fields += f".phone_numbers([{ids}])"
        data = await self._wa_request("GET", f"/{wid}", params={"fields": fields})
        return self._err(data, "Failed to get conversation analytics") or self._ok(
            "Conversation analytics retrieved.", analytics=(data or {}).get("conversation_analytics")
        )

    # ==================================================================
    # HEURISTIC CATCH-ALL
    # ==================================================================

    @tool(
        description=(
            "Heuristic catch-all: given one free-text instruction plus whichever of the optional "
            "parameters are known, infer and execute the single most likely WhatsApp Business "
            "action (send a text message, send a template, mark as read, etc.). Prefer calling a "
            "specific tool directly when the desired action is already clear."
        ),
        params={
            "query": "Free-text instruction describing what to do, e.g. 'send hello to +14155551234'.",
            "to": "Destination phone number, if known.",
            "body": "Message text, if known.",
            "message_id": "A relevant WhatsApp message ID, if known (for reply/react/mark-as-read).",
            "template_name": "A relevant template name, if known.",
            "language_code": "A relevant template language code, if known.",
        },
    )
    async def action_by_query(
        self, query: str, to: Optional[str] = None, body: Optional[str] = None,
        message_id: Optional[str] = None, template_name: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> Dict:
        q = (query or "").lower()
        if not to:
            m = re.search(r"\+?\d[\d\s\-()]{6,}\d", query or "")
            if m:
                to = m.group(0)

        if template_name or "template" in q:
            if not to or not template_name:
                return {"success": False, "response": "A template action needs 'to' and 'template_name'."}
            return await self.send_template_message(to=to, template_name=template_name, language_code=language_code or "en_US")

        if "mark" in q and "read" in q:
            if not message_id:
                return {"success": False, "response": "Marking as read needs 'message_id'."}
            return await self.mark_message_as_read(message_id=message_id)

        if ("react" in q or "reaction" in q) and message_id:
            m = re.search(r"[\U0001F300-\U0001FAFF☀-➿]", query or "")
            if not to:
                return {"success": False, "response": "Reacting needs 'to'."}
            return await self.send_reaction(to=to, message_id=message_id, emoji=m.group(0) if m else "👍")

        if "send" in q or body or to:
            if not to or not body:
                return {"success": False, "response": "Sending a message needs 'to' and 'body'."}
            return await self.send_text_message(to=to, body=body, reply_to_message_id=message_id)

        return {"success": False, "response": f"Could not infer a WhatsApp Business action from: {query!r}"}
