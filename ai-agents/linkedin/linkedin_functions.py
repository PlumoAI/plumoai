"""
LinkedIn functions class for functions_wrapper plugin.

Each public @tool method maps to one or more LinkedIn REST API actions.
Reference: https://learn.microsoft.com/en-us/linkedin/marketing/

All requests require two headers on every call:
  Linkedin-Version: YYYYMM  (set to LINKEDIN_VERSION constant below)
  X-Restli-Protocol-Version: 2.0.0

Entity URN formats used throughout:
  Member:       urn:li:person:{id}
  Organization: urn:li:organization:{id}
  Post/Share:   urn:li:share:{id}  or  urn:li:ugcPost:{id}
  Activity:     urn:li:activity:{id} — a LinkedIn-internal thread id, distinct from the post's own
                share/ugcPost id (the numeric ids do NOT match). It only ever appears inside a
                comment's own 'object'/'commentUrn' field — never derive it from a post's id.
  Comment:      urn:li:comment:(urn:li:activity:{id},{comment_id})
  Event:        urn:li:event:{id}

socialActions endpoints (comments/reactions) target the post directly via its own share/ugcPost
URN (or a comment's composite commentUrn for nested replies/reactions-on-comments) — never an
urn:li:activity URN built from the post's id.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

LINKEDIN_API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_LEGACY_API_BASE = "https://api.linkedin.com/v2"
LINKEDIN_VERSION = "202608"
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


class LinkedInFunctions(ConnectedServiceToolAgent):
    """
    LinkedIn tool functions. Each @tool method is a LinkedIn API capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "LinkedIn: create and manage posts (text, article links, images, videos, documents, polls, reshares), "
        "read/write comments and nested comment threads, manage reactions on posts and comments, "
        "create and manage organization events, look up member profiles and organization pages, "
        "retrieve follower/network size for organizations, "
        "and pull member-level analytics — post performance, follower growth, and 1st-degree connection count."
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("LinkedInFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("LinkedInFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0)
        logger.debug("LinkedInFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _linkedin_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Linkedin-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._httpx_client is None or self._httpx_client.is_closed:
            self._httpx_client = httpx.AsyncClient(timeout=30.0)
        return self._httpx_client

    async def _linkedin_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{LINKEDIN_API_BASE}/{path.lstrip('/')}"
        headers = self._linkedin_headers(extra_headers)
        client = await self._get_client()

        try:
            resp = await client.request(method, url, headers=headers, json=json_body, params=params)
        except Exception as exc:
            logger.warning("LinkedIn request error: %s", exc)
            return None

        if resp.status_code == 401 and _retry_count == 0:
            refreshed = await self.refresh_access_token()
            if refreshed:
                return await self._linkedin_request(
                    method, path, json_body=json_body, params=params,
                    extra_headers=extra_headers, _retry_count=1,
                )
            return None

        if resp.status_code == 429 or resp.status_code >= 500:
            if _retry_count < _REQUEST_MAX_RETRIES:
                delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
                await asyncio.sleep(delay)
                return await self._linkedin_request(
                    method, path, json_body=json_body, params=params,
                    extra_headers=extra_headers, _retry_count=_retry_count + 1,
                )
            return None

        # LinkedIn's create endpoints (e.g. POST /posts) reply 201 with an EMPTY
        # body -- the new entity's URN comes back only in the "x-restli-id"
        # response header, never in JSON. Without surfacing it here, create_post
        # etc. have no way to report the created entity's id/URN to the caller.
        _restli_id = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")

        if resp.status_code == 204:
            result: Dict[str, Any] = {"success": True}
            if _restli_id:
                result["id"] = _restli_id
            return result

        try:
            data = resp.json()
        except Exception:
            result = {"_raw": resp.text, "_status": resp.status_code}
            if _restli_id:
                result["id"] = _restli_id
            return result

        if resp.status_code >= 400:
            logger.warning(
                "LinkedIn API error %s on %s %s: %s",
                resp.status_code, method, path, data,
            )
            if isinstance(data, dict):
                data.setdefault("_status", resp.status_code)
        elif isinstance(data, dict) and "id" not in data and _restli_id:
            data["id"] = _restli_id
        return data

    @staticmethod
    def _strip_none(d: Dict) -> Dict:
        return {k: v for k, v in d.items() if v is not None}

    @staticmethod
    def _err(msg: str) -> Dict[str, Any]:
        return {"success": False, "response": msg}

    @staticmethod
    def _api_error_message(data: Dict) -> Optional[str]:
        """If `data` is a LinkedIn error body (status >= 400 tagged by _linkedin_request), return a
        human-readable message. Returns None if `data` doesn't look like an error."""
        status = data.get("_status")
        if not status or int(status) < 400:
            return None
        detail = data.get("message") or data.get("_raw") or str(data)
        code = data.get("serviceErrorCode") or data.get("code")
        suffix = f" (code {code})" if code else ""
        return f"LinkedIn API error {status}{suffix}: {detail}"

    @staticmethod
    def _ok(data: Any, extra: Optional[Dict] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {"success": True, "response": data}
        if extra:
            result.update(extra)
        return result

    # ------------------------------------------------------------------
    # MEMBER / PROFILE
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Retrieve the authenticated member's own LinkedIn profile. "
            "Returns the person URN, first name, last name, email address, profile picture URL, and locale. "
            "Call this first to get the authenticated user's person URN (urn:li:person:{id}), "
            "which is required as the 'author_urn' when creating posts or comments on behalf of a member. "
            "Takes no parameters."
        ),
    )
    async def get_current_member(self) -> Dict[str, Any]:
        data = await self._linkedin_request("GET", "me")
        if data is None:
            return self._err("Failed to retrieve current member profile.")
        sub = data.get("sub") or data.get("id")
        urn = f"urn:li:person:{sub}" if sub else None
        return self._ok(data, {"person_urn": urn} if urn else None)

    @tool(
        description=(
            "Retrieve a LinkedIn organization (company page) by its numeric organization ID. "
            "Returns the organization's name, vanity name, description, industry, website, logo, "
            "staff count range, organization type, and full URN. "
            "Requires the authenticated member to be an ADMINISTRATOR of the organization for full fields. "
            "Non-admins receive only public fields: id, name, vanityName, localizedWebsite, logoV2, locations."
        ),
        params={
            "organization_id": (
                "Required. Numeric LinkedIn organization ID. "
                "Example: '79988552' (from linkedin.com/company/79988552). "
                "Do NOT include the 'urn:li:organization:' prefix — pass only the number."
            ),
        },
    )
    async def get_organization(self, organization_id: str) -> Dict[str, Any]:
        data = await self._linkedin_request("GET", f"organizations/{organization_id}")
        if data is None:
            return self._err(f"Failed to retrieve organization {organization_id}.")
        urn = data.get("$URN") or f"urn:li:organization:{organization_id}"
        return self._ok(data, {"organization_urn": urn})

    @tool(
        description=(
            "Look up a LinkedIn organization or school by its vanity name — "
            "the slug that appears in the LinkedIn URL after '/company/'. "
            "Example: the vanity name for 'https://www.linkedin.com/company/plumoaiinc' is 'plumoaiinc'. "
            "Also accepts a full LinkedIn company URL — the vanity name will be extracted automatically. "
            "Returns public fields: id, name, vanityName, localizedWebsite, logoV2, locations, "
            "and primaryOrganizationType (SCHOOL, BRAND, or NONE). "
            "Use the returned 'id' to build the organization URN: urn:li:organization:{id}. "
            "NOTE: This uses a non-admin lookup endpoint — works for any public organization, "
            "not just ones the authenticated member administrates."
        ),
        params={
            "vanity_name": (
                "Required. The organization's vanity name OR its full LinkedIn company URL. "
                "Vanity name: the slug after '/company/' in the URL. "
                "Example vanity names: 'microsoft', 'google', 'plumoaiinc'. "
                "Example full URL: 'https://www.linkedin.com/company/plumoaiinc'. "
                "If a full URL is provided, the vanity name is extracted automatically. "
                "Case-insensitive."
            ),
        },
    )
    async def find_organization_by_vanity_name(self, vanity_name: str) -> Dict[str, Any]:
        import re
        # Accept full LinkedIn URL — extract slug automatically
        url_match = re.search(r'linkedin\.com/company/([^/?#\s]+)', vanity_name)
        if url_match:
            vanity_name = url_match.group(1).rstrip('/')

        # /rest/organizations?q=vanityName works for non-admins and returns public fields
        data = await self._linkedin_request(
            "GET", "organizations",
            params={"q": "vanityName", "vanityName": vanity_name},
        )
        if data is None:
            return self._err(f"Failed to find organization with vanity name '{vanity_name}'.")
        api_error = self._api_error_message(data)
        if api_error:
            return self._err(api_error)
        elements = data.get("elements", [])
        if not elements:
            return self._err(
                f"No organization found with vanity name '{vanity_name}'. "
                f"Verify the slug from the LinkedIn URL (linkedin.com/company/{{slug}})."
            )
        total = data.get("paging", {}).get("total", len(elements))
        return self._ok(elements, {"total": total})

    @tool(
        description=(
            "Batch-retrieve multiple LinkedIn organizations by their numeric IDs in a single API call. "
            "Returns a results map keyed by organization ID, plus a statuses map showing the HTTP status "
            "for each ID (200 = success, 403 = not an admin, 404 = not found). "
            "More efficient than calling get_organization repeatedly when looking up several companies at once."
        ),
        params={
            "organization_ids": (
                "Required. Comma-separated list of numeric organization IDs to retrieve. "
                "Example: '79988552,27056405,12345678'. "
                "Maximum 20 IDs per call. Do NOT include 'urn:li:organization:' prefixes."
            ),
        },
    )
    async def batch_get_organizations(self, organization_ids: str) -> Dict[str, Any]:
        ids_str = ",".join(i.strip() for i in organization_ids.split(","))
        data = await self._linkedin_request(
            "GET", "organizations",
            params={"ids": f"List({ids_str})"},
        )
        if data is None:
            return self._err("Failed to batch-retrieve organizations.")
        return self._ok(data)

    @tool(
        description=(
            "Retrieve the total follower count for a LinkedIn organization page. "
            "Returns the number of LinkedIn members who follow the organization. "
            "This is the organization's first-degree network size on LinkedIn."
        ),
        params={
            "organization_id": (
                "Required. Numeric LinkedIn organization ID. "
                "Example: '79988552'. "
                "Do NOT include the 'urn:li:organization:' prefix — pass only the number."
            ),
        },
    )
    async def get_organization_follower_count(self, organization_id: str) -> Dict[str, Any]:
        urn_encoded = quote(f"urn:li:organization:{organization_id}", safe="")
        data = await self._linkedin_request(
            "GET", f"networkSizes/{urn_encoded}",
            params={"edgeType": "COMPANY_FOLLOWED_BY_MEMBER"},
        )
        if data is None:
            return self._err(f"Failed to retrieve follower count for organization {organization_id}.")
        return self._ok(data)

    # ------------------------------------------------------------------
    # POSTS
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Create and publish a new LinkedIn post on behalf of a member or organization page. "
            "Supports text-only posts, article/link previews, images, videos, documents, polls, and reshares. "
            "Call get_current_member first to obtain the authenticated member's person URN. "
            "For image/video/document posts, the media asset must be uploaded separately via the LinkedIn "
            "Assets API to obtain a media URN before calling this tool."
        ),
        params={
            "author_urn": (
                "Required. URN of the post author. "
                "For a member post use 'urn:li:person:{id}' — call get_current_member to get this. "
                "For an organization page post use 'urn:li:organization:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg' or 'urn:li:organization:79988552'."
            ),
            "text": (
                "Required. The main text body of the post. Plain text with optional line breaks. "
                "Maximum 3000 characters for member posts, 700 characters for organization posts. "
                "Example: 'Excited to share our latest product update!'"
            ),
            "visibility": (
                "Optional. Who can see the post. "
                "Accepted values: "
                "'PUBLIC' — visible to anyone including non-members (default); "
                "'CONNECTIONS' — visible only to 1st-degree connections of the author; "
                "'LOGGED_IN' — visible to all logged-in LinkedIn members; "
                "'CONTAINER' — visible in the organization feed only. "
                "Defaults to 'PUBLIC' if omitted."
            ),
            "content_type": (
                "Optional. Type of media content to attach to the post. "
                "Accepted values: "
                "'NONE' — text-only post (default); "
                "'ARTICLE' — share a URL with a link preview (requires article_url); "
                "'IMAGE' — attach an uploaded image (requires media_urn); "
                "'VIDEO' — attach an uploaded video (requires media_urn); "
                "'DOCUMENT' — attach an uploaded document/PDF (requires media_urn); "
                "'RESHARE' — reshare an existing post (requires reshare_urn). "
                "Defaults to 'NONE' if omitted."
            ),
            "article_url": (
                "Optional. URL to share as an article/link preview. "
                "Required when content_type is 'ARTICLE'. "
                "LinkedIn will auto-generate a preview with title, description, and thumbnail from the URL. "
                "Example: 'https://example.com/blog/my-post'."
            ),
            "article_title": (
                "Optional. Custom title to override the auto-generated article preview title. "
                "Only used when content_type is 'ARTICLE'. "
                "Example: 'Our Latest Product Update — Read More'."
            ),
            "article_description": (
                "Optional. Custom description to override the auto-generated article preview summary. "
                "Only used when content_type is 'ARTICLE'. "
                "Example: 'Learn how we improved performance by 40% in our latest release.'."
            ),
            "media_urn": (
                "Optional. URN of an already-uploaded media asset. "
                "Required when content_type is 'IMAGE', 'VIDEO', or 'DOCUMENT'. "
                "Obtain this by first registering and uploading the asset via the LinkedIn Assets API. "
                "Example: 'urn:li:digitalmediaAsset:C5622AQHcccXX12345'."
            ),
            "media_title": (
                "Optional. Display title for the media asset. "
                "Used when content_type is 'IMAGE', 'VIDEO', or 'DOCUMENT'. "
                "Example: 'Q3 Results Infographic'."
            ),
            "media_alt_text": (
                "Optional. Alt text for image accessibility. "
                "Only used when content_type is 'IMAGE'. Maximum 120 characters. "
                "Example: 'Bar chart showing 40% growth in Q3 revenue'."
            ),
            "reshare_urn": (
                "Optional. URN of the post to reshare. "
                "Required when content_type is 'RESHARE'. "
                "Example: 'urn:li:share:6900000000000000000' or 'urn:li:ugcPost:6900000000000000000'."
            ),
            "lifecycle_state": (
                "Optional. Publication state of the post. "
                "Accepted values: "
                "'PUBLISHED' — publish the post immediately (default); "
                "'DRAFT' — save as a draft without publishing (member posts only). "
                "Defaults to 'PUBLISHED' if omitted."
            ),
        },
    )
    async def create_post(
        self,
        author_urn: str,
        text: str,
        visibility: str = "PUBLIC",
        content_type: str = "NONE",
        article_url: Optional[str] = None,
        article_title: Optional[str] = None,
        article_description: Optional[str] = None,
        media_urn: Optional[str] = None,
        media_title: Optional[str] = None,
        media_alt_text: Optional[str] = None,
        reshare_urn: Optional[str] = None,
        lifecycle_state: str = "PUBLISHED",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "author": author_urn,
            "commentary": text,
            "visibility": visibility,
            "lifecycleState": lifecycle_state,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
        }

        ct = (content_type or "NONE").upper()
        if ct == "ARTICLE" and article_url:
            body["content"] = self._strip_none({
                "article": self._strip_none({
                    "source": article_url,
                    "title": article_title,
                    "description": article_description,
                }),
            })
        elif ct in ("IMAGE", "VIDEO", "DOCUMENT") and media_urn:
            media_obj: Dict[str, Any] = self._strip_none({
                "id": media_urn,
                "title": media_title,
                "altText": media_alt_text if ct == "IMAGE" else None,
            })
            body["content"] = {ct.lower(): media_obj}
        elif ct == "RESHARE" and reshare_urn:
            body["reshareContext"] = {"parent": reshare_urn}

        data = await self._linkedin_request("POST", "posts", json_body=body)
        if data is None:
            return self._err("Failed to create LinkedIn post.")
        post_id = data.get("id")
        return self._ok(data, {"post_id": post_id} if post_id else None)

    @tool(
        description=(
            "Retrieve a single LinkedIn post by its URN. "
            "Returns the post's full content including text, author, visibility, lifecycle state, "
            "media content (if any), creation time, last modified time, and engagement statistics. "
            "Accepts both urn:li:share:{id} and urn:li:ugcPost:{id} URN formats."
        ),
        params={
            "post_urn": (
                "Required. Full URN of the post to retrieve. "
                "Accepted formats: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Example: 'urn:li:share:6900000000000000000' or 'urn:li:ugcPost:6900000000000000000'. "
                "Obtain this from find_posts_by_author or find_posts_by_organization results."
            ),
        },
    )
    async def get_post(self, post_urn: str) -> Dict[str, Any]:
        encoded = quote(post_urn, safe="")
        data = await self._linkedin_request("GET", f"posts/{encoded}")
        if data is None:
            return self._err(f"Failed to retrieve post {post_urn}.")
        return self._ok(data)

    @tool(
        description=(
            "Batch-retrieve multiple LinkedIn posts by their URNs in a single API call. "
            "Returns a map of post URN to post data. "
            "More efficient than calling get_post repeatedly when you need several posts at once."
        ),
        params={
            "post_urns": (
                "Required. Comma-separated list of post URNs to retrieve. "
                "Each URN must be in the format 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Example: 'urn:li:share:6900000000000000001,urn:li:share:6900000000000000002'. "
                "Maximum 20 URNs per call."
            ),
        },
    )
    async def batch_get_posts(self, post_urns: str) -> Dict[str, Any]:
        ids = ",".join(quote(u.strip(), safe="") for u in post_urns.split(","))
        data = await self._linkedin_request("GET", "posts", params={"ids": f"List({ids})"})
        if data is None:
            return self._err("Failed to batch-retrieve posts.")
        return self._ok(data)

    @tool(
        description=(
            "List all posts authored by a specific LinkedIn member, sorted by creation time (newest first). "
            "Useful for auditing a member's post history or building a content feed. "
            "Call get_current_member first to obtain the authenticated user's person URN."
        ),
        params={
            "author_urn": (
                "Required. Person URN of the post author. "
                "Format: 'urn:li:person:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'. "
                "Call get_current_member to get the authenticated user's URN."
            ),
            "count": (
                "Optional. Number of posts to return per page. "
                "Minimum 1, maximum 50. Defaults to 10 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. "
                "Use 0 for the first page. Increment by 'count' to get subsequent pages. "
                "Example: start=0 for page 1, start=10 for page 2 (when count=10). "
                "Defaults to 0 if omitted."
            ),
        },
    )
    async def find_posts_by_author(
        self,
        author_urn: str,
        count: int = 10,
        start: int = 0,
    ) -> Dict[str, Any]:
        data = await self._linkedin_request(
            "GET", "posts",
            params=self._strip_none({"q": "author", "author": author_urn, "count": count, "start": start}),
        )
        if data is None:
            return self._err(f"Failed to retrieve posts for author {author_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "List posts published on a LinkedIn organization page feed, sorted by creation time (newest first). "
            "Returns posts authored by the organization. "
            "Requires the authenticated member to have r_organization_social or rw_organization_admin scope. "
            "Call get_organization to look up the organization's ID and URN."
        ),
        params={
            "organization_urn": (
                "Required. Organization URN of the company page. "
                "Format: 'urn:li:organization:{id}'. "
                "Example: 'urn:li:organization:79988552'. "
                "Call get_organization or find_organization_by_vanity_name to look up this URN."
            ),
            "count": (
                "Optional. Number of posts to return per page. "
                "Minimum 1, maximum 50. Defaults to 10 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. "
                "Use 0 for the first page. Increment by 'count' for subsequent pages. "
                "Defaults to 0 if omitted."
            ),
        },
    )
    async def find_posts_by_organization(
        self,
        organization_urn: str,
        count: int = 10,
        start: int = 0,
    ) -> Dict[str, Any]:
        data = await self._linkedin_request(
            "GET", "posts",
            params=self._strip_none({"q": "author", "author": organization_urn, "count": count, "start": start}),
        )
        if data is None:
            return self._err(f"Failed to retrieve posts for organization {organization_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Update the text, visibility, or lifecycle state of an existing LinkedIn post. "
            "Only the post author can update their own posts. "
            "Note: media content (images, videos, documents) cannot be changed after posting — "
            "only the commentary text, visibility, and lifecycle state can be modified. "
            "Provide only the fields you want to change; unspecified fields remain unchanged."
        ),
        params={
            "post_urn": (
                "Required. Full URN of the post to update. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Example: 'urn:li:share:6900000000000000000'."
            ),
            "text": (
                "Optional. New text/commentary for the post. "
                "Replaces the existing text entirely. "
                "Maximum 3000 characters for member posts, 700 for organization posts. "
                "Omit this parameter to leave the text unchanged."
            ),
            "visibility": (
                "Optional. New visibility setting for the post. "
                "Accepted values: 'PUBLIC', 'CONNECTIONS', 'LOGGED_IN', 'CONTAINER'. "
                "Omit this parameter to keep the current visibility."
            ),
            "lifecycle_state": (
                "Optional. New lifecycle state for the post. "
                "Use 'PUBLISHED' to publish a saved draft, or 'DRAFT' to unpublish back to draft. "
                "Omit this parameter to keep the current state."
            ),
        },
    )
    async def update_post(
        self,
        post_urn: str,
        text: Optional[str] = None,
        visibility: Optional[str] = None,
        lifecycle_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if text is not None:
            patch["commentary"] = text
        if visibility is not None:
            patch["visibility"] = visibility
        if lifecycle_state is not None:
            patch["lifecycleState"] = lifecycle_state
        if not patch:
            return self._err("No update fields provided. Specify at least one of: text, visibility, lifecycle_state.")
        encoded = quote(post_urn, safe="")
        data = await self._linkedin_request(
            "POST", f"posts/{encoded}",
            json_body={"patch": {"$set": patch}},
            extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )
        if data is None:
            return self._err(f"Failed to update post {post_urn}.")
        return self._ok({"post_urn": post_urn, "updated": True})

    @tool(
        description=(
            "Permanently delete a LinkedIn post. "
            "Only the post author or an administrator of the authoring organization can delete a post. "
            "This action is irreversible — all comments, reactions, and analytics data for the post will be lost."
        ),
        params={
            "post_urn": (
                "Required. Full URN of the post to delete. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Example: 'urn:li:share:6900000000000000000'. "
                "Warning: this action cannot be undone."
            ),
        },
    )
    async def delete_post(self, post_urn: str) -> Dict[str, Any]:
        encoded = quote(post_urn, safe="")
        data = await self._linkedin_request("DELETE", f"posts/{encoded}")
        if data is None:
            return self._err(f"Failed to delete post {post_urn}.")
        return self._ok({"post_urn": post_urn, "deleted": True})

    # ------------------------------------------------------------------
    # COMMENTS
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Retrieve a single top-level comment on a LinkedIn post by its comment ID. "
            "Returns the comment text, author URN, creation time, like count, and the activity URN it belongs to. "
            "Use list_comments_on_post first to discover comment IDs."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN, exactly as returned in the 'id' field by create_post, "
                "get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Do NOT convert this to a 'urn:li:activity:{id}' URN — LinkedIn's comments endpoint targets "
                "the share/ugcPost URN directly, and the numeric activity id (if you see one in a comment's "
                "'object' field) is a different identifier, not derivable from the post's own id. "
                "Example: 'urn:li:share:6900000000000000000'."
            ),
            "comment_id": (
                "Required. Numeric ID of the comment to retrieve. "
                "Example: '6900000000000000001'. "
                "Obtain this from list_comments_on_post results."
            ),
        },
    )
    async def get_comment(self, activity_urn: str, comment_id: str) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        data = await self._linkedin_request("GET", f"socialActions/{encoded_urn}/comments/{comment_id}")
        if data is None:
            return self._err(f"Failed to retrieve comment {comment_id}.")
        return self._ok(data)

    @tool(
        description=(
            "List all top-level comments on a LinkedIn post. "
            "Returns a paginated list of comments including author URN, comment text, "
            "creation time, like count, and reply count for each comment."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN, exactly as returned in the 'id' field by create_post, "
                "get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. Pass it through unchanged — "
                "do NOT convert it to a 'urn:li:activity:{id}' URN, that will return zero comments. "
                "Example: 'urn:li:share:6900000000000000000'."
            ),
            "count": (
                "Optional. Number of comments to return per page. "
                "Minimum 1, maximum 100. Defaults to 20 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. "
                "Use 0 for the first page. Increment by 'count' for subsequent pages. "
                "Defaults to 0 if omitted."
            ),
        },
    )
    async def list_comments_on_post(
        self,
        activity_urn: str,
        count: int = 20,
        start: int = 0,
    ) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        data = await self._linkedin_request(
            "GET", f"socialActions/{encoded_urn}/comments",
            params={"count": count, "start": start},
        )
        if data is None:
            return self._err(f"Failed to list comments on {activity_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "List nested replies (sub-comments) on a specific top-level comment on a LinkedIn post. "
            "LinkedIn supports one level of nesting — replies to top-level comments only, not replies to replies."
        ),
        params={
            "activity_urn": (
                "Required. The true activity URN of the parent post — must match the 'object' field "
                "returned on the parent comment by list_comments_on_post or get_comment "
                "(format 'urn:li:activity:{id}'). This is a distinct identifier assigned by LinkedIn — "
                "it is NOT the same numeric id as the post's own 'urn:li:share:{id}'/'urn:li:ugcPost:{id}' "
                "URN, so do not pass the post's own id here. Example: 'urn:li:activity:6900000000000000000'."
            ),
            "parent_comment_id": (
                "Required. Numeric ID of the top-level comment whose replies to list. "
                "Example: '6900000000000000001'. "
                "Obtain this from list_comments_on_post results."
            ),
            "count": (
                "Optional. Number of replies to return per page. "
                "Minimum 1, maximum 100. Defaults to 20 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. Defaults to 0 if omitted."
            ),
        },
    )
    async def list_nested_comments(
        self,
        activity_urn: str,
        parent_comment_id: str,
        count: int = 20,
        start: int = 0,
    ) -> Dict[str, Any]:
        comment_urn = f"urn:li:comment:({activity_urn},{parent_comment_id})"
        encoded_urn = quote(comment_urn, safe="")
        data = await self._linkedin_request(
            "GET", f"socialActions/{encoded_urn}/comments",
            params={"count": count, "start": start},
        )
        if data is None:
            return self._err(f"Failed to list replies on comment {parent_comment_id}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Batch-retrieve multiple LinkedIn comments by their comment URNs in a single API call. "
            "More efficient than calling get_comment repeatedly when you need several comments at once."
        ),
        params={
            "comment_urns": (
                "Required. Comma-separated list of comment URNs to retrieve. "
                "Each URN must be in the format 'urn:li:comment:(urn:li:activity:{id},{comment_id})'. "
                "Example: 'urn:li:comment:(urn:li:activity:690000001,690000002),"
                "urn:li:comment:(urn:li:activity:690000001,690000003)'. "
                "Maximum 20 URNs per call."
            ),
        },
    )
    async def batch_get_comments(self, comment_urns: str) -> Dict[str, Any]:
        ids = ",".join(quote(u.strip(), safe="") for u in comment_urns.split(","))
        data = await self._linkedin_request(
            "GET", "socialActions",
            params={"ids": f"List({ids})", "projection": "(elements*(comments*))"},
        )
        if data is None:
            return self._err("Failed to batch-retrieve comments.")
        return self._ok(data)

    @tool(
        description=(
            "Post a new top-level comment on a LinkedIn post. "
            "The comment is authored by the specified member or organization. "
            "Supports plain text comments up to 1250 characters."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN to comment on, exactly as returned in the 'id' field by "
                "create_post, get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. Do NOT convert to a "
                "'urn:li:activity:{id}' URN. Example: 'urn:li:share:6900000000000000000'."
            ),
            "author_urn": (
                "Required. URN of the comment author. "
                "For a member use 'urn:li:person:{id}' — call get_current_member to get this. "
                "For an organization page use 'urn:li:organization:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'."
            ),
            "text": (
                "Required. The comment text. Plain text only — no HTML or markdown. "
                "Maximum 1250 characters. "
                "Example: 'Great insights! Thanks for sharing this.'."
            ),
        },
    )
    async def create_comment(
        self,
        activity_urn: str,
        author_urn: str,
        text: str,
    ) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        body = {"actor": author_urn, "message": {"text": text, "attributes": []}}
        data = await self._linkedin_request("POST", f"socialActions/{encoded_urn}/comments", json_body=body)
        if data is None:
            return self._err(f"Failed to create comment on {activity_urn}.")
        return self._ok(data)

    @tool(
        description=(
            "Post a nested reply to an existing top-level comment on a LinkedIn post. "
            "LinkedIn supports one level of nesting only — you can reply to a top-level comment "
            "but not to a reply. "
            "The reply author can be a member or an organization the member administrates."
        ),
        params={
            "activity_urn": (
                "Required. The true activity URN of the parent post — this is combined with "
                "parent_comment_id to build the reply's parentComment reference, so it must match the "
                "'object' field returned on the parent comment by list_comments_on_post or get_comment "
                "(format 'urn:li:activity:{id}'). This is a distinct identifier assigned by LinkedIn — it "
                "is NOT the same numeric id as the post's own 'urn:li:share:{id}'/'urn:li:ugcPost:{id}' URN, "
                "so do not pass the post's own id here. Example: 'urn:li:activity:6900000000000000000'."
            ),
            "parent_comment_id": (
                "Required. Numeric ID of the top-level comment to reply to. "
                "Example: '6900000000000000001'. "
                "Obtain this from list_comments_on_post results."
            ),
            "post_urn": (
                "Required. The parent post's own URN — the same value used to look up its comments via "
                "list_comments_on_post, exactly as returned in the 'id' field by create_post, get_post, "
                "find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'."
            ),
            "author_urn": (
                "Required. URN of the reply author. "
                "For a member use 'urn:li:person:{id}'. For an org use 'urn:li:organization:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'."
            ),
            "text": (
                "Required. The reply text. Plain text only. "
                "Maximum 1250 characters. "
                "Example: 'Thanks for your comment! Happy to elaborate further.'."
            ),
        },
    )
    async def create_nested_comment(
        self,
        activity_urn: str,
        parent_comment_id: str,
        post_urn: str,
        author_urn: str,
        text: str,
    ) -> Dict[str, Any]:
        parent_comment_urn = f"urn:li:comment:({activity_urn},{parent_comment_id})"
        encoded_urn = quote(parent_comment_urn, safe="")
        body = {
            "actor": author_urn,
            "object": post_urn,
            "message": {"text": text, "attributes": []},
            "parentComment": parent_comment_urn,
        }
        data = await self._linkedin_request(
            "POST", f"socialActions/{encoded_urn}/comments",
            json_body=body,
        )
        if data is None:
            return self._err(f"Failed to create reply on comment {parent_comment_id}.")
        return self._ok(data)

    @tool(
        description=(
            "Edit the text of an existing LinkedIn comment. "
            "Only the comment author can edit their own comments. "
            "The edit replaces the full comment text — partial edits are not supported."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN, exactly as returned in the 'id' field by create_post, "
                "get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. Do NOT convert to a "
                "'urn:li:activity:{id}' URN. Example: 'urn:li:share:6900000000000000000'."
            ),
            "comment_id": (
                "Required. Numeric ID of the comment to edit. "
                "Example: '6900000000000000001'. "
                "Obtain this from list_comments_on_post results."
            ),
            "text": (
                "Required. New text for the comment. Completely replaces the existing text. "
                "Plain text only, maximum 1250 characters. "
                "Example: 'Updated: Great insights — especially point 3!'."
            ),
        },
    )
    async def edit_comment(
        self,
        activity_urn: str,
        comment_id: str,
        text: str,
    ) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        body = {"patch": {"$set": {"message": {"text": text, "attributes": []}}}}
        data = await self._linkedin_request(
            "POST", f"socialActions/{encoded_urn}/comments/{comment_id}",
            json_body=body,
            extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )
        if data is None:
            return self._err(f"Failed to edit comment {comment_id}.")
        return self._ok({"comment_id": comment_id, "updated": True})

    @tool(
        description=(
            "Permanently delete a LinkedIn comment. "
            "Only the comment author or an admin of the post's organization can delete a comment. "
            "Deleting a top-level comment also deletes all its nested replies."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN, exactly as returned in the 'id' field by create_post, "
                "get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. Do NOT convert to a "
                "'urn:li:activity:{id}' URN. Example: 'urn:li:share:6900000000000000000'."
            ),
            "comment_id": (
                "Required. Numeric ID of the comment to delete. "
                "Example: '6900000000000000001'. "
                "Warning: this also deletes all nested replies to this comment."
            ),
        },
    )
    async def delete_comment(self, activity_urn: str, comment_id: str) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        data = await self._linkedin_request("DELETE", f"socialActions/{encoded_urn}/comments/{comment_id}")
        if data is None:
            return self._err(f"Failed to delete comment {comment_id}.")
        return self._ok({"comment_id": comment_id, "deleted": True})

    # ------------------------------------------------------------------
    # REACTIONS
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List all reactions on a LinkedIn post or comment. "
            "Returns a paginated list of reactions including each reactor's person URN, "
            "reaction type, and creation timestamp. "
            "Reaction types: LIKE, PRAISE, APPRECIATION, EMPATHY, INTEREST, ENTERTAINMENT."
        ),
        params={
            "entity_urn": (
                "Required. URN of the post or comment to list reactions on. "
                "For a post, use its own URN exactly as returned in the 'id' field by create_post/get_post/"
                "find_posts_by_author/find_posts_by_organization — 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Do NOT convert a post's id to 'urn:li:activity:{id}'. "
                "For a comment, use the exact 'commentUrn' value returned by list_comments_on_post or "
                "get_comment — format 'urn:li:comment:(urn:li:activity:{id},{comment_id})'. "
                "Example post: 'urn:li:share:6900000000000000000'. "
                "Example comment: 'urn:li:comment:(urn:li:activity:690000001,690000002)'."
            ),
            "count": (
                "Optional. Number of reactions to return per page. "
                "Minimum 1, maximum 100. Defaults to 20 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. Defaults to 0 if omitted."
            ),
        },
    )
    async def list_reactions_on_entity(
        self,
        entity_urn: str,
        count: int = 20,
        start: int = 0,
    ) -> Dict[str, Any]:
        encoded_urn = quote(entity_urn, safe="")
        data = await self._linkedin_request(
            "GET", f"socialActions/{encoded_urn}/reactions",
            params={"count": count, "start": start},
        )
        if data is None:
            return self._err(f"Failed to list reactions on {entity_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Retrieve a specific reaction by a member on a LinkedIn post or comment. "
            "Returns the reaction type and creation time if the member has reacted, "
            "or an empty result if they have not reacted."
        ),
        params={
            "entity_urn": (
                "Required. URN of the post or comment to check. "
                "For a post, use its own URN exactly as returned in the 'id' field by create_post/get_post/"
                "find_posts_by_author/find_posts_by_organization — 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Do NOT convert a post's id to 'urn:li:activity:{id}'. "
                "For a comment, use the exact 'commentUrn' value returned by list_comments_on_post or "
                "get_comment — format 'urn:li:comment:(urn:li:activity:{id},{comment_id})'. "
                "Example: 'urn:li:share:6900000000000000000'."
            ),
            "reactor_urn": (
                "Required. Person URN of the member whose reaction to look up. "
                "Format: 'urn:li:person:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'. "
                "Call get_current_member to get the authenticated user's URN."
            ),
        },
    )
    async def get_reaction(self, entity_urn: str, reactor_urn: str) -> Dict[str, Any]:
        encoded_urn = quote(entity_urn, safe="")
        encoded_reactor = quote(reactor_urn, safe="")
        data = await self._linkedin_request("GET", f"socialActions/{encoded_urn}/reactions/{encoded_reactor}")
        if data is None:
            return self._err(f"Failed to retrieve reaction by {reactor_urn} on {entity_urn}.")
        return self._ok(data)

    @tool(
        description=(
            "Add a reaction to a LinkedIn post on behalf of the authenticated member or organization. "
            "Each member/organization can only have one active reaction per post — "
            "calling this again with a different type replaces the existing reaction. "
            "Reaction types: LIKE (general approval), PRAISE (celebrate achievement), "
            "APPRECIATION (thank someone), EMPATHY (support for difficult news), "
            "INTEREST (show curiosity), ENTERTAINMENT (found it amusing)."
        ),
        params={
            "activity_urn": (
                "Required. The post's own URN to react to, exactly as returned in the 'id' field by "
                "create_post, get_post, find_posts_by_author, or find_posts_by_organization. "
                "Format: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. Do NOT convert to a "
                "'urn:li:activity:{id}' URN. Example: 'urn:li:share:6900000000000000000'."
            ),
            "actor_urn": (
                "Required. URN of the member or organization adding the reaction. "
                "For a member use 'urn:li:person:{id}'. For an org use 'urn:li:organization:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'."
            ),
            "reaction_type": (
                "Optional. Type of reaction to add. "
                "Accepted values: 'LIKE', 'PRAISE', 'APPRECIATION', 'EMPATHY', 'INTEREST', 'ENTERTAINMENT'. "
                "Defaults to 'LIKE' if omitted."
            ),
        },
    )
    async def create_reaction_on_post(
        self,
        activity_urn: str,
        actor_urn: str,
        reaction_type: str = "LIKE",
    ) -> Dict[str, Any]:
        encoded_urn = quote(activity_urn, safe="")
        body = {"actor": actor_urn, "reactionType": reaction_type.upper()}
        data = await self._linkedin_request("POST", f"socialActions/{encoded_urn}/reactions", json_body=body)
        if data is None:
            return self._err(f"Failed to add reaction to {activity_urn}.")
        return self._ok({"activity_urn": activity_urn, "reaction_type": reaction_type, "reacted": True})

    @tool(
        description=(
            "Add a reaction to a LinkedIn comment on behalf of the authenticated member. "
            "Each member can only have one active reaction per comment — "
            "calling this again replaces the existing reaction. "
            "Reaction types: LIKE, PRAISE, APPRECIATION, EMPATHY, INTEREST, ENTERTAINMENT."
        ),
        params={
            "activity_urn": (
                "Required. The true activity URN of the post containing the comment — this is used to "
                "build the comment's composite URN and must match the 'object' field returned on the "
                "comment by list_comments_on_post or get_comment (format 'urn:li:activity:{id}'). "
                "This activity id is a distinct identifier assigned by LinkedIn — it is NOT the same "
                "numeric id as the post's own 'urn:li:share:{id}'/'urn:li:ugcPost:{id}' URN, so do not "
                "pass the post's own id here. Example: 'urn:li:activity:6900000000000000000'."
            ),
            "comment_id": (
                "Required. Numeric ID of the comment to react to. "
                "Example: '6900000000000000001'. "
                "Obtain this from list_comments_on_post results."
            ),
            "actor_urn": (
                "Required. Person URN of the member adding the reaction. "
                "Format: 'urn:li:person:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'."
            ),
            "reaction_type": (
                "Optional. Type of reaction to add. "
                "Accepted values: 'LIKE', 'PRAISE', 'APPRECIATION', 'EMPATHY', 'INTEREST', 'ENTERTAINMENT'. "
                "Defaults to 'LIKE' if omitted."
            ),
        },
    )
    async def create_reaction_on_comment(
        self,
        activity_urn: str,
        comment_id: str,
        actor_urn: str,
        reaction_type: str = "LIKE",
    ) -> Dict[str, Any]:
        comment_urn = f"urn:li:comment:({activity_urn},{comment_id})"
        encoded_comment_urn = quote(comment_urn, safe="")
        body = {"actor": actor_urn, "reactionType": reaction_type.upper()}
        data = await self._linkedin_request("POST", f"socialActions/{encoded_comment_urn}/reactions", json_body=body)
        if data is None:
            return self._err(f"Failed to add reaction to comment {comment_id}.")
        return self._ok({"comment_id": comment_id, "reaction_type": reaction_type, "reacted": True})

    @tool(
        description=(
            "Remove a reaction from a LinkedIn post or comment by the authenticated member. "
            "After deletion, the member can add a new reaction of a different type."
        ),
        params={
            "entity_urn": (
                "Required. URN of the post or comment to remove the reaction from. "
                "For a post, use its own URN exactly as returned in the 'id' field by create_post/get_post/"
                "find_posts_by_author/find_posts_by_organization — 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Do NOT convert a post's id to 'urn:li:activity:{id}'. "
                "For a comment, use the exact 'commentUrn' value returned by list_comments_on_post or "
                "get_comment — format 'urn:li:comment:(urn:li:activity:{id},{comment_id})'. "
                "Example: 'urn:li:share:6900000000000000000'."
            ),
            "actor_urn": (
                "Required. Person URN of the member whose reaction to remove. "
                "Format: 'urn:li:person:{id}'. "
                "Example: 'urn:li:person:ACoAABcdefg'."
            ),
        },
    )
    async def delete_reaction(self, entity_urn: str, actor_urn: str) -> Dict[str, Any]:
        encoded_urn = quote(entity_urn, safe="")
        encoded_actor = quote(actor_urn, safe="")
        data = await self._linkedin_request("DELETE", f"socialActions/{encoded_urn}/reactions/{encoded_actor}")
        if data is None:
            return self._err(f"Failed to delete reaction by {actor_urn} on {entity_urn}.")
        return self._ok({"entity_urn": entity_urn, "deleted": True})

    # ------------------------------------------------------------------
    # EVENTS
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Create a new event on a LinkedIn organization page. "
            "Events can be online (virtual/webinar) or in-person. "
            "Published events appear on the organization's LinkedIn page. "
            "The authenticated member must be an ADMINISTRATOR of the organizing organization. "
            "All times must be Unix timestamps in milliseconds (epoch ms). "
            "Call get_organization to look up the organization URN."
        ),
        params={
            "organizer_urn": (
                "Required. URN of the organization hosting the event. "
                "Format: 'urn:li:organization:{id}'. "
                "Example: 'urn:li:organization:79988552'. "
                "The authenticated member must be an ADMINISTRATOR of this organization."
            ),
            "name": (
                "Required. Name/title of the event. "
                "Maximum 200 characters. "
                "Example: 'Q3 Product Webinar' or 'Annual Company Meetup 2026'."
            ),
            "start_time": (
                "Required. Event start date and time as a Unix timestamp in milliseconds (epoch ms). "
                "Example: 1750000000000 (approx. June 15, 2025 UTC). "
                "Use an online epoch converter to obtain this value from a human-readable datetime."
            ),
            "end_time": (
                "Required. Event end date and time as a Unix timestamp in milliseconds. "
                "Must be after start_time. "
                "Example: 1750003600000 (1 hour after the start_time example above)."
            ),
            "description": (
                "Optional. Detailed description of the event. Plain text. "
                "Maximum 5000 characters. "
                "Example: 'Join us for a live demo of our new product features and a Q&A session.'."
            ),
            "timezone": (
                "Optional. IANA timezone identifier for the event. "
                "Used for display purposes on the LinkedIn event page. "
                "Example: 'America/New_York', 'Europe/London', 'Asia/Karachi'. "
                "Defaults to 'UTC' if omitted."
            ),
            "event_type": (
                "Optional. Format of the event. "
                "Accepted values: "
                "'ONLINE' — virtual/webinar event with a join URL (default); "
                "'IN_PERSON' — physical location event. "
                "Defaults to 'ONLINE' if omitted."
            ),
            "online_event_url": (
                "Optional. URL for joining the online event (e.g., Zoom, Teams, or Google Meet link). "
                "Recommended when event_type is 'ONLINE'. "
                "Example: 'https://zoom.us/j/123456789'."
            ),
            "venue_name": (
                "Optional. Name of the physical venue. "
                "Used when event_type is 'IN_PERSON'. "
                "Example: 'Grand Hyatt New York'."
            ),
            "venue_address": (
                "Optional. Full street address of the event venue. "
                "Used when event_type is 'IN_PERSON'. "
                "Example: '109 E 42nd St, New York, NY 10017'."
            ),
            "venue_city": (
                "Optional. City where the event takes place. "
                "Used when event_type is 'IN_PERSON'. "
                "Example: 'New York'."
            ),
            "venue_country": (
                "Optional. Two-letter ISO 3166-1 alpha-2 country code of the venue. "
                "Used when event_type is 'IN_PERSON'. "
                "Example: 'US', 'GB', 'PK'."
            ),
            "broadcast_type": (
                "Optional. Controls who can see and attend the event. "
                "Accepted values: "
                "'EXTERNAL' — public, visible to all LinkedIn members (default); "
                "'INTERNAL' — private, visible only to organization members. "
                "Defaults to 'EXTERNAL' if omitted."
            ),
        },
    )
    async def create_event(
        self,
        organizer_urn: str,
        name: str,
        start_time: int,
        end_time: int,
        description: Optional[str] = None,
        timezone: str = "UTC",
        event_type: str = "ONLINE",
        online_event_url: Optional[str] = None,
        venue_name: Optional[str] = None,
        venue_address: Optional[str] = None,
        venue_city: Optional[str] = None,
        venue_country: Optional[str] = None,
        broadcast_type: str = "EXTERNAL",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = self._strip_none({
            "organizer": organizer_urn,
            "name": {"localized": {"en_US": name}, "preferredLocale": {"country": "US", "language": "en"}},
            "description": {"localized": {"en_US": description}, "preferredLocale": {"country": "US", "language": "en"}} if description else None,
            "startAt": start_time,
            "endAt": end_time,
            "timeZoneId": timezone,
            "broadcastType": broadcast_type,
        })

        if event_type.upper() == "IN_PERSON":
            body["eventType"] = "IN_PERSON"
            venue: Dict[str, Any] = self._strip_none({
                "name": venue_name, "address1": venue_address,
                "city": venue_city, "countryCode": venue_country,
            })
            if venue:
                body["venue"] = venue
        else:
            body["eventType"] = "ONLINE"
            if online_event_url:
                body["onlineEvent"] = {"joinLink": online_event_url}

        data = await self._linkedin_request("POST", "events", json_body=body)
        if data is None:
            return self._err("Failed to create LinkedIn event.")
        event_id = data.get("id")
        urn = f"urn:li:event:{event_id}" if event_id else None
        return self._ok(data, {"event_urn": urn} if urn else None)

    @tool(
        description=(
            "Retrieve a LinkedIn event by its numeric event ID. "
            "Returns full event details including name, description, start/end times, event type, "
            "venue (for in-person events), online join URL (for online events), "
            "organizer URN, and the event's vanity URL on LinkedIn."
        ),
        params={
            "event_id": (
                "Required. Numeric LinkedIn event ID. "
                "Example: '12345678'. "
                "Do NOT include the 'urn:li:event:' prefix — pass only the number. "
                "Obtain this from create_event or list_events_by_organizer results."
            ),
        },
    )
    async def get_event(self, event_id: str) -> Dict[str, Any]:
        data = await self._linkedin_request("GET", f"events/{event_id}")
        if data is None:
            return self._err(f"Failed to retrieve event {event_id}.")
        return self._ok(data)

    @tool(
        description=(
            "List all LinkedIn events hosted by an organization, sorted by start time. "
            "Returns upcoming and past events for the organization page. "
            "Requires the authenticated member to be an administrator of the organization."
        ),
        params={
            "organizer_urn": (
                "Required. Organization URN of the event host. "
                "Format: 'urn:li:organization:{id}'. "
                "Example: 'urn:li:organization:79988552'. "
                "Call get_organization to look up this URN."
            ),
            "count": (
                "Optional. Number of events to return per page. "
                "Minimum 1, maximum 50. Defaults to 10 if omitted."
            ),
            "start": (
                "Optional. Zero-based offset for pagination. Defaults to 0 if omitted."
            ),
        },
    )
    async def list_events_by_organizer(
        self,
        organizer_urn: str,
        count: int = 10,
        start: int = 0,
    ) -> Dict[str, Any]:
        data = await self._linkedin_request(
            "GET", "events",
            params={"q": "organizer", "organizer": organizer_urn, "count": count, "start": start},
        )
        if data is None:
            return self._err(f"Failed to list events for organizer {organizer_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Update the details of an existing LinkedIn event. "
            "Only the organizing member or an organization administrator can update an event. "
            "Provide only the parameters you want to change — all unspecified fields remain unchanged."
        ),
        params={
            "event_id": (
                "Required. Numeric LinkedIn event ID to update. "
                "Example: '12345678'. "
                "Do NOT include the 'urn:li:event:' prefix."
            ),
            "name": (
                "Optional. New event name/title. Maximum 200 characters. "
                "Example: 'Q3 Product Webinar — Updated Schedule'. "
                "Omit to keep the current name."
            ),
            "description": (
                "Optional. New event description. Plain text, maximum 5000 characters. "
                "Omit to keep the current description."
            ),
            "start_time": (
                "Optional. New event start time as a Unix timestamp in milliseconds. "
                "Example: 1750000000000. Omit to keep the current start time."
            ),
            "end_time": (
                "Optional. New event end time as a Unix timestamp in milliseconds. Must be after start_time. "
                "Omit to keep the current end time."
            ),
            "timezone": (
                "Optional. New IANA timezone identifier. "
                "Example: 'America/New_York', 'Europe/London'. "
                "Omit to keep the current timezone."
            ),
            "online_event_url": (
                "Optional. New URL for the online event join link. "
                "Example: 'https://zoom.us/j/987654321'. "
                "Omit to keep the current join URL."
            ),
            "broadcast_type": (
                "Optional. New broadcast type. "
                "Accepted values: 'EXTERNAL' (public) or 'INTERNAL' (org members only). "
                "Omit to keep the current broadcast type."
            ),
        },
    )
    async def update_event(
        self,
        event_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        timezone: Optional[str] = None,
        online_event_url: Optional[str] = None,
        broadcast_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if name:
            patch["name"] = {"localized": {"en_US": name}, "preferredLocale": {"country": "US", "language": "en"}}
        if description:
            patch["description"] = {"localized": {"en_US": description}, "preferredLocale": {"country": "US", "language": "en"}}
        if start_time is not None:
            patch["startAt"] = start_time
        if end_time is not None:
            patch["endAt"] = end_time
        if timezone:
            patch["timeZoneId"] = timezone
        if online_event_url:
            patch["onlineEvent"] = {"joinLink": online_event_url}
        if broadcast_type:
            patch["broadcastType"] = broadcast_type
        if not patch:
            return self._err("No update fields provided. Specify at least one optional parameter to update.")
        data = await self._linkedin_request(
            "POST", f"events/{event_id}",
            json_body={"patch": {"$set": patch}},
            extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
        )
        if data is None:
            return self._err(f"Failed to update event {event_id}.")
        return self._ok({"event_id": event_id, "updated": True})

    @tool(
        description=(
            "Permanently delete a LinkedIn event. "
            "Only the organizing member or an administrator of the organizing organization can delete an event. "
            "Deleted events are immediately removed from LinkedIn and cannot be recovered."
        ),
        params={
            "event_id": (
                "Required. Numeric LinkedIn event ID to delete. "
                "Example: '12345678'. "
                "Do NOT include the 'urn:li:event:' prefix. "
                "Warning: this action cannot be undone."
            ),
        },
    )
    async def delete_event(self, event_id: str) -> Dict[str, Any]:
        data = await self._linkedin_request("DELETE", f"events/{event_id}")
        if data is None:
            return self._err(f"Failed to delete event {event_id}.")
        return self._ok({"event_id": event_id, "deleted": True})

    # ------------------------------------------------------------------
    # ANALYTICS & NETWORK SIZE
    # ------------------------------------------------------------------

    @staticmethod
    def _build_date_range(start_date: Optional[str], end_date: Optional[str]) -> Optional[str]:
        if not start_date and not end_date:
            return None

        def _part(date_str: str) -> str:
            y, m, d = date_str.split("-")
            return f"(day:{int(d)},month:{int(m)},year:{int(y)})"

        parts = []
        if start_date:
            parts.append(f"start:{_part(start_date)}")
        if end_date:
            parts.append(f"end:{_part(end_date)}")
        return "(" + ",".join(parts) + ")"

    @tool(
        description=(
            "Retrieve performance analytics for a single post authored by the authenticated member "
            "(impressions, unique viewers reached, reshares, reactions, comments, saves, sends, link clicks, "
            "and follower/profile-view lift attributed to the post). "
            "Requires the r_member_postAnalytics scope. Only works for posts authored by a member — "
            "not organization posts. Call get_current_member and find_posts_by_author first to locate the post."
        ),
        params={
            "post_urn": (
                "Required. URN of the member's post to retrieve analytics for. "
                "Accepted formats: 'urn:li:share:{id}' or 'urn:li:ugcPost:{id}'. "
                "Example: 'urn:li:share:7325786486870552578'."
            ),
            "metric": (
                "Optional. Which analytics metric to retrieve. "
                "Accepted values: 'IMPRESSION', 'MEMBERS_REACHED', 'RESHARE', 'REACTION', 'COMMENT', "
                "'POST_SAVE', 'POST_SEND', 'LINK_CLICKS', 'PREMIUM_CTA_CLICKS', "
                "'FOLLOWER_GAINED_FROM_CONTENT', 'PROFILE_VIEW_FROM_CONTENT'. "
                "Defaults to 'IMPRESSION' if omitted."
            ),
            "aggregation": (
                "Optional. How to bucket the returned data. "
                "'TOTAL' — a single data point with the lifetime (or date-range) total (default). "
                "'DAILY' — one data point per day. Not supported for MEMBERS_REACHED, LINK_CLICKS, "
                "FOLLOWER_GAINED_FROM_CONTENT, or PROFILE_VIEW_FROM_CONTENT."
            ),
            "start_date": (
                "Optional. Inclusive start date for the analytics window, format 'YYYY-MM-DD'. "
                "Omit together with end_date to get lifetime analytics for the post."
            ),
            "end_date": (
                "Optional. Exclusive end date for the analytics window, format 'YYYY-MM-DD'. "
                "Omit together with start_date to get lifetime analytics for the post."
            ),
        },
    )
    async def get_member_post_analytics(
        self,
        post_urn: str,
        metric: str = "IMPRESSION",
        aggregation: str = "TOTAL",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        entity_type = "ugc" if "ugcPost" in post_urn else "share"
        params: Dict[str, Any] = {
            "q": "entity",
            "entity": f"({entity_type}:{post_urn})",
            "queryType": metric.upper(),
            "aggregation": aggregation.upper(),
        }
        date_range = self._build_date_range(start_date, end_date)
        if date_range:
            params["dateRange"] = date_range
        data = await self._linkedin_request("GET", "memberCreatorPostAnalytics", params=params)
        if data is None:
            return self._err(f"Failed to retrieve post analytics for {post_urn}.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Retrieve aggregated performance analytics across all of the authenticated member's posts "
            "for a given metric (e.g. total impressions or reactions across every post they've published). "
            "Requires the r_member_postAnalytics scope."
        ),
        params={
            "metric": (
                "Optional. Which analytics metric to aggregate. "
                "Accepted values: 'IMPRESSION', 'MEMBERS_REACHED', 'RESHARE', 'REACTION', 'COMMENT', "
                "'POST_SAVE', 'POST_SEND', 'LINK_CLICKS', 'PREMIUM_CTA_CLICKS', "
                "'FOLLOWER_GAINED_FROM_CONTENT', 'PROFILE_VIEW_FROM_CONTENT'. "
                "Defaults to 'IMPRESSION' if omitted."
            ),
            "aggregation": (
                "Optional. 'TOTAL' — a single data point with the summed total (default). "
                "'DAILY' — one data point per day. Not supported for MEMBERS_REACHED, LINK_CLICKS, "
                "FOLLOWER_GAINED_FROM_CONTENT, or PROFILE_VIEW_FROM_CONTENT."
            ),
            "start_date": (
                "Optional. Inclusive start date, format 'YYYY-MM-DD'. "
                "Omit together with end_date to get lifetime totals across all posts."
            ),
            "end_date": (
                "Optional. Exclusive end date, format 'YYYY-MM-DD'. "
                "Omit together with start_date to get lifetime totals across all posts."
            ),
        },
    )
    async def get_member_aggregated_post_analytics(
        self,
        metric: str = "IMPRESSION",
        aggregation: str = "TOTAL",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "q": "me",
            "queryType": metric.upper(),
            "aggregation": aggregation.upper(),
        }
        date_range = self._build_date_range(start_date, end_date)
        if date_range:
            params["dateRange"] = date_range
        data = await self._linkedin_request("GET", "memberCreatorPostAnalytics", params=params)
        if data is None:
            return self._err("Failed to retrieve aggregated post analytics.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Retrieve the authenticated member's LinkedIn follower count — lifetime total, or a "
            "day-by-day breakdown over a date range. Requires the r_member_profileAnalytics scope."
        ),
        params={
            "start_date": (
                "Optional. Inclusive start date for a daily breakdown, format 'YYYY-MM-DD'. "
                "Omit both start_date and end_date to get the lifetime follower total instead."
            ),
            "end_date": (
                "Optional. Exclusive end date for a daily breakdown, format 'YYYY-MM-DD'. "
                "Omit both start_date and end_date to get the lifetime follower total instead."
            ),
        },
    )
    async def get_member_follower_statistics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        date_range = self._build_date_range(start_date, end_date)
        params = {"q": "dateRange", "dateRange": date_range} if date_range else {"q": "me"}
        data = await self._linkedin_request("GET", "memberFollowersCount", params=params)
        if data is None:
            return self._err("Failed to retrieve member follower statistics.")
        elements = data.get("elements", [])
        return self._ok(elements, {"paging": data.get("paging"), "total": len(elements)})

    @tool(
        description=(
            "Retrieve the number of 1st-degree connections in the authenticated member's LinkedIn network. "
            "Requires the r_1st_connections_size scope. Only the currently authenticated member's own "
            "connection count can be retrieved — this cannot look up other members. "
            "Call get_current_member first to obtain the person URN."
        ),
        params={
            "person_urn": (
                "Required. Person URN of the authenticated member. "
                "Format: 'urn:li:person:{id}'. "
                "Call get_current_member to get this — it must be the currently authenticated member's own URN."
            ),
        },
    )
    async def get_connections_size(self, person_urn: str) -> Dict[str, Any]:
        encoded = quote(person_urn, safe="")
        url = f"{LINKEDIN_LEGACY_API_BASE}/connections/{encoded}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
        }
        client = await self._get_client()
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 401:
                if await self.refresh_access_token():
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    resp = await client.get(url, headers=headers)
        except Exception as exc:
            logger.warning("LinkedIn connections size request error: %s", exc)
            return self._err(f"Failed to retrieve connections size for {person_urn}.")

        if resp.status_code != 200:
            return self._err(
                f"Failed to retrieve connections size for {person_urn} (status {resp.status_code})."
            )
        try:
            data = resp.json()
        except Exception:
            return self._err(f"Failed to parse connections size response for {person_urn}.")
        return self._ok(data)
