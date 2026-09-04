"""
Google Drive functions class for functions_wrapper plugin.

Each public @tool method maps to one Google Drive REST API v2 action.
Reference: https://developers.google.com/workspace/drive/api/reference/rest/v2

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v2"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v2"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


class GoogleDriveFunctions(ConnectedServiceToolAgent):
    """
    Google Drive tool functions. Each @tool method is a Drive API v2 capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Google Drive: list/search/get/create/update/copy/delete/trash/untrash/export files, "
        "manage folder structure (parents/children), permissions/sharing, comments, replies, "
        "revisions, custom properties, shared drives, and account info."
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
        # Set by FunctionsWrapperAgentTool before each tool call
        self._current_query: str = ""
        self._step_results: List[Dict] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("GoogleDriveFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GoogleDriveFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GoogleDriveFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_access_token(self) -> bool:
        ok = await self.refresh_access_token(client=self._httpx_client)
        return bool(ok and self.access_token)

    async def _drive_request(
        self,
        method: str,
        path: str,
        *,
        base: str = DRIVE_API_BASE,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if content is not None:
            headers = {"Content-Type": content_type or "application/octet-stream"}
            r = await self._httpx_client.request(method, url, content=content, params=params, headers=headers)
        elif json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._drive_request(
                method, path, base=base, json_body=json_body, params=params,
                content=content, content_type=content_type, retry_401=False,
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "Drive API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._drive_request(
                method, path, base=base, json_body=json_body, params=params,
                content=content, content_type=content_type,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("Drive API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("Drive API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            return None
        if r.status_code == 204 or not r.content:
            return {}
        ctype = r.headers.get("content-type", "")
        if "application/json" not in ctype:
            # export/raw content endpoints return non-JSON bodies
            return {"_raw_content": r.content, "_content_type": ctype}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Drive API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # @tool public methods — Files
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List or search files and folders in the user's Google Drive. "
            "Use the q parameter to filter results with Drive's query syntax. "
            "Returns file IDs, titles, MIME types, parents, and links — "
            "file/folder IDs returned here are required by most other Drive tools."
        ),
        params={
            "q": (
                "Optional Drive search query string. Examples: "
                "\"title contains 'Report'\" — files whose title contains 'Report'; "
                "\"mimeType = 'application/vnd.google-apps.folder'\" — only folders; "
                "\"'1234folderId' in parents\" — files directly inside a given folder; "
                "\"trashed = false\" — exclude trashed files (default behavior); "
                "\"fullText contains 'budget'\" — full-text search of file contents; "
                "\"sharedWithMe = true\" — files shared with the user. "
                "Combine clauses with 'and' / 'or', e.g. \"trashed = false and mimeType != 'application/vnd.google-apps.folder'\"."
            ),
            "page_size": "Maximum number of files to return (maxResults). Default 100, maximum 1000.",
            "page_token": "Pagination token from a previous list_files call's nextPageToken.",
            "order_by": (
                "Comma-separated sort keys. Valid keys: createdDate, folder, lastViewedByMeDate, "
                "modifiedByMeDate, modifiedDate, quotaBytesUsed, recency, sharedWithMeDate, "
                "starred, title, title_natural, viewedByMeDate. "
                "Append ' desc' for descending order, e.g. 'modifiedDate desc'."
            ),
            "spaces": "Comma-separated list of spaces to query: 'drive', 'appDataFolder', 'photos'. Default 'drive'.",
            "corpora": (
                "Bodies of items to search. One or more of: 'user' (user's own items), "
                "'domain' (items shared to the user's domain), 'drive' (a specific shared drive, "
                "requires drive_id), 'allDrives' (all shared drives the user is a member of, deprecated)."
            ),
            "drive_id": "ID of the shared drive to search. Required when corpora is 'drive'.",
            "include_items_from_all_drives": "If true, include items from both My Drive and shared drives. Default false.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def list_files(
        self,
        q: Optional[str] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        order_by: Optional[str] = None,
        spaces: Optional[str] = None,
        corpora: Optional[str] = None,
        drive_id: Optional[str] = None,
        include_items_from_all_drives: Optional[bool] = None,
        supports_all_drives: Optional[bool] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"maxResults": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE)}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        if order_by:
            params["orderBy"] = order_by
        if spaces:
            params["spaces"] = spaces
        if corpora:
            params["corpora"] = corpora
        if drive_id:
            params["driveId"] = drive_id
        if include_items_from_all_drives is not None:
            params["includeItemsFromAllDrives"] = bool(include_items_from_all_drives)
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        data = await self._drive_request("GET", "/files", params=params)
        if not data:
            return {"success": True, "response": "No files found.", "files": [], "count": 0}
        items = data.get("items") or []
        out = [_file_summary(f) for f in items]
        return {
            "success": True,
            "response": f"Found {len(out)} file(s).",
            "files": out,
            "count": len(out),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve the metadata for a single Google Drive file or folder by its ID. "
            "Returns title, MIME type, owners, parents, size, timestamps, sharing links, "
            "and (for Google Docs/Sheets/Slides) export links."
        ),
        params={
            "file_id": "The ID of the file or folder to retrieve. Get this from list_files. Required.",
            "fields": (
                "Optional partial response field mask (Google API fields syntax), e.g. "
                "'title,mimeType,parents,webViewLink'. If omitted, the full resource is returned."
            ),
            "acknowledge_abuse": (
                "If true, allows downloading metadata for files flagged as malware/abuse "
                "(the requesting user must still be the owner). Default false."
            ),
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def get_file(
        self,
        file_id: str,
        fields: Optional[str] = None,
        acknowledge_abuse: Optional[bool] = None,
        supports_all_drives: Optional[bool] = None,
    ) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = fields
        if acknowledge_abuse is not None:
            params["acknowledgeAbuse"] = bool(acknowledge_abuse)
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        data = await self._drive_request("GET", f"/files/{file_id}", params=params)
        if not data:
            return {"success": False, "response": "File not found.", "file_id": file_id}
        return {"success": True, "response": _format_file_summary(data), "file": data}

    @tool(
        description=(
            "Create a new file or folder in Google Drive. "
            "To create a folder, set mime_type to 'application/vnd.google-apps.folder'. "
            "To create a plain file with text content, provide content and mime_type "
            "(e.g. 'text/plain', 'text/csv', 'text/html'). "
            "If content is omitted, an empty file/folder of the given MIME type is created. "
            "Returns the new file's ID, title, and links."
        ),
        params={
            "name": "Title of the new file or folder. Example: 'Q3 Report' or 'Project Files'.",
            "mime_type": (
                "MIME type of the new file. Use 'application/vnd.google-apps.folder' for a folder. "
                "For plain files, examples: 'text/plain', 'text/csv', 'text/html', 'application/json'. "
                "If omitted and no content is provided, Drive infers a generic type."
            ),
            "parents": (
                "Optional list of parent folder IDs the new file should be placed in. "
                "If omitted, the file is created in the root of My Drive. "
                "Example: ['1AbCDefGhIJKLmnoPQRstuVWxyz']."
            ),
            "description": "Optional short description of the file.",
            "content": (
                "Optional text content to upload as the file body. Only used for non-folder files. "
                "The content is uploaded after the file is created, using mime_type as its content type."
            ),
            "starred": "If true, marks the new file as starred. Default false.",
        },
    )
    async def create_file(
        self,
        name: str,
        mime_type: Optional[str] = None,
        parents: Optional[List[str]] = None,
        description: Optional[str] = None,
        content: Optional[str] = None,
        starred: Optional[bool] = None,
    ) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        body = _build_file_body(
            name=name, mime_type=mime_type, parents=parents,
            description=description, starred=starred,
        )
        created = await self._drive_request("POST", "/files", json_body=body)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create the file."}
        if content is not None:
            uploaded = await self._drive_request(
                "PUT", f"/files/{created['id']}",
                base=DRIVE_UPLOAD_BASE,
                params={"uploadType": "media"},
                content=content.encode("utf-8"),
                content_type=mime_type or "text/plain",
            )
            if uploaded:
                created = uploaded
        return {"success": True, "response": _format_file_created_response(created), "file": created}

    @tool(
        description=(
            "Update (patch) an existing Google Drive file's metadata and/or content. "
            "Only the fields you provide are changed. "
            "Use add_parents/remove_parents to move a file between folders. "
            "If content is provided, the file's body is replaced with the new content."
        ),
        params={
            "file_id": "The ID of the file to update. Required.",
            "name": "New title for the file.",
            "description": "New description for the file.",
            "mime_type": "New MIME type for the file (rarely needed).",
            "content": "New text content to replace the file's body. Uses mime_type (or the file's existing type) as the content type.",
            "add_parents": "List of folder IDs to add as parents (move/copy into these folders).",
            "remove_parents": "List of folder IDs to remove as parents (move out of these folders).",
            "starred": "Set to true to star the file, false to unstar it.",
            "trashed": "Set to true to move the file to trash, false to restore it (prefer trash_file/untrash_file).",
            "new_revision": "If true (default), creates a new file revision when content is updated. Set false to overwrite without revisioning.",
            "set_modified_date": "If true, sets the file's modifiedDate to the current time. Default false.",
        },
    )
    async def update_file(
        self,
        file_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        mime_type: Optional[str] = None,
        content: Optional[str] = None,
        add_parents: Optional[List[str]] = None,
        remove_parents: Optional[List[str]] = None,
        starred: Optional[bool] = None,
        trashed: Optional[bool] = None,
        new_revision: Optional[bool] = None,
        set_modified_date: Optional[bool] = None,
    ) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        body = _build_file_body(
            name=name, mime_type=mime_type, description=description,
            starred=starred, trashed=trashed,
        )
        params: Dict[str, Any] = {}
        if add_parents:
            params["addParents"] = ",".join(add_parents)
        if remove_parents:
            params["removeParents"] = ",".join(remove_parents)
        if set_modified_date is not None:
            params["setModifiedDate"] = bool(set_modified_date)
        if not body and not params and content is None:
            return {"success": False, "response": "Provide at least one field to update."}
        updated = body or None
        result = await self._drive_request("PATCH", f"/files/{file_id}", json_body=updated, params=params or None)
        if result is None:
            return {"success": False, "response": "Could not update the file."}
        if content is not None:
            upload_params: Dict[str, Any] = {"uploadType": "media"}
            if new_revision is not None:
                upload_params["newRevision"] = bool(new_revision)
            uploaded = await self._drive_request(
                "PUT", f"/files/{file_id}",
                base=DRIVE_UPLOAD_BASE,
                params=upload_params,
                content=content.encode("utf-8"),
                content_type=mime_type or "text/plain",
            )
            if uploaded:
                result = uploaded
        return {"success": True, "response": _format_file_summary(result), "file": result}

    @tool(
        description=(
            "Create a copy of an existing Google Drive file. "
            "Returns the new copy's file ID, title, and links."
        ),
        params={
            "file_id": "The ID of the file to copy. Required.",
            "name": "Title for the new copy. If omitted, Drive prefixes the original title with 'Copy of'.",
            "parents": "Optional list of parent folder IDs for the copy. If omitted, the copy is placed alongside the original.",
        },
    )
    async def copy_file(
        self,
        file_id: str,
        name: Optional[str] = None,
        parents: Optional[List[str]] = None,
    ) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        body = _build_file_body(name=name, parents=parents)
        copied = await self._drive_request("POST", f"/files/{file_id}/copy", json_body=body or {})
        if not copied or not copied.get("id"):
            return {"success": False, "response": "Could not copy the file."}
        return {"success": True, "response": _format_file_created_response(copied), "file": copied}

    @tool(
        description=(
            "Permanently delete a file or folder from Google Drive, bypassing the trash. "
            "This action cannot be undone. Deleting a folder also deletes everything inside it. "
            "Prefer trash_file for a reversible delete."
        ),
        params={
            "file_id": "The ID of the file or folder to permanently delete. Required.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def delete_file(self, file_id: str, supports_all_drives: Optional[bool] = None) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        params: Dict[str, Any] = {}
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        result = await self._drive_request("DELETE", f"/files/{file_id}", params=params or None)
        if result is None:
            return {"success": False, "response": "Could not delete the file."}
        return {"success": True, "response": "File permanently deleted.", "file_id": file_id}

    @tool(
        description=(
            "Move a Google Drive file or folder to the trash. "
            "Trashed files can be restored with untrash_file or permanently removed with empty_trash. "
            "Returns the updated file metadata showing trashed=true."
        ),
        params={"file_id": "The ID of the file or folder to trash. Required."},
    )
    async def trash_file(self, file_id: str) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        result = await self._drive_request("POST", f"/files/{file_id}/trash")
        if not result:
            return {"success": False, "response": "Could not trash the file."}
        return {"success": True, "response": f"File '{result.get('title')}' moved to trash.", "file": result}

    @tool(
        description=(
            "Restore a file or folder from the trash back to its previous location. "
            "Returns the updated file metadata showing trashed=false."
        ),
        params={"file_id": "The ID of the file or folder to restore from trash. Required."},
    )
    async def untrash_file(self, file_id: str) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        result = await self._drive_request("POST", f"/files/{file_id}/untrash")
        if not result:
            return {"success": False, "response": "Could not restore the file from trash."}
        return {"success": True, "response": f"File '{result.get('title')}' restored from trash.", "file": result}

    @tool(
        description=(
            "Permanently delete ALL files currently in the user's trash. "
            "This action cannot be undone. Use with caution."
        ),
    )
    async def empty_trash(self) -> Dict:
        result = await self._drive_request("DELETE", "/files/trash")
        if result is None:
            return {"success": False, "response": "Could not empty the trash."}
        return {"success": True, "response": "Trash emptied. All trashed files were permanently deleted."}

    @tool(
        description=(
            "Export a Google Workspace document (Docs, Sheets, Slides, Drawings) to a different "
            "MIME type, such as PDF, DOCX, XLSX, CSV, or plain text. "
            "Only works for native Google Docs Editors files, not regular uploaded files. "
            "For text-based export formats, returns the exported content as text. "
            "For binary formats (e.g. PDF), returns the size in bytes instead of the raw content."
        ),
        params={
            "file_id": "The ID of the Google Docs Editors file to export. Required.",
            "mime_type": (
                "Target MIME type for the export. Common values: "
                "'application/pdf', 'text/plain', 'text/csv', 'text/html', "
                "'application/vnd.openxmlformats-officedocument.wordprocessingml.document' (DOCX), "
                "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' (XLSX), "
                "'application/vnd.openxmlformats-officedocument.presentationml.presentation' (PPTX). "
                "Use get_file to see a file's exportLinks for the formats it supports. Required."
            ),
        },
    )
    async def export_file(self, file_id: str, mime_type: str) -> Dict:
        if not file_id or not mime_type:
            return {"success": False, "response": "file_id and mime_type are required."}
        data = await self._drive_request("GET", f"/files/{file_id}/export", params={"mimeType": mime_type})
        if data is None:
            return {"success": False, "response": "Could not export the file. Verify the file is a Google Docs Editors file and mime_type is a supported export format."}
        raw = data.get("_raw_content")
        if raw is None:
            return {"success": True, "response": "Export returned no content.", "content": ""}
        if mime_type.startswith("text/") or mime_type in ("application/json",):
            try:
                text = raw.decode("utf-8")
                return {"success": True, "response": f"Exported {len(text)} characters as {mime_type}.", "content": text}
            except UnicodeDecodeError:
                pass
        return {
            "success": True,
            "response": f"Exported {len(raw)} bytes as {mime_type} (binary content not returned).",
            "size_bytes": len(raw),
            "mime_type": mime_type,
        }

    # ------------------------------------------------------------------
    # @tool public methods — Permissions / Sharing
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List all sharing permissions on a Google Drive file, folder, or shared drive. "
            "Returns each permission's ID, type (user/group/domain/anyone), role, and the "
            "associated email address or domain."
        ),
        params={
            "file_id": "The ID of the file, folder, or shared drive whose permissions to list. Required.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def list_permissions(self, file_id: str, supports_all_drives: Optional[bool] = None) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        params: Dict[str, Any] = {}
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        data = await self._drive_request("GET", f"/files/{file_id}/permissions", params=params or None)
        if not data:
            return {"success": True, "response": "No permissions found.", "permissions": [], "count": 0}
        items = data.get("items") or []
        return {"success": True, "response": f"Found {len(items)} permission(s).", "permissions": items, "count": len(items)}

    @tool(
        description="Retrieve a single sharing permission on a Google Drive file by its permission ID.",
        params={
            "file_id": "The ID of the file whose permission to retrieve. Required.",
            "permission_id": "The ID of the permission to retrieve. Get this from list_permissions. Required.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def get_permission(self, file_id: str, permission_id: str, supports_all_drives: Optional[bool] = None) -> Dict:
        if not file_id or not permission_id:
            return {"success": False, "response": "file_id and permission_id are required."}
        params: Dict[str, Any] = {}
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        data = await self._drive_request("GET", f"/files/{file_id}/permissions/{permission_id}", params=params or None)
        if not data:
            return {"success": False, "response": "Permission not found."}
        return {"success": True, "response": _permission_summary(data), "permission": data}

    @tool(
        description=(
            "Share a Google Drive file or folder with a user, group, domain, or the public (anyone with the link). "
            "Creates a new permission. Returns the new permission's ID and details."
        ),
        params={
            "file_id": "The ID of the file or folder to share. Required.",
            "role": (
                "Access level to grant. One of: 'owner', 'organizer', 'fileOrganizer', "
                "'writer', 'commenter', 'reader'. Required."
            ),
            "type": (
                "Scope of the permission. One of: "
                "'user' — a specific person (requires email_address); "
                "'group' — a Google Group (requires email_address); "
                "'domain' — everyone in a domain (requires domain); "
                "'anyone' — anyone with the link. Required."
            ),
            "email_address": "Email address of the user or group to share with. Required when type is 'user' or 'group'.",
            "domain": "Domain name to share with. Required when type is 'domain'. Example: 'example.com'.",
            "allow_file_discovery": (
                "Whether the file can be discovered via search when type is 'domain' or 'anyone'. "
                "If false, only people with the link can access it."
            ),
            "send_notification_email": "Whether to send a notification email to the user/group being granted access. Default true.",
            "email_message": "Optional custom message included in the sharing notification email.",
            "transfer_ownership": "If true and role is 'owner', transfers file ownership to the specified user. Default false.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def create_permission(
        self,
        file_id: str,
        role: str,
        type: str,
        email_address: Optional[str] = None,
        domain: Optional[str] = None,
        allow_file_discovery: Optional[bool] = None,
        send_notification_email: Optional[bool] = None,
        email_message: Optional[str] = None,
        transfer_ownership: Optional[bool] = None,
        supports_all_drives: Optional[bool] = None,
    ) -> Dict:
        if not file_id or not role or not type:
            return {"success": False, "response": "file_id, role, and type are required."}
        body: Dict[str, Any] = {"role": role, "type": type}
        if email_address:
            body["value"] = email_address
            body["emailAddress"] = email_address
        if domain:
            body["value"] = domain
            body["domain"] = domain
        if allow_file_discovery is not None:
            body["withLink"] = not bool(allow_file_discovery)
        params: Dict[str, Any] = {}
        if send_notification_email is not None:
            params["sendNotificationEmail"] = bool(send_notification_email)
        if email_message:
            params["emailMessage"] = email_message
        if transfer_ownership is not None:
            params["transferOwnership"] = bool(transfer_ownership)
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        created = await self._drive_request("POST", f"/files/{file_id}/permissions", json_body=body, params=params or None)
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create the permission."}
        return {"success": True, "response": _permission_summary(created), "permission": created}

    @tool(
        description=(
            "Update (patch) an existing sharing permission on a Google Drive file — typically used "
            "to change a collaborator's role."
        ),
        params={
            "file_id": "The ID of the file whose permission to update. Required.",
            "permission_id": "The ID of the permission to update. Get this from list_permissions. Required.",
            "role": "New access level. One of: 'owner', 'organizer', 'fileOrganizer', 'writer', 'commenter', 'reader'.",
            "remove_expiration": "If true, removes the expiration date from the permission. Default false.",
            "transfer_ownership": "If true and role is 'owner', transfers file ownership. Default false.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def update_permission(
        self,
        file_id: str,
        permission_id: str,
        role: Optional[str] = None,
        remove_expiration: Optional[bool] = None,
        transfer_ownership: Optional[bool] = None,
        supports_all_drives: Optional[bool] = None,
    ) -> Dict:
        if not file_id or not permission_id:
            return {"success": False, "response": "file_id and permission_id are required."}
        body: Dict[str, Any] = {}
        if role:
            body["role"] = role
        if not body:
            return {"success": False, "response": "Provide at least one field to update (role)."}
        params: Dict[str, Any] = {}
        if remove_expiration is not None:
            params["removeExpiration"] = bool(remove_expiration)
        if transfer_ownership is not None:
            params["transferOwnership"] = bool(transfer_ownership)
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        updated = await self._drive_request("PATCH", f"/files/{file_id}/permissions/{permission_id}", json_body=body, params=params or None)
        if not updated:
            return {"success": False, "response": "Could not update the permission."}
        return {"success": True, "response": _permission_summary(updated), "permission": updated}

    @tool(
        description="Remove a sharing permission from a Google Drive file, revoking that user/group/domain's access.",
        params={
            "file_id": "The ID of the file whose permission to remove. Required.",
            "permission_id": "The ID of the permission to remove. Get this from list_permissions. Required.",
            "supports_all_drives": "If true, indicates the requesting app supports shared drives. Default false.",
        },
    )
    async def delete_permission(self, file_id: str, permission_id: str, supports_all_drives: Optional[bool] = None) -> Dict:
        if not file_id or not permission_id:
            return {"success": False, "response": "file_id and permission_id are required."}
        params: Dict[str, Any] = {}
        if supports_all_drives is not None:
            params["supportsAllDrives"] = bool(supports_all_drives)
        result = await self._drive_request("DELETE", f"/files/{file_id}/permissions/{permission_id}", params=params or None)
        if result is None:
            return {"success": False, "response": "Could not remove the permission."}
        return {"success": True, "response": "Permission removed.", "permission_id": permission_id}

    @tool(
        description=(
            "Look up the Drive permission ID corresponding to an email address. "
            "Useful for constructing permission resource names without first listing permissions."
        ),
        params={"email": "The email address to look up. Required."},
    )
    async def get_permission_id_for_email(self, email: str) -> Dict:
        if not email:
            return {"success": False, "response": "email is required."}
        data = await self._drive_request("GET", f"/permissionIds/{email}")
        if not data or not data.get("id"):
            return {"success": False, "response": "Could not resolve a permission ID for that email."}
        return {"success": True, "response": f"Permission ID for {email}: {data.get('id')}.", "permission_id": data.get("id")}

    # ------------------------------------------------------------------
    # @tool public methods — Comments
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the comments on a Google Drive file. "
            "Returns each comment's ID, author, content, anchor (location in the document), "
            "creation/modification time, and resolved status. "
            "Comment IDs returned here are required by get_comment, update_comment, delete_comment, "
            "and the replies tools."
        ),
        params={
            "file_id": "The ID of the file whose comments to list. Required.",
            "include_deleted": "If true, includes deleted comments (with content removed). Default false.",
            "max_results": "Maximum number of comments to return. Default 20.",
            "page_token": "Pagination token from a previous list_comments call's nextPageToken.",
            "updated_min": "Only return comments modified after this RFC3339 timestamp, e.g. '2026-01-01T00:00:00Z'.",
        },
    )
    async def list_comments(
        self,
        file_id: str,
        include_deleted: Optional[bool] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
        updated_min: Optional[str] = None,
    ) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        params: Dict[str, Any] = {}
        if include_deleted is not None:
            params["includeDeleted"] = bool(include_deleted)
        if max_results:
            params["maxResults"] = int(max_results)
        if page_token:
            params["pageToken"] = page_token
        if updated_min:
            params["updatedMin"] = updated_min
        data = await self._drive_request("GET", f"/files/{file_id}/comments", params=params or None)
        if not data:
            return {"success": True, "response": "No comments found.", "comments": [], "count": 0}
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} comment(s).",
            "comments": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Retrieve a single comment on a Google Drive file by its ID.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the comment to retrieve. Get this from list_comments. Required.",
            "include_deleted": "If true, allows retrieving a deleted comment. Default false.",
        },
    )
    async def get_comment(self, file_id: str, comment_id: str, include_deleted: Optional[bool] = None) -> Dict:
        if not file_id or not comment_id:
            return {"success": False, "response": "file_id and comment_id are required."}
        params: Dict[str, Any] = {}
        if include_deleted is not None:
            params["includeDeleted"] = bool(include_deleted)
        data = await self._drive_request("GET", f"/files/{file_id}/comments/{comment_id}", params=params or None)
        if not data:
            return {"success": False, "response": "Comment not found."}
        return {"success": True, "response": _comment_summary(data), "comment": data}

    @tool(
        description="Add a new top-level comment to a Google Drive file. Returns the created comment's ID and details.",
        params={
            "file_id": "The ID of the file to comment on. Required.",
            "content": "The plain-text content of the comment. Required.",
            "anchor": (
                "Optional region of the document this comment refers to, as a JSON-encoded "
                "anchor string (file-type specific). Usually left empty for a general comment."
            ),
        },
    )
    async def create_comment(self, file_id: str, content: str, anchor: Optional[str] = None) -> Dict:
        if not file_id or not content:
            return {"success": False, "response": "file_id and content are required."}
        body: Dict[str, Any] = {"content": content}
        if anchor:
            body["anchor"] = anchor
        created = await self._drive_request("POST", f"/files/{file_id}/comments", json_body=body)
        if not created or not created.get("commentId"):
            return {"success": False, "response": "Could not create the comment."}
        return {"success": True, "response": _comment_summary(created), "comment": created}

    @tool(
        description="Update the text content of an existing comment on a Google Drive file.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the comment to update. Get this from list_comments. Required.",
            "content": "The new plain-text content for the comment. Required.",
        },
    )
    async def update_comment(self, file_id: str, comment_id: str, content: str) -> Dict:
        if not file_id or not comment_id or content is None:
            return {"success": False, "response": "file_id, comment_id, and content are required."}
        updated = await self._drive_request("PATCH", f"/files/{file_id}/comments/{comment_id}", json_body={"content": content})
        if not updated:
            return {"success": False, "response": "Could not update the comment."}
        return {"success": True, "response": _comment_summary(updated), "comment": updated}

    @tool(
        description="Delete a comment from a Google Drive file. This action cannot be undone.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the comment to delete. Get this from list_comments. Required.",
        },
    )
    async def delete_comment(self, file_id: str, comment_id: str) -> Dict:
        if not file_id or not comment_id:
            return {"success": False, "response": "file_id and comment_id are required."}
        result = await self._drive_request("DELETE", f"/files/{file_id}/comments/{comment_id}")
        if result is None:
            return {"success": False, "response": "Could not delete the comment."}
        return {"success": True, "response": "Comment deleted.", "comment_id": comment_id}

    # ------------------------------------------------------------------
    # @tool public methods — Replies
    # ------------------------------------------------------------------

    @tool(
        description="List all replies to a specific comment on a Google Drive file.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the comment whose replies to list. Get this from list_comments. Required.",
            "include_deleted": "If true, includes deleted replies (with content removed). Default false.",
            "max_results": "Maximum number of replies to return. Default 20.",
            "page_token": "Pagination token from a previous list_replies call's nextPageToken.",
        },
    )
    async def list_replies(
        self,
        file_id: str,
        comment_id: str,
        include_deleted: Optional[bool] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not file_id or not comment_id:
            return {"success": False, "response": "file_id and comment_id are required."}
        params: Dict[str, Any] = {}
        if include_deleted is not None:
            params["includeDeleted"] = bool(include_deleted)
        if max_results:
            params["maxResults"] = int(max_results)
        if page_token:
            params["pageToken"] = page_token
        data = await self._drive_request("GET", f"/files/{file_id}/comments/{comment_id}/replies", params=params or None)
        if not data:
            return {"success": True, "response": "No replies found.", "replies": [], "count": 0}
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} reply(ies).",
            "replies": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Retrieve a single reply to a comment on a Google Drive file by its ID.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the parent comment. Required.",
            "reply_id": "The ID of the reply to retrieve. Get this from list_replies. Required.",
            "include_deleted": "If true, allows retrieving a deleted reply. Default false.",
        },
    )
    async def get_reply(self, file_id: str, comment_id: str, reply_id: str, include_deleted: Optional[bool] = None) -> Dict:
        if not file_id or not comment_id or not reply_id:
            return {"success": False, "response": "file_id, comment_id, and reply_id are required."}
        params: Dict[str, Any] = {}
        if include_deleted is not None:
            params["includeDeleted"] = bool(include_deleted)
        data = await self._drive_request("GET", f"/files/{file_id}/comments/{comment_id}/replies/{reply_id}", params=params or None)
        if not data:
            return {"success": False, "response": "Reply not found."}
        return {"success": True, "response": _reply_summary(data), "reply": data}

    @tool(
        description=(
            "Create a reply to a comment on a Google Drive file. "
            "Can also be used to resolve or reopen the comment via the verb parameter."
        ),
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the comment to reply to. Get this from list_comments. Required.",
            "content": "Plain-text content of the reply. Required unless only setting verb.",
            "verb": (
                "Optional action this reply performs on the comment. One of: "
                "'resolve' — marks the parent comment as resolved; "
                "'reopen' — reopens a previously resolved comment."
            ),
        },
    )
    async def create_reply(self, file_id: str, comment_id: str, content: Optional[str] = None, verb: Optional[str] = None) -> Dict:
        if not file_id or not comment_id:
            return {"success": False, "response": "file_id and comment_id are required."}
        body: Dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if verb:
            body["verb"] = verb
        if not body:
            return {"success": False, "response": "Provide content and/or verb."}
        created = await self._drive_request("POST", f"/files/{file_id}/comments/{comment_id}/replies", json_body=body)
        if not created or not created.get("replyId"):
            return {"success": False, "response": "Could not create the reply."}
        return {"success": True, "response": _reply_summary(created), "reply": created}

    @tool(
        description="Update the text content of an existing reply to a comment.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the parent comment. Required.",
            "reply_id": "The ID of the reply to update. Get this from list_replies. Required.",
            "content": "The new plain-text content for the reply. Required.",
        },
    )
    async def update_reply(self, file_id: str, comment_id: str, reply_id: str, content: str) -> Dict:
        if not file_id or not comment_id or not reply_id or content is None:
            return {"success": False, "response": "file_id, comment_id, reply_id, and content are required."}
        updated = await self._drive_request("PATCH", f"/files/{file_id}/comments/{comment_id}/replies/{reply_id}", json_body={"content": content})
        if not updated:
            return {"success": False, "response": "Could not update the reply."}
        return {"success": True, "response": _reply_summary(updated), "reply": updated}

    @tool(
        description="Delete a reply to a comment on a Google Drive file. This action cannot be undone.",
        params={
            "file_id": "The ID of the file containing the comment. Required.",
            "comment_id": "The ID of the parent comment. Required.",
            "reply_id": "The ID of the reply to delete. Get this from list_replies. Required.",
        },
    )
    async def delete_reply(self, file_id: str, comment_id: str, reply_id: str) -> Dict:
        if not file_id or not comment_id or not reply_id:
            return {"success": False, "response": "file_id, comment_id, and reply_id are required."}
        result = await self._drive_request("DELETE", f"/files/{file_id}/comments/{comment_id}/replies/{reply_id}")
        if result is None:
            return {"success": False, "response": "Could not delete the reply."}
        return {"success": True, "response": "Reply deleted.", "reply_id": reply_id}

    # ------------------------------------------------------------------
    # @tool public methods — Revisions
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the revision history of a Google Drive file. "
            "Returns each revision's ID, modification time, last modifying user, and size. "
            "Revision IDs returned here are required by get_revision and delete_revision."
        ),
        params={"file_id": "The ID of the file whose revisions to list. Required."},
    )
    async def list_revisions(self, file_id: str) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        data = await self._drive_request("GET", f"/files/{file_id}/revisions")
        if not data:
            return {"success": True, "response": "No revisions found.", "revisions": [], "count": 0}
        items = data.get("items") or []
        return {"success": True, "response": f"Found {len(items)} revision(s).", "revisions": items, "count": len(items)}

    @tool(
        description="Retrieve a single revision of a Google Drive file by its revision ID.",
        params={
            "file_id": "The ID of the file. Required.",
            "revision_id": "The ID of the revision to retrieve. Get this from list_revisions. Required.",
        },
    )
    async def get_revision(self, file_id: str, revision_id: str) -> Dict:
        if not file_id or not revision_id:
            return {"success": False, "response": "file_id and revision_id are required."}
        data = await self._drive_request("GET", f"/files/{file_id}/revisions/{revision_id}")
        if not data:
            return {"success": False, "response": "Revision not found."}
        return {"success": True, "response": f"Revision {data.get('id')}: modified {data.get('modifiedDate')}.", "revision": data}

    @tool(
        description=(
            "Permanently delete a specific revision of a Google Drive file. "
            "Only non-head (older) revisions of files that have revisioning enabled can be deleted. "
            "This action cannot be undone."
        ),
        params={
            "file_id": "The ID of the file. Required.",
            "revision_id": "The ID of the revision to delete. Get this from list_revisions. Required.",
        },
    )
    async def delete_revision(self, file_id: str, revision_id: str) -> Dict:
        if not file_id or not revision_id:
            return {"success": False, "response": "file_id and revision_id are required."}
        result = await self._drive_request("DELETE", f"/files/{file_id}/revisions/{revision_id}")
        if result is None:
            return {"success": False, "response": "Could not delete the revision."}
        return {"success": True, "response": "Revision deleted.", "revision_id": revision_id}

    # ------------------------------------------------------------------
    # @tool public methods — Parents (folder membership)
    # ------------------------------------------------------------------

    @tool(
        description="List the parent folders of a Google Drive file (a file can have multiple parents).",
        params={"file_id": "The ID of the file whose parents to list. Required."},
    )
    async def list_parents(self, file_id: str) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        data = await self._drive_request("GET", f"/files/{file_id}/parents")
        if not data:
            return {"success": True, "response": "No parents found.", "parents": [], "count": 0}
        items = data.get("items") or []
        return {"success": True, "response": f"Found {len(items)} parent(s).", "parents": items, "count": len(items)}

    @tool(
        description="Add a folder as a parent of a file, placing the file inside that folder (files can have multiple parents).",
        params={
            "file_id": "The ID of the file to add a parent to. Required.",
            "parent_id": "The ID of the folder to add as a parent. Required.",
        },
    )
    async def add_parent(self, file_id: str, parent_id: str) -> Dict:
        if not file_id or not parent_id:
            return {"success": False, "response": "file_id and parent_id are required."}
        created = await self._drive_request("POST", f"/files/{file_id}/parents", json_body={"id": parent_id})
        if not created:
            return {"success": False, "response": "Could not add the parent."}
        return {"success": True, "response": f"Folder {parent_id} added as a parent of {file_id}.", "parent": created}

    @tool(
        description="Remove a folder as a parent of a file, taking the file out of that folder.",
        params={
            "file_id": "The ID of the file to remove a parent from. Required.",
            "parent_id": "The ID of the folder to remove as a parent. Required.",
        },
    )
    async def remove_parent(self, file_id: str, parent_id: str) -> Dict:
        if not file_id or not parent_id:
            return {"success": False, "response": "file_id and parent_id are required."}
        result = await self._drive_request("DELETE", f"/files/{file_id}/parents/{parent_id}")
        if result is None:
            return {"success": False, "response": "Could not remove the parent."}
        return {"success": True, "response": f"Folder {parent_id} removed as a parent of {file_id}.", "file_id": file_id, "parent_id": parent_id}

    # ------------------------------------------------------------------
    # @tool public methods — Children (folder contents)
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the direct children (files and subfolders) of a Google Drive folder. "
            "Equivalent to list_files with q=\"'{folder_id}' in parents\", but returns child "
            "references rather than full file metadata."
        ),
        params={
            "folder_id": "The ID of the folder whose children to list. Required.",
            "q": "Optional query string to filter children, using the same syntax as list_files' q parameter.",
            "max_results": "Maximum number of children to return. Default 100.",
            "page_token": "Pagination token from a previous list_children call's nextPageToken.",
        },
    )
    async def list_children(
        self,
        folder_id: str,
        q: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not folder_id:
            return {"success": False, "response": "folder_id is required."}
        params: Dict[str, Any] = {"maxResults": _clamp_page_size(max_results, DEFAULT_PAGE_SIZE)}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        data = await self._drive_request("GET", f"/files/{folder_id}/children", params=params)
        if not data:
            return {"success": True, "response": "No children found.", "children": [], "count": 0}
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} child item(s).",
            "children": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Insert a file into a folder as a child (equivalent to add_parent, expressed from the folder's side).",
        params={
            "folder_id": "The ID of the folder to insert the file into. Required.",
            "child_id": "The ID of the file to insert. Required.",
        },
    )
    async def add_child(self, folder_id: str, child_id: str) -> Dict:
        if not folder_id or not child_id:
            return {"success": False, "response": "folder_id and child_id are required."}
        created = await self._drive_request("POST", f"/files/{folder_id}/children", json_body={"id": child_id})
        if not created:
            return {"success": False, "response": "Could not add the child."}
        return {"success": True, "response": f"File {child_id} added to folder {folder_id}.", "child": created}

    @tool(
        description="Remove a file from a folder (equivalent to remove_parent, expressed from the folder's side).",
        params={
            "folder_id": "The ID of the folder to remove the file from. Required.",
            "child_id": "The ID of the file to remove. Required.",
        },
    )
    async def remove_child(self, folder_id: str, child_id: str) -> Dict:
        if not folder_id or not child_id:
            return {"success": False, "response": "folder_id and child_id are required."}
        result = await self._drive_request("DELETE", f"/files/{folder_id}/children/{child_id}")
        if result is None:
            return {"success": False, "response": "Could not remove the child."}
        return {"success": True, "response": f"File {child_id} removed from folder {folder_id}.", "folder_id": folder_id, "child_id": child_id}

    # ------------------------------------------------------------------
    # @tool public methods — Properties (custom key/value metadata)
    # ------------------------------------------------------------------

    @tool(
        description="List the custom key/value properties attached to a Google Drive file.",
        params={"file_id": "The ID of the file whose properties to list. Required."},
    )
    async def list_properties(self, file_id: str) -> Dict:
        if not file_id:
            return {"success": False, "response": "file_id is required."}
        data = await self._drive_request("GET", f"/files/{file_id}/properties")
        if not data:
            return {"success": True, "response": "No properties found.", "properties": [], "count": 0}
        items = data.get("items") or []
        return {"success": True, "response": f"Found {len(items)} property(ies).", "properties": items, "count": len(items)}

    @tool(
        description="Retrieve a single custom property from a Google Drive file by its key.",
        params={
            "file_id": "The ID of the file. Required.",
            "property_key": "The key of the property to retrieve. Required.",
            "visibility": "Visibility of the property: 'PRIVATE' (default, only visible to the app that created it) or 'PUBLIC'.",
        },
    )
    async def get_property(self, file_id: str, property_key: str, visibility: Optional[str] = None) -> Dict:
        if not file_id or not property_key:
            return {"success": False, "response": "file_id and property_key are required."}
        params: Dict[str, Any] = {}
        if visibility:
            params["visibility"] = visibility
        data = await self._drive_request("GET", f"/files/{file_id}/properties/{property_key}", params=params or None)
        if not data:
            return {"success": False, "response": "Property not found."}
        return {"success": True, "response": f"{data.get('key')} = {data.get('value')} ({data.get('visibility')}).", "property": data}

    @tool(
        description="Add a custom key/value property to a Google Drive file, or update it if the key already exists.",
        params={
            "file_id": "The ID of the file. Required.",
            "key": "The property key. Required.",
            "value": "The property value (string). Required.",
            "visibility": "Visibility of the property: 'PRIVATE' (default, only visible to the app that created it) or 'PUBLIC'.",
        },
    )
    async def create_property(self, file_id: str, key: str, value: str, visibility: Optional[str] = None) -> Dict:
        if not file_id or not key:
            return {"success": False, "response": "file_id and key are required."}
        body: Dict[str, Any] = {"key": key, "value": value}
        if visibility:
            body["visibility"] = visibility
        created = await self._drive_request("POST", f"/files/{file_id}/properties", json_body=body)
        if not created:
            return {"success": False, "response": "Could not create the property."}
        return {"success": True, "response": f"Property {created.get('key')} = {created.get('value')} set.", "property": created}

    @tool(
        description="Update the value of an existing custom property on a Google Drive file.",
        params={
            "file_id": "The ID of the file. Required.",
            "property_key": "The key of the property to update. Required.",
            "value": "The new property value (string). Required.",
            "visibility": "Visibility of the property being updated: 'PRIVATE' (default) or 'PUBLIC'.",
        },
    )
    async def update_property(self, file_id: str, property_key: str, value: str, visibility: Optional[str] = None) -> Dict:
        if not file_id or not property_key:
            return {"success": False, "response": "file_id and property_key are required."}
        body: Dict[str, Any] = {"key": property_key, "value": value}
        if visibility:
            body["visibility"] = visibility
        params: Dict[str, Any] = {}
        if visibility:
            params["visibility"] = visibility
        updated = await self._drive_request("PATCH", f"/files/{file_id}/properties/{property_key}", json_body=body, params=params or None)
        if not updated:
            return {"success": False, "response": "Could not update the property."}
        return {"success": True, "response": f"Property {updated.get('key')} = {updated.get('value')} updated.", "property": updated}

    @tool(
        description="Remove a custom property from a Google Drive file.",
        params={
            "file_id": "The ID of the file. Required.",
            "property_key": "The key of the property to remove. Required.",
            "visibility": "Visibility of the property being removed: 'PRIVATE' (default) or 'PUBLIC'.",
        },
    )
    async def delete_property(self, file_id: str, property_key: str, visibility: Optional[str] = None) -> Dict:
        if not file_id or not property_key:
            return {"success": False, "response": "file_id and property_key are required."}
        params: Dict[str, Any] = {}
        if visibility:
            params["visibility"] = visibility
        result = await self._drive_request("DELETE", f"/files/{file_id}/properties/{property_key}", params=params or None)
        if result is None:
            return {"success": False, "response": "Could not delete the property."}
        return {"success": True, "response": f"Property {property_key} removed.", "property_key": property_key}

    # ------------------------------------------------------------------
    # @tool public methods — About (account info)
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Get information about the current user's Google Drive account: storage quota "
            "(total/used bytes), the user's name and email, supported import/export MIME type "
            "mappings, the root folder ID, and the maximum upload size."
        ),
        params={
            "include_subscribed": "If true (default), includes shared files the user has explicitly added to their drive.",
            "max_change_id_count": "Maximum number of remaining change IDs to count, capped at 1.",
            "start_change_id": "Deprecated. Change ID to start counting from when computing largestChangeId.",
        },
    )
    async def get_about(
        self,
        include_subscribed: Optional[bool] = None,
        max_change_id_count: Optional[int] = None,
        start_change_id: Optional[int] = None,
    ) -> Dict:
        params: Dict[str, Any] = {}
        if include_subscribed is not None:
            params["includeSubscribed"] = bool(include_subscribed)
        if max_change_id_count is not None:
            params["maxChangeIdCount"] = int(max_change_id_count)
        if start_change_id is not None:
            params["startChangeId"] = int(start_change_id)
        data = await self._drive_request("GET", "/about", params=params or None)
        if not data:
            return {"success": False, "response": "Could not fetch account info."}
        quota = data.get("quotaBytesTotal")
        used = data.get("quotaBytesUsed")
        parts = [f"User: {data.get('name')} <{data.get('user', {}).get('emailAddress', '')}>."]
        if quota and used:
            parts.append(f"Storage: {used} / {quota} bytes used.")
        if data.get("rootFolderId"):
            parts.append(f"Root folder ID: {data.get('rootFolderId')}.")
        return {"success": True, "response": " ".join(parts), "about": data}

    # ------------------------------------------------------------------
    # @tool public methods — Shared drives
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the shared drives (Team Drives) the user is a member of, or (with "
            "use_domain_admin_access) all shared drives in the domain. "
            "Returns each shared drive's ID, name, and theme/background settings."
        ),
        params={
            "q": "Optional query string to filter shared drives, e.g. \"name contains 'Marketing'\".",
            "page_size": "Maximum number of shared drives to return. Default 100, maximum 100.",
            "page_token": "Pagination token from a previous list_drives call's nextPageToken.",
            "use_domain_admin_access": "If true, the user is acting as a domain administrator and can see all shared drives in the domain. Default false.",
        },
    )
    async def list_drives(
        self,
        q: Optional[str] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        use_domain_admin_access: Optional[bool] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"maxResults": _clamp_page_size(page_size, 100, 100)}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        if use_domain_admin_access is not None:
            params["useDomainAdminAccess"] = bool(use_domain_admin_access)
        data = await self._drive_request("GET", "/drives", params=params)
        if not data:
            return {"success": True, "response": "No shared drives found.", "drives": [], "count": 0}
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} shared drive(s).",
            "drives": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Retrieve metadata for a single shared drive (Team Drive) by its ID.",
        params={
            "drive_id": "The ID of the shared drive to retrieve. Required.",
            "use_domain_admin_access": "If true, the user is acting as a domain administrator. Default false.",
        },
    )
    async def get_drive(self, drive_id: str, use_domain_admin_access: Optional[bool] = None) -> Dict:
        if not drive_id:
            return {"success": False, "response": "drive_id is required."}
        params: Dict[str, Any] = {}
        if use_domain_admin_access is not None:
            params["useDomainAdminAccess"] = bool(use_domain_admin_access)
        data = await self._drive_request("GET", f"/drives/{drive_id}", params=params or None)
        if not data:
            return {"success": False, "response": "Shared drive not found."}
        return {"success": True, "response": f"Shared drive: {data.get('name')} ({data.get('id')}).", "drive": data}

    @tool(
        description=(
            "Create a new shared drive (Team Drive). "
            "Requires a unique request_id to ensure the operation is not performed multiple times "
            "in case of retries. Returns the new shared drive's ID and name."
        ),
        params={
            "name": "Display name for the new shared drive. Required.",
            "request_id": (
                "A unique, client-generated idempotency token (e.g. a UUID) for this request. "
                "Reusing the same request_id with the same name returns the same drive instead of "
                "creating a duplicate. Required."
            ),
        },
    )
    async def create_drive(self, name: str, request_id: str) -> Dict:
        if not name or not request_id:
            return {"success": False, "response": "name and request_id are required."}
        created = await self._drive_request("POST", "/drives", json_body={"name": name}, params={"requestId": request_id})
        if not created or not created.get("id"):
            return {"success": False, "response": "Could not create the shared drive."}
        return {"success": True, "response": f"Shared drive created: {created.get('name')} ({created.get('id')}).", "drive": created}

    @tool(
        description="Update (rename or reconfigure) an existing shared drive (Team Drive).",
        params={
            "drive_id": "The ID of the shared drive to update. Required.",
            "name": "New display name for the shared drive.",
            "color_rgb": "New color for the shared drive icon, as an RGB hex string, e.g. '#FF0000'.",
            "use_domain_admin_access": "If true, the user is acting as a domain administrator. Default false.",
        },
    )
    async def update_drive(
        self,
        drive_id: str,
        name: Optional[str] = None,
        color_rgb: Optional[str] = None,
        use_domain_admin_access: Optional[bool] = None,
    ) -> Dict:
        if not drive_id:
            return {"success": False, "response": "drive_id is required."}
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if color_rgb is not None:
            body["colorRgb"] = color_rgb
        if not body:
            return {"success": False, "response": "Provide at least one field to update (name, color_rgb)."}
        params: Dict[str, Any] = {}
        if use_domain_admin_access is not None:
            params["useDomainAdminAccess"] = bool(use_domain_admin_access)
        updated = await self._drive_request("PUT", f"/drives/{drive_id}", json_body=body, params=params or None)
        if not updated:
            return {"success": False, "response": "Could not update the shared drive."}
        return {"success": True, "response": f"Shared drive updated: {updated.get('name')} ({updated.get('id')}).", "drive": updated}

    @tool(
        description=(
            "Permanently delete a shared drive (Team Drive). "
            "The shared drive must be empty (contain no files or folders) and the user must be "
            "an organizer. This action cannot be undone."
        ),
        params={
            "drive_id": "The ID of the shared drive to delete. Required.",
            "use_domain_admin_access": "If true, the user is acting as a domain administrator. Default false.",
        },
    )
    async def delete_drive(self, drive_id: str, use_domain_admin_access: Optional[bool] = None) -> Dict:
        if not drive_id:
            return {"success": False, "response": "drive_id is required."}
        params: Dict[str, Any] = {}
        if use_domain_admin_access is not None:
            params["useDomainAdminAccess"] = bool(use_domain_admin_access)
        result = await self._drive_request("DELETE", f"/drives/{drive_id}", params=params or None)
        if result is None:
            return {"success": False, "response": "Could not delete the shared drive."}
        return {"success": True, "response": "Shared drive deleted.", "drive_id": drive_id}


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

