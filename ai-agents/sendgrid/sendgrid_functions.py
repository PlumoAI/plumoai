"""
SendGrid functions class for functions_wrapper plugin.

Each public @tool method is one SendGrid Web API v3 action exposed to the LLM
(mirrors the structure of whatsapp_business_functions.py / calcom_functions.py).
Private helpers handle HTTP, token refresh, and response formatting.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"]:
  - credentials: {"access_token": <SendGrid API key>}
  - metadata: {"default_from_email": ..., "default_from_name": ...}

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider --
all natural-language-to-tool-call reasoning (which action to call, how to fill
its parameters) is done by the outer ReAct brain (MCPAgentTool), the same way
it is done for every other functions_wrapper tool such as GmailFunctions.
`action_by_query` is a heuristic (non-LLM) catch-all for callers that hand off
a single free-text instruction instead of picking a specific tool.

https://www.twilio.com/docs/sendgrid/api-reference
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

SENDGRID_API_BASE = "https://api.sendgrid.com/v3"


class SendGridFunctions(ConnectedServiceToolAgent):
    """
    SendGrid Web API v3 tool functions. Each @tool method is one SendGrid
    capability (mail send, verified senders, templates, marketing contact
    lists/contacts, suppression management, and activity stats).

    Access token (the SendGrid API key) is read from credentials["access_token"].
    SendGrid API keys are static -- they do not expire/rotate via an OAuth
    refresh flow -- so _refresh_access_token() is wired for parity with other
    connected-service tools but will simply return False if the platform has
    no rotation configured for this connection.

    default_from_email / default_from_name are read from the connected
    service's metadata (service_credential["metadata"]) and used as sensible
    defaults so the LLM does not have to repeat them on every send, while
    still allowing an explicit override per-call.
    """

    TOOL_DESCRIPTION = (
        "SendGrid: send plain-text/HTML and dynamic-template emails, validate email addresses, "
        "manage verified senders, templates and template versions, marketing contact lists and "
        "contacts, suppression lists (bounces, blocks, invalid emails, spam reports, global "
        "unsubscribes), and view email activity stats via the SendGrid Web API v3."
    )

    _SUPPRESSION_PATHS = {
        "bounces": "/suppression/bounces",
        "blocks": "/suppression/blocks",
        "invalid_emails": "/suppression/invalid_emails",
        "spam_reports": "/suppression/spam_reports",
        "global_unsubscribes": "/asm/suppressions/global",
    }

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

        metadata = self.service_credential.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata) if metadata else {}
            except json.JSONDecodeError:
                metadata = {}
        self._metadata: Dict[str, Any] = metadata if isinstance(metadata, dict) else {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError(
            "SendGridFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool"
        )

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("SendGridFunctions: no access_token (API key) in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("SendGridFunctions initialized")

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

    async def _sg_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Tuple[Any, Optional[httpx.Response]]:
        url = f"{SENDGRID_API_BASE}{path}" if path.startswith("/") else f"{SENDGRID_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())

        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)

        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._sg_request(method, path, json_body=json_body, params=params, retry_401=False)

        if r.status_code >= 400:
            try:
                err = r.json()
                errors = err.get("errors") if isinstance(err, dict) else None
                message = "; ".join(e.get("message", "") for e in errors) if errors else r.text
            except Exception:
                message = r.text
            logger.warning("SendGrid API %s %s -> %s %s", method, path, r.status_code, (message or "")[:500])
            return {"_error": True, "status_code": r.status_code, "message": (message or "")[:500]}, r

        if r.status_code in (202, 204) or not r.content:
            return {}, r

        try:
            data_out = r.json()
            snippet = json.dumps(data_out, default=str)
            if len(snippet) > 2000:
                snippet = snippet[:2000] + "... (truncated)"
            logger.info("SendGrid API %s %s -> %s: %s", method, path, r.status_code, snippet)
            return data_out, r
        except Exception:
            return {}, r

    @staticmethod
    def _err(data: Any, msg: str) -> Optional[Dict]:
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

    @staticmethod
    def _email_list(value: Optional[str]) -> List[Dict[str, str]]:
        return [{"email": e.strip()} for e in (value or "").split(",") if e.strip()]

    def _custom_args(self) -> Optional[Dict[str, str]]:
        cid = self.connected_service_id
        return {"plumo_connected_service_id": str(cid)} if cid else None

    # ==================================================================
    # MAIL SEND
    # ==================================================================

    @tool(
        description="Send a plain-text and/or HTML email via SendGrid.",
        params={
            "to": "Recipient email address, or comma-separated list of addresses.",
            "subject": "Email subject line.",
            "text_content": "Plain-text body. Provide this and/or html_content.",
            "html_content": "HTML body. Provide this and/or text_content.",
            "from_email": "Sender email address. Defaults to this connection's configured default_from_email.",
            "from_name": "Sender display name. Defaults to this connection's configured default_from_name.",
            "cc": "Comma-separated list of CC email addresses.",
            "bcc": "Comma-separated list of BCC email addresses.",
            "reply_to": "Reply-To email address.",
            "attachments": (
                "Optional list of attachment objects, e.g. "
                "[{'filename':'invoice.pdf','content_base64':'<base64>','type':'application/pdf'}]."
            ),
        },
    )
    async def send_email(
        self, to: str, subject: str, text_content: Optional[str] = None, html_content: Optional[str] = None,
        from_email: Optional[str] = None, from_name: Optional[str] = None,
        cc: Optional[str] = None, bcc: Optional[str] = None, reply_to: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        if not text_content and not html_content:
            return {"success": False, "response": "Provide text_content and/or html_content."}
        sender_email = from_email or self._metadata.get("default_from_email")
        if not sender_email:
            return {"success": False, "response": "No from_email provided and no default_from_email configured on this connection."}

        personalization: Dict[str, Any] = {"to": self._email_list(to), "subject": subject}
        if cc:
            personalization["cc"] = self._email_list(cc)
        if bcc:
            personalization["bcc"] = self._email_list(bcc)

        content = [c for c in [
            {"type": "text/plain", "value": text_content} if text_content else None,
            {"type": "text/html", "value": html_content} if html_content else None,
        ] if c]

        body: Dict[str, Any] = {
            "personalizations": [personalization],
            "from": self._strip_none({"email": sender_email, "name": from_name or self._metadata.get("default_from_name")}),
            "subject": subject,
            "content": content,
        }
        if reply_to:
            body["reply_to"] = {"email": reply_to}
        if attachments:
            body["attachments"] = [
                self._strip_none({
                    "content": a.get("content_base64"), "filename": a.get("filename"),
                    "type": a.get("type"), "disposition": a.get("disposition") or "attachment",
                })
                for a in attachments
            ]
        custom_args = self._custom_args()
        if custom_args:
            body["custom_args"] = custom_args

        data, resp = await self._sg_request("POST", "/mail/send", json_body=body)
        err = self._err(data, "Failed to send email")
        if err:
            return err
        message_id = resp.headers.get("X-Message-Id") if resp is not None else None
        return self._ok(f"Email sent to {to} (message_id={message_id}).", message_id=message_id)

    @tool(
        description="Send a transactional email using a pre-built SendGrid dynamic template.",
        params={
            "to": "Recipient email address, or comma-separated list of addresses.",
            "template_id": "SendGrid dynamic template ID, e.g. 'd-abc123...'.",
            "dynamic_template_data": (
                "Dict of substitution variables referenced by the template, "
                "e.g. {'first_name':'Jane','order_id':'123'}."
            ),
            "from_email": "Sender email address. Defaults to this connection's configured default_from_email.",
            "from_name": "Sender display name. Defaults to this connection's configured default_from_name.",
            "cc": "Comma-separated list of CC email addresses.",
            "bcc": "Comma-separated list of BCC email addresses.",
            "reply_to": "Reply-To email address.",
        },
    )
    async def send_template_email(
        self, to: str, template_id: str, dynamic_template_data: Optional[Dict[str, Any]] = None,
        from_email: Optional[str] = None, from_name: Optional[str] = None,
        cc: Optional[str] = None, bcc: Optional[str] = None, reply_to: Optional[str] = None,
    ) -> Dict:
        sender_email = from_email or self._metadata.get("default_from_email")
        if not sender_email:
            return {"success": False, "response": "No from_email provided and no default_from_email configured on this connection."}

        personalization: Dict[str, Any] = {"to": self._email_list(to)}
        if dynamic_template_data:
            personalization["dynamic_template_data"] = dynamic_template_data
        if cc:
            personalization["cc"] = self._email_list(cc)
        if bcc:
            personalization["bcc"] = self._email_list(bcc)

        body: Dict[str, Any] = {
            "personalizations": [personalization],
            "from": self._strip_none({"email": sender_email, "name": from_name or self._metadata.get("default_from_name")}),
            "template_id": template_id,
        }
        if reply_to:
            body["reply_to"] = {"email": reply_to}
        custom_args = self._custom_args()
        if custom_args:
            body["custom_args"] = custom_args

        data, resp = await self._sg_request("POST", "/mail/send", json_body=body)
        err = self._err(data, "Failed to send template email")
        if err:
            return err
        message_id = resp.headers.get("X-Message-Id") if resp is not None else None
        return self._ok(f"Template email sent to {to} (message_id={message_id}).", message_id=message_id)

    # ==================================================================
    # EMAIL VALIDATION
    # ==================================================================

    @tool(
        description=(
            "Validate a single email address using SendGrid's Email Address Validation API: "
            "returns a deliverability verdict (Valid/Risky/Invalid), a 0-1 score, syntax/MX-record/"
            "disposable/role-address checks, and a typo suggestion if one applies. Requires the "
            "Email Address Validation add-on to be enabled on this SendGrid account."
        ),
        params={
            "email": "The email address to validate.",
            "source": "Optional label identifying where this validation came from, e.g. 'signup_form'.",
        },
    )
    async def validate_email(self, email: str, source: Optional[str] = None) -> Dict:
        body = self._strip_none({"email": email, "source": source})
        data, _ = await self._sg_request("POST", "/validations/email", json_body=body)
        err = self._err(data, "Failed to validate email")
        if err:
            return err
        result = (data or {}).get("result") or {}
        verdict = result.get("verdict") or "unknown"
        return self._ok(f"'{email}' validation verdict: {verdict}.", validation=result)

    # ==================================================================
    # VERIFIED SENDERS
    # ==================================================================

    @tool(description="List verified sender identities on this SendGrid account.", params={})
    async def list_verified_senders(self) -> Dict:
        data, _ = await self._sg_request("GET", "/verified_senders")
        err = self._err(data, "Failed to list verified senders")
        if err:
            return err
        results = (data or {}).get("results") or []
        return self._ok(f"Found {len(results)} verified sender(s).", senders=results, count=len(results))

    @tool(
        description="Create a new verified sender identity (SendGrid emails the address a verification link before it can be used to send).",
        params={
            "nickname": "Internal label for this sender identity.",
            "from_email": "Email address to verify and send from.",
            "from_name": "Display name for this sender.",
            "reply_to": "Reply-To email address.",
            "address": "Street address (required by SendGrid/CAN-SPAM).",
            "city": "City.",
            "country": "Country.",
            "state": "State/province (optional).",
            "zip_code": "ZIP/postal code (optional).",
        },
    )
    async def create_verified_sender(
        self, nickname: str, from_email: str, from_name: str, reply_to: str,
        address: str, city: str, country: str, state: Optional[str] = None, zip_code: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({
            "nickname": nickname, "from_email": from_email, "from_name": from_name,
            "reply_to": reply_to, "address": address, "city": city, "country": country,
            "state": state, "zip": zip_code,
        })
        data, _ = await self._sg_request("POST", "/verified_senders", json_body=body)
        err = self._err(data, "Failed to create verified sender")
        if err:
            return err
        return self._ok(
            f"Verified sender '{from_email}' created (id={(data or {}).get('id')}); a verification email was sent.",
            sender=data,
        )

    @tool(description="Delete a verified sender identity by ID.", params={"sender_id": "ID of the verified sender to delete."})
    async def delete_verified_sender(self, sender_id: int) -> Dict:
        data, _ = await self._sg_request("DELETE", f"/verified_senders/{sender_id}")
        err = self._err(data, "Failed to delete verified sender")
        if err:
            return err
        return self._ok(f"Verified sender {sender_id} deleted.")

    # ==================================================================
    # TEMPLATES
    # ==================================================================

    @tool(
        description="List email templates on this account.",
        params={"generation": "Template generation to filter by: 'dynamic' (default) or 'legacy'."},
    )
    async def list_templates(self, generation: str = "dynamic") -> Dict:
        data, _ = await self._sg_request("GET", "/templates", params={"generations": generation, "page_size": 200})
        err = self._err(data, "Failed to list templates")
        if err:
            return err
        templates = (data or {}).get("templates") or []
        return self._ok(f"Found {len(templates)} template(s).", templates=templates, count=len(templates))

    @tool(description="Get a template and all of its versions by ID.", params={"template_id": "The template ID, e.g. 'd-abc123...'."})
    async def get_template(self, template_id: str) -> Dict:
        data, _ = await self._sg_request("GET", f"/templates/{template_id}")
        err = self._err(data, "Failed to get template")
        if err:
            return err
        return self._ok(f"Template '{(data or {}).get('name')}'.", template=data)

    @tool(
        description="Create a new (empty) template shell. Add content with create_template_version.",
        params={"name": "Template name.", "generation": "'dynamic' (default) or 'legacy'."},
    )
    async def create_template(self, name: str, generation: str = "dynamic") -> Dict:
        data, _ = await self._sg_request("POST", "/templates", json_body={"name": name, "generation": generation})
        err = self._err(data, "Failed to create template")
        if err:
            return err
        return self._ok(f"Template '{name}' created (id={(data or {}).get('id')}).", template=data)

    @tool(description="Delete a template and all of its versions by ID.", params={"template_id": "The template ID to delete."})
    async def delete_template(self, template_id: str) -> Dict:
        data, _ = await self._sg_request("DELETE", f"/templates/{template_id}")
        err = self._err(data, "Failed to delete template")
        if err:
            return err
        return self._ok(f"Template {template_id} deleted.")

    @tool(
        description="Create (and optionally activate) a new content version for an existing dynamic template.",
        params={
            "template_id": "The parent template ID.",
            "name": "Name for this version, e.g. 'v1' or 'welcome-en'.",
            "subject": "Email subject line for this version (supports {{handlebars}} variables).",
            "html_content": "HTML body for this version (supports {{handlebars}} variables).",
            "plain_content": "Plain-text body for this version.",
            "active": "Whether to activate this version immediately (only one version can be active at a time). Default true.",
        },
    )
    async def create_template_version(
        self, template_id: str, name: str, subject: str,
        html_content: Optional[str] = None, plain_content: Optional[str] = None, active: bool = True,
    ) -> Dict:
        body = self._strip_none({
            "name": name, "subject": subject, "html_content": html_content,
            "plain_content": plain_content, "active": 1 if active else 0,
        })
        data, _ = await self._sg_request("POST", f"/templates/{template_id}/versions", json_body=body)
        err = self._err(data, "Failed to create template version")
        if err:
            return err
        return self._ok(f"Template version '{name}' created (id={(data or {}).get('id')}).", version=data)

    # ==================================================================
    # MARKETING CONTACT LISTS & CONTACTS
    # ==================================================================

    @tool(description="List marketing contact lists on this account.", params={})
    async def list_contact_lists(self) -> Dict:
        data, _ = await self._sg_request("GET", "/marketing/lists", params={"page_size": 100})
        err = self._err(data, "Failed to list contact lists")
        if err:
            return err
        result = (data or {}).get("result") or []
        return self._ok(f"Found {len(result)} contact list(s).", lists=result, count=len(result))

    @tool(description="Create a new marketing contact list.", params={"name": "Name for the new list."})
    async def create_contact_list(self, name: str) -> Dict:
        data, _ = await self._sg_request("POST", "/marketing/lists", json_body={"name": name})
        err = self._err(data, "Failed to create contact list")
        if err:
            return err
        return self._ok(f"Contact list '{name}' created (id={(data or {}).get('id')}).", list=data)

    @tool(
        description="Delete a marketing contact list by ID.",
        params={
            "list_id": "The list ID to delete.",
            "delete_contacts": "Whether to also delete the contacts that only belong to this list. Default false.",
        },
    )
    async def delete_contact_list(self, list_id: str, delete_contacts: bool = False) -> Dict:
        data, _ = await self._sg_request(
            "DELETE", f"/marketing/lists/{list_id}", params={"delete_contacts": str(delete_contacts).lower()}
        )
        err = self._err(data, "Failed to delete contact list")
        if err:
            return err
        return self._ok(f"Contact list {list_id} deleted.")

    @tool(
        description="Create or update (upsert by email) one or more marketing contacts, optionally adding them to lists. Runs asynchronously on SendGrid's side.",
        params={
            "contacts": "List of contact objects, e.g. [{'email':'a@b.com','first_name':'Jane','last_name':'Doe'}].",
            "list_ids": "Optional list of contact list IDs to add these contacts to.",
        },
    )
    async def upsert_contacts(self, contacts: List[Dict[str, Any]], list_ids: Optional[List[str]] = None) -> Dict:
        if not contacts:
            return {"success": False, "response": "Provide at least one contact."}
        body: Dict[str, Any] = {"contacts": contacts}
        if list_ids:
            body["list_ids"] = list_ids
        data, _ = await self._sg_request("PUT", "/marketing/contacts", json_body=body)
        err = self._err(data, "Failed to upsert contacts")
        if err:
            return err
        return self._ok(
            f"Upserted {len(contacts)} contact(s) (job_id={(data or {}).get('job_id')}).",
            job_id=(data or {}).get("job_id"),
        )

    @tool(
        description="Look up contacts by exact email address(es), or search with a SendGrid query-language (SGQL) expression.",
        params={
            "emails": "List of exact email addresses to look up.",
            "query": "SGQL search expression, e.g. \"first_name LIKE 'John%'\". Used only if emails is not provided.",
        },
    )
    async def search_contacts(self, emails: Optional[List[str]] = None, query: Optional[str] = None) -> Dict:
        if emails:
            data, _ = await self._sg_request("POST", "/marketing/contacts/search/emails", json_body={"emails": emails})
            err = self._err(data, "Failed to look up contacts by email")
            if err:
                return err
            result = (data or {}).get("result") or {}
            return self._ok(f"Found {len(result)} matching contact(s).", contacts=result, count=len(result))
        if query:
            data, _ = await self._sg_request("POST", "/marketing/contacts/search", json_body={"query": query})
            err = self._err(data, "Failed to search contacts")
            if err:
                return err
            result = (data or {}).get("result") or []
            return self._ok(f"Found {len(result)} matching contact(s).", contacts=result, count=len(result))
        return {"success": False, "response": "Provide either emails or query."}

    @tool(
        description="Delete contacts by ID, or delete all contacts on the account.",
        params={
            "contact_ids": "List of contact IDs to delete.",
            "delete_all": "Whether to delete ALL contacts on the account. Destructive -- default false.",
        },
    )
    async def delete_contacts(self, contact_ids: Optional[List[str]] = None, delete_all: bool = False) -> Dict:
        if not contact_ids and not delete_all:
            return {"success": False, "response": "Provide contact_ids or set delete_all=true."}
        params: Dict[str, Any] = {"delete_all_contacts": "true"} if delete_all else {"ids": ",".join(contact_ids or [])}
        data, _ = await self._sg_request("DELETE", "/marketing/contacts", params=params)
        err = self._err(data, "Failed to delete contacts")
        if err:
            return err
        return self._ok("Contact deletion job queued.", job_id=(data or {}).get("job_id"))

    # ==================================================================
    # SUPPRESSION MANAGEMENT
    # ==================================================================

    @tool(
        description="List suppressed email addresses of a given type (bounces, blocks, invalid_emails, spam_reports, or global_unsubscribes).",
        params={
            "suppression_type": "One of: 'bounces', 'blocks', 'invalid_emails', 'spam_reports', 'global_unsubscribes'.",
            "start_time": "Optional Unix timestamp to filter results from.",
            "end_time": "Optional Unix timestamp to filter results until.",
            "email": "Optional email address to filter the results down to.",
        },
    )
    async def list_suppressions(
        self, suppression_type: str, start_time: Optional[int] = None, end_time: Optional[int] = None,
        email: Optional[str] = None,
    ) -> Dict:
        path = self._SUPPRESSION_PATHS.get(suppression_type)
        if not path:
            return {
                "success": False,
                "response": f"Unknown suppression_type '{suppression_type}'. Use one of: {', '.join(self._SUPPRESSION_PATHS)}.",
            }
        params = self._strip_none({"start_time": start_time, "end_time": end_time})
        data, _ = await self._sg_request("GET", path, params=params or None)
        err = self._err(data, f"Failed to list {suppression_type}")
        if err:
            return err
        items = data if isinstance(data, list) else (data or {}).get("result") or []
        if email:
            items = [i for i in items if isinstance(i, dict) and (i.get("email") or "").lower() == email.lower()]
        return self._ok(f"Found {len(items)} {suppression_type} entry(ies).", entries=items, count=len(items))

    @tool(
        description="Delete suppressed email address(es) of a given type -- a single email, or all entries of that type.",
        params={
            "suppression_type": "One of: 'bounces', 'blocks', 'invalid_emails', 'spam_reports', 'global_unsubscribes'.",
            "email": "Single email address to remove from the suppression list. Required for 'global_unsubscribes'.",
            "delete_all": "Whether to clear the ENTIRE suppression list of this type. Destructive -- default false. Not supported for 'global_unsubscribes'.",
        },
    )
    async def delete_suppression(self, suppression_type: str, email: Optional[str] = None, delete_all: bool = False) -> Dict:
        path = self._SUPPRESSION_PATHS.get(suppression_type)
        if not path:
            return {
                "success": False,
                "response": f"Unknown suppression_type '{suppression_type}'. Use one of: {', '.join(self._SUPPRESSION_PATHS)}.",
            }
        if suppression_type == "global_unsubscribes":
            if not email:
                return {"success": False, "response": "email is required to remove a global unsubscribe."}
            data, _ = await self._sg_request("DELETE", f"{path}/{email}")
        elif delete_all:
            data, _ = await self._sg_request("DELETE", path, json_body={"delete_all": True})
        elif email:
            data, _ = await self._sg_request("DELETE", path, json_body={"emails": [email]})
        else:
            return {"success": False, "response": "Provide email or set delete_all=true."}
        err = self._err(data, f"Failed to delete {suppression_type} entry")
        if err:
            return err
        return self._ok(f"Removed {email or 'all'} from {suppression_type}.")

    # ==================================================================
    # STATS
    # ==================================================================

    @tool(
        description="Get aggregate email activity stats (delivered, opens, clicks, bounces, etc.) for a date range.",
        params={
            "start_date": "Start date, format YYYY-MM-DD.",
            "end_date": "End date, format YYYY-MM-DD. Defaults to start_date.",
            "aggregated_by": "Bucket granularity: 'day' (default), 'week', or 'month'.",
        },
    )
    async def get_email_stats(self, start_date: str, end_date: Optional[str] = None, aggregated_by: str = "day") -> Dict:
        params = self._strip_none({"start_date": start_date, "end_date": end_date, "aggregated_by": aggregated_by})
        data, _ = await self._sg_request("GET", "/stats", params=params)
        err = self._err(data, "Failed to get email stats")
        if err:
            return err
        stats = data if isinstance(data, list) else []
        return self._ok(f"Retrieved stats for {len(stats)} period(s).", stats=stats)

    # ==================================================================
    # HEURISTIC CATCH-ALL
    # ==================================================================

    @tool(
        description=(
            "Heuristic catch-all: given one free-text instruction plus whichever of the optional "
            "parameters are known, infer and execute the single most likely SendGrid action "
            "(usually sending an email). Prefer calling a specific tool directly when the desired "
            "action is already clear."
        ),
        params={
            "query": "Free-text instruction, e.g. 'email jane@acme.com about the invoice'.",
            "to": "Recipient email address, if known.",
            "subject": "Email subject, if known.",
            "body": "Email body text, if known.",
            "template_id": "A relevant dynamic template ID, if known.",
        },
    )
    async def action_by_query(
        self, query: str, to: Optional[str] = None, subject: Optional[str] = None,
        body: Optional[str] = None, template_id: Optional[str] = None,
    ) -> Dict:
        q = (query or "").lower()
        if not to:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", query or "")
            if m:
                to = m.group(0)

        if template_id or "template" in q:
            if not to or not template_id:
                return {"success": False, "response": "A template email needs 'to' and 'template_id'."}
            return await self.send_template_email(to=to, template_id=template_id)

        if "send" in q or "email" in q or to:
            if not to or not subject or not body:
                return {"success": False, "response": "Sending an email needs 'to', 'subject', and 'body'."}
            return await self.send_email(to=to, subject=subject, text_content=body)

        return {"success": False, "response": f"Could not infer a SendGrid action from: {query!r}"}
