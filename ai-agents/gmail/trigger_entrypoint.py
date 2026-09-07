from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent
from .gmail_functions import GmailFunctions, _get_snippet_or_body, _parse_email_headers

logger = logging.getLogger(__name__)

GMAIL_WATCH_URL = "https://gmail.googleapis.com/gmail/v1/users/me/watch"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_HISTORY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/history"
GMAIL_MESSAGE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _gmail_expiration_to_expiry_datetime(expiration: Optional[Any]) -> Optional[str]:
    """Convert Gmail watch's `expiration` (epoch milliseconds, as a string) into
    the "YYYY-MM-DD HH:MM:SS" format the trigger-callbacks API expects."""
    if not expiration:
        return None
    try:
        return datetime.fromtimestamp(int(expiration) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


class GmailEmailReceivedTrigger(ConnectedServiceTriggerAgent):
    def __init__(
        self,
        *,
        app_code: Optional[str] = None,
        workflow_id: Optional[str] = None,
        trigger_node_id: Optional[str] = None,
        trigger_static_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.app_code = app_code
        self.workflow_id = workflow_id
        self.trigger_node_id = trigger_node_id
        self.static_config: Dict[str, Any] = trigger_static_config or {}

    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return "Subscribes to Gmail push notifications (Pub/Sub) so new incoming mail generates a watch history record."

    async def _get_profile_email(self) -> Optional[str]:
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.get(
                    GMAIL_PROFILE_URL,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            logger.warning("Gmail profile fetch failed: %s", e)
            return None

        if resp.status_code != 200:
            logger.warning("Gmail profile API returned %s: %s", resp.status_code, resp.text[:500])
            return None
        return (resp.json() or {}).get("emailAddress")

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        # topic_name/label_ids/label_filter_action are static, manifest-level config
        # (ai_agents/gmail/plugin.json's triggers[].config) -- same idea as
        # ToolPlugin.mcp_url for mcp_wrapper tools -- not supplied by the API caller.
        logger.info("Gmail email_received trigger static_config: %s", self.static_config)
        topic_name = (self.static_config.get("topic_name") or "").strip()
        if not topic_name:
            return {"success": False, "error": "trigger is missing config.topic_name in ai_agents/gmail/plugin.json"}
        if not self.access_token:
            return {"success": False, "error": "No access token available for this connected Gmail account"}

        body: Dict[str, Any] = {"topicName": topic_name}
        label_ids = self.static_config.get("label_ids")
        if label_ids:
            body["labelIds"] = label_ids
        label_filter_action = self.static_config.get("label_filter_action")
        if label_filter_action:
            body["labelFilterAction"] = label_filter_action

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.post(
                    GMAIL_WATCH_URL,
                    headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
                    json=body,
                )

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            return {"success": False, "error": f"Request to Gmail watch API failed: {e}"}

        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"Gmail watch API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }

        data = resp.json()
        history_id = data.get("historyId")
        expiration = data.get("expiration")

        # Identify which mailbox this watch belongs to, then register that as the
        # callback_unique_identifier so the workflow engine can match a future
        # push notification (which carries the same emailAddress) back to
        # workflow_id/trigger_node_id.
        email_address = await self._get_profile_email()
        if not email_address:
            return {
                "success": False,
                "error": "Gmail watch succeeded but failed to fetch profile emailAddress for callback registration",
                "topic_name": topic_name,
                "history_id": history_id,
                "expiration": expiration,
                "raw_response": data,
            }

        callback_result = await self.register_workflow_trigger_callback(
            callback_unique_identifier=email_address,
            metadata={"topic_name": topic_name, "history_id": history_id, "expiration": expiration},
            expiry_datetime=_gmail_expiration_to_expiry_datetime(expiration),
        )
        if not callback_result.get("success"):
            return {
                "success": False,
                "error": f"Gmail watch succeeded but workflow trigger callback registration failed: {callback_result.get('error')}",
                "topic_name": topic_name,
                "history_id": history_id,
                "expiration": expiration,
                "callback_unique_identifier": email_address,
                "raw_response": data,
            }

        return {
            "success": True,
            "topic_name": topic_name,
            "history_id": history_id,
            "expiration": expiration,
            "callback_unique_identifier": email_address,
            "raw_response": data,
        }

    async def execute_trigger(
        self, trigger_body: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Called by the workflow engine when this trigger node runs. metadata is
        what subscribe() saved via register_workflow_trigger_callback() for
        this exact workflow/node -- specifically history_id, the Gmail
        historyId the watch subscription started from.

        Calls Gmail's history.list from that point. If there's new history,
        takes the last (most recent) history entry, then the last message
        within it, re-registers the trigger callback with metadata.history_id
        advanced to this response's historyId (same topic_name/expiration,
        otherwise unchanged) so the next run doesn't reprocess the same
        message, then fetches that message's full detail via
        messages.get?format=full and returns that as the node's output.
        Returns an empty list if there's no new history to process.
        """
        metadata = metadata or {}
        history_id = metadata.get("history_id")
        if not history_id:
            logger.warning(
                "Gmail execute_trigger: no history_id in metadata (metadata=%r) -- the registered "
                "trigger callback for this workflow/node likely predates metadata support and needs "
                "to be re-subscribed via POST /api/agent-triggers/subscribe",
                metadata,
            )
            return [{"success": False, "error": "no history_id found in trigger metadata"}]
        if not self.access_token:
            logger.warning("Gmail execute_trigger: no access token available for this connected Gmail account")
            return [{"success": False, "error": "No access token available for this connected Gmail account"}]

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.get(
                    GMAIL_HISTORY_URL,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params={"startHistoryId": history_id},
                )

        try:
            resp = await _call()
            if resp.status_code == 401 and await self.refresh_access_token():
                resp = await _call()
        except httpx.RequestError as e:
            logger.warning("Gmail execute_trigger: request to history API failed: %s", e)
            return [{"success": False, "error": f"Request to Gmail history API failed: {e}"}]

        if resp.status_code != 200:
            logger.warning("Gmail execute_trigger: history API returned %s: %s", resp.status_code, resp.text[:500])
            return [{
                "success": False,
                "error": f"Gmail history API returned {resp.status_code}: {resp.text[:500]}",
                "status_code": resp.status_code,
            }]

        data = resp.json()
        history_list = data.get("history") or []
        if not history_list:
            logger.info("Gmail execute_trigger: no new history since history_id=%s", history_id)
            return []

        last_history_entry = history_list[-1]
        messages_list = last_history_entry.get("messages") or []
        if not messages_list:
            logger.info("Gmail execute_trigger: last history entry has no messages (history_id=%s)", history_id)
            return []
        last_message = messages_list[-1]

        new_history_id = data.get("historyId") or last_history_entry.get("id") or history_id
        email_address = await self._get_profile_email()
        if email_address:
            updated_metadata = dict(metadata)
            updated_metadata["history_id"] = new_history_id
            callback_result = await self.register_workflow_trigger_callback(
                callback_unique_identifier=email_address,
                metadata=updated_metadata,
                # history.list doesn't extend the watch subscription -- the original
                # expiration (from subscribe's watch call) still applies, so it's
                # preserved as-is, not recomputed.
                expiry_datetime=_gmail_expiration_to_expiry_datetime(updated_metadata.get("expiration")),
            )
            if not callback_result.get("success"):
                logger.warning(
                    "Gmail execute_trigger: failed to advance trigger callback history_id to %s: %s",
                    new_history_id, callback_result.get("error"),
                )
        else:
            logger.warning("Gmail execute_trigger: could not fetch profile emailAddress to advance trigger callback history_id")

        message_id = (last_message or {}).get("id")
        if not message_id:
            logger.warning("Gmail execute_trigger: last message has no id, returning message stub as-is: %r", last_message)
            return [last_message]

        async def _call_message_detail() -> httpx.Response:
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.get(
                    f"{GMAIL_MESSAGE_URL}/{message_id}",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params={"format": "full"},
                )

        try:
            detail_resp = await _call_message_detail()
            if detail_resp.status_code == 401 and await self.refresh_access_token():
                detail_resp = await _call_message_detail()
        except httpx.RequestError as e:
            logger.warning("Gmail execute_trigger: request to messages.get failed: %s", e)
            return [{"success": False, "error": f"Request to Gmail messages.get failed: {e}"}]

        if detail_resp.status_code != 200:
            logger.warning("Gmail execute_trigger: messages.get returned %s: %s", detail_resp.status_code, detail_resp.text[:500])
            return [{
                "success": False,
                "error": f"Gmail messages.get returned {detail_resp.status_code}: {detail_resp.text[:500]}",
                "status_code": detail_resp.status_code,
            }]

        detail = detail_resp.json()
        headers = _parse_email_headers(detail)
        body_text = _get_snippet_or_body(detail)
        # _extract_attachment_list is a pure function of msg -- doesn't touch self --
        # so it's called unbound rather than instantiating a whole GmailFunctions
        # just to reach it.
        attachments = GmailFunctions._extract_attachment_list(None, detail)
        att_str = ""
        if attachments:
            att_str = "\n\nAttachments:\n" + "\n".join(
                f"- {a.get('filename', 'file')} ({a.get('mimeType', '')}, {a.get('size', 0)} bytes, ID: {a.get('attachmentId', '')})"
                for a in attachments
            )
        # Same response shape as GmailFunctions.read_email() -- a trigger firing is
        # conceptually "an email was received", so its output should look identical
        # to reading that same email via the read_email action -- plus label_ids so
        # downstream nodes can branch on which label (e.g. INBOX) the message has.
        return [{
            "success": True,
            "response": f"From: {headers.get('from')}\nTo: {headers.get('to')}\nSubject: {headers.get('subject')}\nDate: {headers.get('date')}\n\n{body_text or ''}{att_str}",
            "message_id": detail.get("id") or message_id,
            "thread_id": detail.get("threadId"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "body": body_text,
            "attachments": attachments,
            "label_ids": detail.get("labelIds") or [],
        }]


async def create_trigger_agent(
    *,
    app_code: str,
    trigger_id: str,
    app_config: Dict[str, Any],
    trigger_static_config: Optional[Dict[str, Any]] = None,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    trigger_node_id: Optional[str] = None,
) -> GmailEmailReceivedTrigger:
    return GmailEmailReceivedTrigger(
        token=token,
        company_id=company_id,
        user_id=user_id,
        app_config=app_config or {},
        trigger_static_config=trigger_static_config or {},
        app_code=app_code,
        workflow_id=workflow_id,
        trigger_node_id=trigger_node_id,
    )


def handle_trigger_callback(
    *,
    app_code: str,
    trigger_defs: List[Any],
    method: str,
    headers: Dict[str, str],
    query_params: Dict[str, str],
    raw_body: bytes,
) -> Dict[str, Any]:
    """
    Owns the whole inbound-callback decision for Gmail's Pub/Sub push
    notifications: parses the body, extracts callback_unique_identifier (the
    mailbox's emailAddress -- the same value subscribe() registered via
    register_workflow_trigger_callback()), and decides this endpoint's actual
    response. No signature to validate here -- Gmail push auth, if ever
    configured, would arrive as a bearer OIDC token in the Authorization
    header, but nothing in subscribe() sets that up yet, so there's no 401
    path. Always acks 200 + an empty JSON body: Pub/Sub push only needs any
    2xx to consider the message delivered, and a non-2xx causes Pub/Sub to
    redeliver the same notification.
    """
    try:
        callback_body = json.loads(raw_body) if raw_body else {}
    except ValueError:
        callback_body = {}
    if not isinstance(callback_body, dict):
        callback_body = {}

    logger.info("Gmail trigger callback received: app_code=%s body=%s", app_code, callback_body)
    email_address = callback_body.get("emailAddress") or callback_body.get("callbackUniqueIdentifier")

    return {
        "status_code": 200,
        "response_body": {},
        "response_content_type": "application/json",
        "callback_unique_identifiers": [email_address] if email_address else [],
    }