def _clamp_page_size(value: Optional[Any], default: int, maximum: int = MAX_PAGE_SIZE) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        n = default
    return min(n, maximum)


def _build_file_body(
    *,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
    parents: Optional[List[str]] = None,
    description: Optional[str] = None,
    starred: Optional[bool] = None,
    trashed: Optional[bool] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if name is not None:
        body["title"] = str(name)
    if mime_type is not None:
        body["mimeType"] = str(mime_type)
    if description is not None:
        body["description"] = str(description)
    if parents:
        body["parents"] = [{"id": p} for p in parents if p]
    labels: Dict[str, Any] = {}
    if starred is not None:
        labels["starred"] = bool(starred)
    if trashed is not None:
        labels["trashed"] = bool(trashed)
    if labels:
        body["labels"] = labels
    return body


def _file_summary(f: Dict) -> Dict:
    return {
        "id": f.get("id"),
        "title": f.get("title"),
        "mimeType": f.get("mimeType"),
        "parents": [p.get("id") for p in (f.get("parents") or []) if p.get("id")],
        "trashed": (f.get("labels") or {}).get("trashed"),
        "starred": (f.get("labels") or {}).get("starred"),
        "createdDate": f.get("createdDate"),
        "modifiedDate": f.get("modifiedDate"),
        "webViewLink": f.get("webViewLink") or f.get("alternateLink"),
    }


def _format_file_summary(f: Dict) -> str:
    parts = [f"File: {f.get('title')} (id={f.get('id')}, type={f.get('mimeType')})."]
    if f.get("modifiedDate"):
        parts.append(f"Modified: {f.get('modifiedDate')}.")
    if f.get("webViewLink") or f.get("alternateLink"):
        parts.append(f"Link: {f.get('webViewLink') or f.get('alternateLink')}")
    return " ".join(parts)


def _format_file_created_response(f: Dict) -> str:
    parts = [f"Created: {f.get('title')} (id={f.get('id')}, type={f.get('mimeType')})."]
    if f.get("webViewLink") or f.get("alternateLink"):
        parts.append(f"Link: {f.get('webViewLink') or f.get('alternateLink')}")
    return " ".join(parts)


def _permission_summary(p: Dict) -> str:
    target = p.get("emailAddress") or p.get("domain") or p.get("type")
    return f"Permission {p.get('id')}: {p.get('role')} for {target} (type={p.get('type')})."


def _comment_summary(c: Dict) -> str:
    author = (c.get("author") or {}).get("displayName", "unknown")
    content = (c.get("content") or "")[:200]
    status = "resolved" if c.get("status") == "resolved" else "open"
    return f"Comment {c.get('commentId')} by {author} [{status}]: {content}"


def _reply_summary(r: Dict) -> str:
    author = (r.get("author") or {}).get("displayName", "unknown")
    content = (r.get("content") or "")[:200]
    verb = f" (verb={r.get('verb')})" if r.get("verb") else ""
    return f"Reply {r.get('replyId')} by {author}{verb}: {content}"
