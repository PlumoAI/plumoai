"""
YouTube functions class for functions_wrapper plugin.

Each public @tool method maps to one or more YouTube Data API v3 actions.
Reference: https://developers.google.com/youtube/v3/docs

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].

Notes:
- Most "list" endpoints are paginated: pass the returned next_page_token back in as
  page_token to fetch the next page.
- Most "update" endpoints in this file first fetch the current resource and merge in
  only the fields you provide, so omitted fields are preserved rather than reset.
- video_id / playlist_id / channel_id / comment_id values can be obtained from
  get_id_from_url (if you have a YouTube URL), or from the "id" fields returned by
  search, get_videos, list_playlists, list_playlist_items, get_channel, etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


def _missing_params(**kwargs: Any) -> List[str]:
    """Return the names of any required parameters whose value is None, in call order."""
    return [name for name, value in kwargs.items() if value is None]


def _missing_params_error(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """If any of the given required parameters is None, return an error dict naming them; else None."""
    missing = _missing_params(**kwargs)
    if missing:
        return {"success": False, "response": f"Missing required parameter(s): {', '.join(missing)}."}
    return None


def _api_error_detail(data: Optional[Dict]) -> Optional[str]:
    """Extract a human-readable error message from a YouTube API error response, if any."""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            status = err.get("status")
            if not message:
                errors = err.get("errors") or []
                if errors and isinstance(errors[0], dict):
                    message = errors[0].get("message")
                    status = status or errors[0].get("reason")
            if message and status:
                return f"{message} ({status})"
            return message or status
    return None


def _is_error(data: Optional[Dict]) -> bool:
    """True if data represents a failed YouTube API request (no data, or a Google error body)."""
    return data is None or _api_error_detail(data) is not None


def _error_response(data: Optional[Dict], fallback: str, hint: str = "") -> Dict[str, Any]:
    """Build a {"success": False, "response": ...} error dict that surfaces the real Google API
    error message (if any) alongside a fallback summary and an optional hint for what to try next."""
    detail = _api_error_detail(data)
    msg = f"{fallback} Google API error: {detail}" if detail else fallback
    if hint:
        msg = f"{msg} {hint}"
    return {"success": False, "response": msg}


def _video_placeholder(video_id: Optional[str], title: Optional[str] = None, channel_title: Optional[str] = None) -> Optional[str]:
    """Build a `[[YOUTUBE_VIDEO_PLACEHOLDER {...}]]` token for a video reference, so the frontend
    can render a rich video embed/card without any YouTube-specific logic in the main agent brain.
    Returns None if video_id is not set."""
    if not video_id:
        return None
    payload: Dict[str, Any] = {"videoId": video_id}
    if title:
        payload["title"] = title
    if channel_title:
        payload["channelTitle"] = channel_title
    return "[[YOUTUBE_VIDEO_PLACEHOLDER " + json.dumps(payload, separators=(",", ":")) + "]]"


class YouTubeFunctions(ConnectedServiceToolAgent):
    """
    YouTube tool functions. Each @tool method is a YouTube Data API v3 capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "YouTube: search videos/channels/playlists; get/update/delete videos; rate videos; "
        "manage playlists and playlist items; read and post comments and replies; manage "
        "subscriptions; and manage caption tracks. Uses the YouTube Data API v3."
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
        raise NotImplementedError("YouTubeFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("YouTubeFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("YouTubeFunctions initialized")

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

    async def _youtube_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{YOUTUBE_API_BASE}{path}" if path.startswith("/") else f"{YOUTUBE_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._youtube_request(
                method, path, json_body=json_body, params=params, retry_401=False,
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "YouTube API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._youtube_request(
                method, path, json_body=json_body, params=params,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("YouTube API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("YouTube API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            try:
                error_data = r.json()
            except Exception:
                error_data = None
            if isinstance(error_data, dict) and isinstance(error_data.get("error"), dict):
                return error_data
            return {"error": {"code": r.status_code, "message": (r.text or "")[:500] or f"HTTP {r.status_code}", "status": "UNKNOWN"}}
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("YouTube API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # @tool public methods — URL helper
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Extract a video ID, playlist ID, channel ID, and/or handle from a YouTube URL. "
            "Most other tools need a bare ID (not the full URL) — use this first when the user "
            "gives you a link such as 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', "
            "'https://youtu.be/dQw4w9WgXcQ', 'https://www.youtube.com/playlist?list=PL...', "
            "'https://www.youtube.com/channel/UC...', or 'https://www.youtube.com/@SomeHandle'."
        ),
        params={
            "url": "A YouTube URL, or a bare video/playlist/channel ID, which is returned as-is. Required.",
        },
    )
    async def get_id_from_url(self, url: str) -> Dict:
        if err := _missing_params_error(url=url):
            return err
        video_id = _extract_video_id(url)
        playlist_id = _extract_playlist_id(url)
        channel_id = _extract_channel_id(url)
        handle = _extract_handle(url)
        if not any([video_id, playlist_id, channel_id, handle]):
            return {"success": False, "response": "Could not find a video, playlist, channel ID, or handle in that URL."}
        parts = []
        if video_id:
            parts.append(f"video_id = {video_id}")
        if playlist_id:
            parts.append(f"playlist_id = {playlist_id}")
        if channel_id:
            parts.append(f"channel_id = {channel_id}")
        if handle:
            parts.append(f"handle = {handle} (call get_channel(for_handle='{handle}') to resolve its channel_id)")
        result = {
            "success": True,
            "response": ", ".join(parts) + ".",
            "video_id": video_id,
            "playlist_id": playlist_id,
            "channel_id": channel_id,
            "handle": handle,
        }
        if video_id:
            result["Embeded_Video"] = _video_placeholder(video_id)
        return result

    # ------------------------------------------------------------------
    # @tool public methods — Search
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Search YouTube for videos, channels, and/or playlists matching a query. "
            "Returns matching items with their IDs (video_id/channel_id/playlist_id), titles, "
            "descriptions, and thumbnails — these IDs feed into get_videos, get_channel, "
            "list_playlist_items, etc."
        ),
        params={
            "query": "Search query text. Required.",
            "search_type": (
                "Comma-separated list of resource types to search for: 'video', 'channel', "
                "'playlist'. Default 'video'."
            ),
            "channel_id": "Restrict results to videos/playlists uploaded by this channel ID.",
            "order": (
                "Sort order for results. One of: 'relevance' (default), 'date', 'rating', "
                "'title', 'videoCount', 'viewCount'."
            ),
            "max_results": "Maximum number of results to return (1-50). Default 10.",
            "page_token": "Pagination token from a previous search call's next_page_token.",
            "published_after": "Only return results published after this RFC 3339 datetime, e.g. '2026-01-01T00:00:00Z'.",
            "published_before": "Only return results published before this RFC 3339 datetime.",
            "video_duration": "For video searches: 'any' (default), 'short' (<4 min), 'medium' (4-20 min), or 'long' (>20 min).",
            "region_code": "ISO 3166-1 alpha-2 country code to bias results toward, e.g. 'US'.",
            "safe_search": "Content filtering level: 'moderate' (default), 'none', or 'strict'.",
        },
    )
    async def search(
        self,
        query: str,
        search_type: Optional[str] = None,
        channel_id: Optional[str] = None,
        order: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
        published_after: Optional[str] = None,
        published_before: Optional[str] = None,
        video_duration: Optional[str] = None,
        region_code: Optional[str] = None,
        safe_search: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(query=query):
            return err
        params: Dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": search_type or "video",
            "maxResults": int(max_results) if max_results else 10,
        }
        if channel_id:
            params["channelId"] = channel_id
        if order:
            params["order"] = order
        if page_token:
            params["pageToken"] = page_token
        if published_after:
            params["publishedAfter"] = published_after
        if published_before:
            params["publishedBefore"] = published_before
        if video_duration:
            params["videoDuration"] = video_duration
        if region_code:
            params["regionCode"] = region_code
        if safe_search:
            params["safeSearch"] = safe_search
        data = await self._youtube_request("GET", "/search", params=params)
        if _is_error(data):
            return _error_response(data, "Search failed.", "Check that query and any filters (channel_id, search_type, etc.) are valid.")
        items = data.get("items") or []
        results = [_search_result_summary(item) for item in items]
        response = f"Found {len(results)} result(s)."
        if results:
            response += " Next: pass a video_id to get_videos for full details, a channel_id to get_channel, or a playlist_id to list_playlist_items."
        return {
            "success": True,
            "response": response,
            "results": results,
            "next_page_token": data.get("nextPageToken"),
        }

    # ------------------------------------------------------------------
    # @tool public methods — Videos
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Get full details for one or more videos by ID: title, description, tags, channel, "
            "publish date, duration, view/like/comment counts, and privacy status."
        ),
        params={
            "video_ids": "List of video IDs to look up, e.g. ['dQw4w9WgXcQ']. Up to 50. Required.",
            "parts": (
                "List of resource parts to include. Default ['snippet', 'contentDetails', "
                "'statistics', 'status']. Other options: 'player', 'topicDetails', 'recordingDetails', "
                "'liveStreamingDetails'."
            ),
        },
    )
    async def get_videos(self, video_ids: List[str], parts: Optional[List[str]] = None) -> Dict:
        if err := _missing_params_error(video_ids=video_ids):
            return err
        params = {
            "part": ",".join(parts) if parts else "snippet,contentDetails,statistics,status",
            "id": ",".join(video_ids),
        }
        data = await self._youtube_request("GET", "/videos", params=params)
        if _is_error(data):
            return _error_response(data, "Could not get video details.", "Check that the video IDs are correct and public (or that you own them).")
        items = data.get("items") or []
        if not items:
            return {"success": True, "response": "No videos found for the given ID(s).", "videos": [], "count": 0}
        videos = [_video_summary(v) for v in items]
        return {
            "success": True,
            "response": f"Found {len(items)} video(s).",
            "videos": videos,
            "count": len(items),
        }

    @tool(
        description=(
            "Update a video's metadata: title, description, tags, category, language, privacy "
            "status, or made-for-kids flag. Only the fields you provide are changed — the current "
            "values for everything else are preserved."
        ),
        params={
            "video_id": "The ID of the video to update. Required.",
            "title": "New title for the video.",
            "description": "New description for the video.",
            "tags": "New list of tags/keywords for the video, e.g. ['demo', 'tutorial'].",
            "category_id": "New numeric YouTube video category ID (from list_video_categories).",
            "default_language": "New default language for the title/description, as a BCP-47 code, e.g. 'en'.",
            "privacy_status": "New privacy status: 'public', 'private', or 'unlisted'.",
            "made_for_kids": "If true, marks the video as made for kids; if false, marks it as not made for kids.",
        },
    )
    async def update_video(
        self,
        video_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        category_id: Optional[str] = None,
        default_language: Optional[str] = None,
        privacy_status: Optional[str] = None,
        made_for_kids: Optional[bool] = None,
    ) -> Dict:
        if err := _missing_params_error(video_id=video_id):
            return err
        wants_snippet = any(v is not None for v in (title, description, tags, category_id, default_language))
        wants_status = any(v is not None for v in (privacy_status, made_for_kids))
        if not wants_snippet and not wants_status:
            return {"success": False, "response": "Provide at least one field to update (title, description, tags, category_id, default_language, privacy_status, made_for_kids)."}
        parts_needed = []
        if wants_snippet:
            parts_needed.append("snippet")
        if wants_status:
            parts_needed.append("status")
        current = await self._youtube_request("GET", "/videos", params={"part": ",".join(parts_needed), "id": video_id})
        if _is_error(current) or not (current.get("items") or []):
            return _error_response(current, "Could not load the current video to update.", "Check that video_id is correct and that you own this video.")
        resource = current["items"][0]
        if wants_snippet:
            snippet = resource.get("snippet") or {}
            if title is not None:
                snippet["title"] = title
            if description is not None:
                snippet["description"] = description
            if tags is not None:
                snippet["tags"] = tags
            if category_id is not None:
                snippet["categoryId"] = str(category_id)
            if default_language is not None:
                snippet["defaultLanguage"] = default_language
            resource["snippet"] = snippet
        if wants_status:
            status = resource.get("status") or {}
            if privacy_status is not None:
                status["privacyStatus"] = privacy_status
            if made_for_kids is not None:
                status["selfDeclaredMadeForKids"] = bool(made_for_kids)
            resource["status"] = status
        body = {"id": video_id}
        if wants_snippet:
            body["snippet"] = resource["snippet"]
        if wants_status:
            body["status"] = resource["status"]
        data = await self._youtube_request("PUT", "/videos", params={"part": ",".join(parts_needed)}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not update the video.", "Check that video_id is correct and that you own this video.")
        video = _video_summary(data)
        return {
            "success": True,
            "response": f"Video {video_id} updated. Next: call get_videos(video_ids=['{video_id}']) to verify the new metadata.",
            "video": video,
        }

    @tool(
        description="Permanently delete a video that you own. This action cannot be undone.",
        params={"video_id": "The ID of the video to delete. Required."},
    )
    async def delete_video(self, video_id: str) -> Dict:
        if err := _missing_params_error(video_id=video_id):
            return err
        data = await self._youtube_request("DELETE", "/videos", params={"id": video_id})
        if _is_error(data):
            return _error_response(data, "Could not delete the video.", "Check that video_id is correct and that you own this video.")
        return {"success": True, "response": f"Video {video_id} deleted.", "video_id": video_id}

    @tool(
        description="Like, dislike, or remove your rating from a video.",
        params={
            "video_id": "The ID of the video to rate. Required.",
            "rating": "Rating to apply: 'like', 'dislike', or 'none' (removes your existing rating). Required.",
        },
    )
    async def rate_video(self, video_id: str, rating: str) -> Dict:
        if err := _missing_params_error(video_id=video_id, rating=rating):
            return err
        data = await self._youtube_request("POST", "/videos/rate", params={"id": video_id, "rating": rating})
        if _is_error(data):
            return _error_response(data, "Could not rate the video.", "rating must be one of 'like', 'dislike', or 'none'.")
        return {"success": True, "response": f"Set rating '{rating}' on video {video_id}.", "video_id": video_id, "rating": rating}

    @tool(
        description="Get your own rating ('like', 'dislike', or 'none') for one or more videos.",
        params={"video_ids": "List of video IDs to check. Required."},
    )
    async def get_video_rating(self, video_ids: List[str]) -> Dict:
        if err := _missing_params_error(video_ids=video_ids):
            return err
        data = await self._youtube_request("GET", "/videos/getRating", params={"id": ",".join(video_ids)})
        if _is_error(data):
            return _error_response(data, "Could not get video rating(s).")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Got rating(s) for {len(items)} video(s).",
            "ratings": [{"video_id": i.get("videoId"), "rating": i.get("rating")} for i in items],
        }

    @tool(
        description=(
            "List the official YouTube video categories (e.g. 'Music', 'Gaming', 'Education') "
            "for a region, with their numeric IDs. Use these IDs with update_video's category_id."
        ),
        params={"region_code": "ISO 3166-1 alpha-2 country code, e.g. 'US'. Default 'US'."},
    )
    async def list_video_categories(self, region_code: Optional[str] = None) -> Dict:
        data = await self._youtube_request("GET", "/videoCategories", params={"part": "snippet", "regionCode": region_code or "US"})
        if _is_error(data):
            return _error_response(data, "Could not list video categories.")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} categor(ies).",
            "categories": [{"id": c.get("id"), "title": (c.get("snippet") or {}).get("title")} for c in items],
        }

    # ------------------------------------------------------------------
    # @tool public methods — Channels
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Get details for a YouTube channel: title, description, subscriber/video/view counts, "
            "and (if it's your own channel) the IDs of its 'uploads' playlist and other related "
            "playlists. Provide exactly one of channel_id, for_handle, for_username, or mine=true."
        ),
        params={
            "channel_id": "The channel's ID (starts with 'UC...').",
            "for_handle": "The channel's @handle (with or without the leading '@'), e.g. '@GoogleDevelopers'.",
            "for_username": "The channel's legacy username.",
            "mine": "If true, returns the authenticated user's own channel. Ignores other parameters.",
            "parts": (
                "List of resource parts to include. Default ['snippet', 'contentDetails', 'statistics']. "
                "Other options: 'brandingSettings', 'status', 'topicDetails'."
            ),
        },
    )
    async def get_channel(
        self,
        channel_id: Optional[str] = None,
        for_handle: Optional[str] = None,
        for_username: Optional[str] = None,
        mine: Optional[bool] = None,
        parts: Optional[List[str]] = None,
    ) -> Dict:
        if not any([channel_id, for_handle, for_username, mine]):
            return {"success": False, "response": "Provide one of channel_id, for_handle, for_username, or mine=true."}
        params: Dict[str, Any] = {"part": ",".join(parts) if parts else "snippet,contentDetails,statistics"}
        if mine:
            params["mine"] = "true"
        elif channel_id:
            params["id"] = channel_id
        elif for_handle:
            handle = for_handle if for_handle.startswith("@") else f"@{for_handle}"
            params["forHandle"] = handle
        elif for_username:
            params["forUsername"] = for_username
        data = await self._youtube_request("GET", "/channels", params=params)
        if _is_error(data):
            return _error_response(data, "Could not get channel details.")
        items = data.get("items") or []
        if not items:
            return {"success": False, "response": "No channel found for the given identifier."}
        channel = items[0]
        uploads_playlist = (((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads"))
        response = f"Channel '{(channel.get('snippet') or {}).get('title')}' (id={channel.get('id')})."
        if uploads_playlist:
            response += f" Next: call list_playlist_items(playlist_id='{uploads_playlist}') to list its uploaded videos."
        return {
            "success": True,
            "response": response,
            "channel": _channel_summary(channel),
        }

    # ------------------------------------------------------------------
    # @tool public methods — Playlists
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List playlists owned by a channel or the authenticated user, or look up specific "
            "playlists by ID."
        ),
        params={
            "channel_id": "List playlists owned by this channel.",
            "mine": "If true, list the authenticated user's own playlists.",
            "playlist_ids": "List of specific playlist IDs to look up.",
            "max_results": "Maximum number of results to return (1-50). Default 25.",
            "page_token": "Pagination token from a previous list_playlists call's next_page_token.",
        },
    )
    async def list_playlists(
        self,
        channel_id: Optional[str] = None,
        mine: Optional[bool] = None,
        playlist_ids: Optional[List[str]] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not any([channel_id, mine, playlist_ids]):
            return {"success": False, "response": "Provide one of channel_id, mine=true, or playlist_ids."}
        params: Dict[str, Any] = {"part": "snippet,contentDetails,status", "maxResults": int(max_results) if max_results else 25}
        if playlist_ids:
            params["id"] = ",".join(playlist_ids)
        elif mine:
            params["mine"] = "true"
        elif channel_id:
            params["channelId"] = channel_id
        if page_token:
            params["pageToken"] = page_token
        data = await self._youtube_request("GET", "/playlists", params=params)
        if _is_error(data):
            return _error_response(data, "Could not list playlists.")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} playlist(s).",
            "playlists": [_playlist_summary(p) for p in items],
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Create a new playlist on the authenticated user's channel. Returns the new "
            "playlist's ID, needed by add_playlist_item and other playlist tools."
        ),
        params={
            "title": "Title for the new playlist. Required.",
            "description": "Optional description for the playlist.",
            "privacy_status": "Privacy status: 'private' (default), 'public', or 'unlisted'.",
            "tags": "Optional list of keyword tags for the playlist.",
        },
    )
    async def create_playlist(
        self,
        title: str,
        description: Optional[str] = None,
        privacy_status: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict:
        if err := _missing_params_error(title=title):
            return err
        snippet: Dict[str, Any] = {"title": title}
        if description is not None:
            snippet["description"] = description
        if tags:
            snippet["tags"] = tags
        body = {"snippet": snippet, "status": {"privacyStatus": privacy_status or "private"}}
        data = await self._youtube_request("POST", "/playlists", params={"part": "snippet,status"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not create the playlist.")
        return {
            "success": True,
            "response": (
                f"Created playlist '{title}' (id={data.get('id')}). "
                f"Next: call add_playlist_item(playlist_id='{data.get('id')}', video_id=...) to add videos to it."
            ),
            "playlist": _playlist_summary(data),
        }

    @tool(
        description=(
            "Update a playlist's title, description, privacy status, or tags. Only the fields "
            "you provide are changed — current values for everything else are preserved."
        ),
        params={
            "playlist_id": "The ID of the playlist to update. Required.",
            "title": "New title for the playlist.",
            "description": "New description for the playlist.",
            "privacy_status": "New privacy status: 'private', 'public', or 'unlisted'.",
            "tags": "New list of keyword tags for the playlist.",
        },
    )
    async def update_playlist(
        self,
        playlist_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        privacy_status: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict:
        if err := _missing_params_error(playlist_id=playlist_id):
            return err
        if all(v is None for v in (title, description, privacy_status, tags)):
            return {"success": False, "response": "Provide at least one field to update (title, description, privacy_status, tags)."}
        current = await self._youtube_request("GET", "/playlists", params={"part": "snippet,status", "id": playlist_id})
        if _is_error(current) or not (current.get("items") or []):
            return _error_response(current, "Could not load the current playlist to update.", "Check that playlist_id is correct and that you own this playlist.")
        resource = current["items"][0]
        snippet = resource.get("snippet") or {}
        if title is not None:
            snippet["title"] = title
        if description is not None:
            snippet["description"] = description
        if tags is not None:
            snippet["tags"] = tags
        resource["snippet"] = snippet
        if privacy_status is not None:
            status = resource.get("status") or {}
            status["privacyStatus"] = privacy_status
            resource["status"] = status
        body = {"id": playlist_id, "snippet": resource["snippet"], "status": resource.get("status")}
        data = await self._youtube_request("PUT", "/playlists", params={"part": "snippet,status"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not update the playlist.", "Check that playlist_id is correct and that you own this playlist.")
        return {"success": True, "response": f"Playlist {playlist_id} updated.", "playlist": _playlist_summary(data)}

    @tool(
        description="Permanently delete a playlist that you own. This action cannot be undone.",
        params={"playlist_id": "The ID of the playlist to delete. Required."},
    )
    async def delete_playlist(self, playlist_id: str) -> Dict:
        if err := _missing_params_error(playlist_id=playlist_id):
            return err
        data = await self._youtube_request("DELETE", "/playlists", params={"id": playlist_id})
        if _is_error(data):
            return _error_response(data, "Could not delete the playlist.", "Check that playlist_id is correct and that you own this playlist.")
        return {"success": True, "response": f"Playlist {playlist_id} deleted.", "playlist_id": playlist_id}

    # ------------------------------------------------------------------
    # @tool public methods — Playlist items
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the videos in a playlist, in order, with their playlist-item IDs "
            "(needed by update_playlist_item / delete_playlist_item) and positions."
        ),
        params={
            "playlist_id": "The ID of the playlist to list items from. Required.",
            "video_id": "Optional: restrict results to a specific video within the playlist.",
            "max_results": "Maximum number of results to return (1-50). Default 25.",
            "page_token": "Pagination token from a previous list_playlist_items call's next_page_token.",
        },
    )
    async def list_playlist_items(
        self,
        playlist_id: str,
        video_id: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(playlist_id=playlist_id):
            return err
        params: Dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": int(max_results) if max_results else 25,
        }
        if video_id:
            params["videoId"] = video_id
        if page_token:
            params["pageToken"] = page_token
        data = await self._youtube_request("GET", "/playlistItems", params=params)
        if _is_error(data):
            return _error_response(data, "Could not list playlist items.", "Check that playlist_id is correct (use get_id_from_url if you only have a URL).")
        items = data.get("items") or []
        playlist_items = [_playlist_item_summary(i) for i in items]
        response = f"Found {len(playlist_items)} item(s) in playlist {playlist_id}."
        video_tokens = _video_placeholders(playlist_items, id_key="videoId")
        if video_tokens:
            response += " " + video_tokens
        return {
            "success": True,
            "response": response,
            "items": playlist_items,
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Add a video to a playlist, optionally at a specific position.",
        params={
            "playlist_id": "The ID of the playlist to add to. Required.",
            "video_id": "The ID of the video to add. Required.",
            "position": "0-based position in the playlist for the new item. If omitted, it is added at the end.",
            "note": "Optional user-visible note to attach to this playlist item.",
        },
    )
    async def add_playlist_item(
        self,
        playlist_id: str,
        video_id: str,
        position: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(playlist_id=playlist_id, video_id=video_id):
            return err
        snippet: Dict[str, Any] = {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
        if position is not None:
            snippet["position"] = int(position)
        if note is not None:
            snippet["note"] = note
        data = await self._youtube_request("POST", "/playlistItems", params={"part": "snippet"}, json_body={"snippet": snippet})
        if _is_error(data):
            return _error_response(data, "Could not add the video to the playlist.", "Check that playlist_id and video_id are correct and that you own the playlist.")
        item = _playlist_item_summary(data)
        return {
            "success": True,
            "response": f"Added video {video_id} to playlist {playlist_id} (playlist item id={data.get('id')}).",
            "item": item,
        }

    @tool(
        description=(
            "Update a playlist item's position within its playlist, and/or its note. "
            "Only the fields you provide are changed."
        ),
        params={
            "playlist_item_id": "The ID of the playlist item to update (from list_playlist_items). Required.",
            "playlist_id": "The ID of the playlist containing this item. Required.",
            "video_id": "The video ID this item points to. Required (the API requires resourceId to be re-specified).",
            "position": "New 0-based position for this item within the playlist.",
            "note": "New user-visible note for this item.",
        },
    )
    async def update_playlist_item(
        self,
        playlist_item_id: str,
        playlist_id: str,
        video_id: str,
        position: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Dict:
        if err := _missing_params_error(playlist_item_id=playlist_item_id, playlist_id=playlist_id, video_id=video_id):
            return err
        if position is None and note is None:
            return {"success": False, "response": "Provide at least one field to update (position, note)."}
        snippet: Dict[str, Any] = {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
        if position is not None:
            snippet["position"] = int(position)
        if note is not None:
            snippet["note"] = note
        body = {"id": playlist_item_id, "snippet": snippet}
        data = await self._youtube_request("PUT", "/playlistItems", params={"part": "snippet"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not update the playlist item.", "Check that playlist_item_id, playlist_id, and video_id are all correct — call list_playlist_items to confirm current values.")
        item = _playlist_item_summary(data)
        return {"success": True, "response": f"Playlist item {playlist_item_id} updated.", "item": item}

    @tool(
        description="Remove an item from a playlist (does not delete the underlying video).",
        params={"playlist_item_id": "The ID of the playlist item to remove (from list_playlist_items). Required."},
    )
    async def delete_playlist_item(self, playlist_item_id: str) -> Dict:
        if err := _missing_params_error(playlist_item_id=playlist_item_id):
            return err
        data = await self._youtube_request("DELETE", "/playlistItems", params={"id": playlist_item_id})
        if _is_error(data):
            return _error_response(data, "Could not remove the playlist item.", "Check that playlist_item_id is correct — call list_playlist_items to confirm.")
        return {"success": True, "response": f"Playlist item {playlist_item_id} removed.", "playlist_item_id": playlist_item_id}

    # ------------------------------------------------------------------
    # @tool public methods — Comments
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List top-level comment threads on a video (or all comment threads referencing a "
            "channel). Each thread includes the top-level comment and a reply count — use "
            "list_comments with the thread's id to fetch replies."
        ),
        params={
            "video_id": "List comment threads on this video.",
            "channel_id": "List comment threads for this channel's videos (channel-level comments).",
            "max_results": "Maximum number of results to return (1-100). Default 20.",
            "page_token": "Pagination token from a previous call's next_page_token.",
            "order": "Sort order: 'time' (default, newest first) or 'relevance'.",
            "search_terms": "Only return threads containing this text.",
            "moderation_status": "Filter by moderation status: 'published' (default), 'heldForReview', 'likelySpam', or 'rejected'. Requires being the video/channel owner for anything other than 'published'.",
        },
    )
    async def list_comment_threads(
        self,
        video_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
        order: Optional[str] = None,
        search_terms: Optional[str] = None,
        moderation_status: Optional[str] = None,
    ) -> Dict:
        if not any([video_id, channel_id]):
            return {"success": False, "response": "Provide video_id and/or channel_id."}
        params: Dict[str, Any] = {"part": "snippet", "maxResults": int(max_results) if max_results else 20}
        if video_id:
            params["videoId"] = video_id
        if channel_id:
            params["allThreadsRelatedToChannelId"] = channel_id
        if page_token:
            params["pageToken"] = page_token
        if order:
            params["order"] = order
        if search_terms:
            params["searchTerms"] = search_terms
        if moderation_status:
            params["moderationStatus"] = moderation_status
        data = await self._youtube_request("GET", "/commentThreads", params=params)
        if _is_error(data):
            return _error_response(data, "Could not list comment threads.", "Check that video_id/channel_id is correct, and that comments are enabled on this video.")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} comment thread(s).",
            "threads": [_comment_thread_summary(t) for t in items],
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Post a new top-level comment on a video (or on a channel's discussion).",
        params={
            "video_id": "Post the comment on this video. Provide this or channel_id.",
            "channel_id": "Post the comment on this channel's discussion. Ignored if video_id is given.",
            "text": "The comment text. Required.",
        },
    )
    async def insert_comment_thread(self, text: str, video_id: Optional[str] = None, channel_id: Optional[str] = None) -> Dict:
        if err := _missing_params_error(text=text):
            return err
        if not any([video_id, channel_id]):
            return {"success": False, "response": "Provide video_id and/or channel_id."}
        snippet: Dict[str, Any] = {"topLevelComment": {"snippet": {"textOriginal": text}}}
        if video_id:
            snippet["videoId"] = video_id
        if channel_id:
            snippet["channelId"] = channel_id
        data = await self._youtube_request("POST", "/commentThreads", params={"part": "snippet"}, json_body={"snippet": snippet})
        if _is_error(data):
            return _error_response(data, "Could not post the comment.", "Check that video_id/channel_id is correct and that comments are enabled.")
        return {
            "success": True,
            "response": f"Posted comment thread {data.get('id')}. Next: call list_comments(parent_id='{(data.get('snippet') or {}).get('topLevelComment', {}).get('id')}') to see replies, or insert_comment to reply to it.",
            "thread": _comment_thread_summary(data),
        }

    @tool(
        description="List the replies to a top-level comment.",
        params={
            "parent_id": "The ID of the top-level comment whose replies to list (from list_comment_threads). Required.",
            "max_results": "Maximum number of results to return (1-100). Default 20.",
            "page_token": "Pagination token from a previous call's next_page_token.",
        },
    )
    async def list_comments(self, parent_id: str, max_results: Optional[int] = None, page_token: Optional[str] = None) -> Dict:
        if err := _missing_params_error(parent_id=parent_id):
            return err
        params: Dict[str, Any] = {"part": "snippet", "parentId": parent_id, "maxResults": int(max_results) if max_results else 20}
        if page_token:
            params["pageToken"] = page_token
        data = await self._youtube_request("GET", "/comments", params=params)
        if _is_error(data):
            return _error_response(data, "Could not list replies.", "Check that parent_id is the ID of a top-level comment (from list_comment_threads).")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} reply(ies).",
            "comments": [_comment_summary(c) for c in items],
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Post a reply to an existing top-level comment.",
        params={
            "parent_id": "The ID of the top-level comment to reply to (from list_comment_threads). Required.",
            "text": "The reply text. Required.",
        },
    )
    async def insert_comment(self, parent_id: str, text: str) -> Dict:
        if err := _missing_params_error(parent_id=parent_id, text=text):
            return err
        body = {"snippet": {"parentId": parent_id, "textOriginal": text}}
        data = await self._youtube_request("POST", "/comments", params={"part": "snippet"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not post the reply.", "Check that parent_id is the ID of a top-level comment (from list_comment_threads).")
        return {"success": True, "response": f"Posted reply {data.get('id')}.", "comment": _comment_summary(data)}

    @tool(
        description="Edit the text of a comment or reply that you posted.",
        params={
            "comment_id": "The ID of the comment to edit. Required.",
            "text": "New text for the comment. Required.",
        },
    )
    async def update_comment(self, comment_id: str, text: str) -> Dict:
        if err := _missing_params_error(comment_id=comment_id, text=text):
            return err
        body = {"id": comment_id, "snippet": {"textOriginal": text}}
        data = await self._youtube_request("PUT", "/comments", params={"part": "snippet"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not update the comment.", "Check that comment_id is correct and that you own this comment.")
        return {"success": True, "response": f"Comment {comment_id} updated.", "comment": _comment_summary(data)}

    @tool(
        description="Permanently delete a comment or reply. This action cannot be undone.",
        params={"comment_id": "The ID of the comment to delete. Required."},
    )
    async def delete_comment(self, comment_id: str) -> Dict:
        if err := _missing_params_error(comment_id=comment_id):
            return err
        data = await self._youtube_request("DELETE", "/comments", params={"id": comment_id})
        if _is_error(data):
            return _error_response(data, "Could not delete the comment.", "Check that comment_id is correct and that you own this comment or the video it's on.")
        return {"success": True, "response": f"Comment {comment_id} deleted.", "comment_id": comment_id}

    @tool(
        description=(
            "Set the moderation status of one or more comments on videos you own — approve, "
            "hold for review, or reject (mark as spam/abusive)."
        ),
        params={
            "comment_ids": "List of comment IDs to update. Required.",
            "moderation_status": "New status: 'published', 'heldForReview', or 'rejected'. Required.",
            "ban_author": "If true (and moderation_status is 'rejected'), also bans the author from commenting on your channel's videos. Default false.",
        },
    )
    async def set_comment_moderation_status(self, comment_ids: List[str], moderation_status: str, ban_author: Optional[bool] = None) -> Dict:
        if err := _missing_params_error(comment_ids=comment_ids, moderation_status=moderation_status):
            return err
        params: Dict[str, Any] = {"id": ",".join(comment_ids), "moderationStatus": moderation_status}
        if ban_author is not None:
            params["banAuthor"] = "true" if ban_author else "false"
        data = await self._youtube_request("POST", "/comments/setModerationStatus", params=params)
        if _is_error(data):
            return _error_response(data, "Could not set moderation status.", "moderation_status must be one of 'published', 'heldForReview', or 'rejected', and you must own the related video(s).")
        return {"success": True, "response": f"Set moderation status '{moderation_status}' on {len(comment_ids)} comment(s).", "comment_ids": comment_ids}

    # ------------------------------------------------------------------
    # @tool public methods — Subscriptions
    # ------------------------------------------------------------------

    @tool(
        description="List the authenticated user's subscriptions, or check if they subscribe to a specific channel.",
        params={
            "mine": "If true, list the authenticated user's own subscriptions. Default true if for_channel_id is not given.",
            "for_channel_id": "Only return the authenticated user's subscription to this channel, if any.",
            "max_results": "Maximum number of results to return (1-50). Default 25.",
            "page_token": "Pagination token from a previous call's next_page_token.",
        },
    )
    async def list_subscriptions(
        self,
        mine: Optional[bool] = None,
        for_channel_id: Optional[str] = None,
        max_results: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"part": "snippet,contentDetails", "maxResults": int(max_results) if max_results else 25}
        if for_channel_id:
            params["forChannelId"] = for_channel_id
            params["mine"] = "true"
        elif mine is False:
            return {"success": False, "response": "Provide for_channel_id, or leave mine unset/true to list your own subscriptions."}
        else:
            params["mine"] = "true"
        if page_token:
            params["pageToken"] = page_token
        data = await self._youtube_request("GET", "/subscriptions", params=params)
        if _is_error(data):
            return _error_response(data, "Could not list subscriptions.")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} subscription(s).",
            "subscriptions": [_subscription_summary(s) for s in items],
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description="Subscribe the authenticated user to a channel.",
        params={"channel_id": "The ID of the channel to subscribe to. Required."},
    )
    async def subscribe_to_channel(self, channel_id: str) -> Dict:
        if err := _missing_params_error(channel_id=channel_id):
            return err
        body = {"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel_id}}}
        data = await self._youtube_request("POST", "/subscriptions", params={"part": "snippet"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not subscribe to the channel.", "Check that channel_id is correct.")
        return {"success": True, "response": f"Subscribed to channel {channel_id} (subscription id={data.get('id')}).", "subscription": _subscription_summary(data)}

    @tool(
        description="Unsubscribe (delete a subscription) by its subscription ID.",
        params={"subscription_id": "The ID of the subscription to remove (from list_subscriptions). Required."},
    )
    async def delete_subscription(self, subscription_id: str) -> Dict:
        if err := _missing_params_error(subscription_id=subscription_id):
            return err
        data = await self._youtube_request("DELETE", "/subscriptions", params={"id": subscription_id})
        if _is_error(data):
            return _error_response(data, "Could not remove the subscription.", "Check that subscription_id is correct — call list_subscriptions to confirm.")
        return {"success": True, "response": f"Subscription {subscription_id} removed.", "subscription_id": subscription_id}

    # ------------------------------------------------------------------
    # @tool public methods — Captions
    # ------------------------------------------------------------------

    @tool(
        description="List the caption tracks available for a video, with their language, name, and draft status.",
        params={"video_id": "The ID of the video. Required."},
    )
    async def list_captions(self, video_id: str) -> Dict:
        if err := _missing_params_error(video_id=video_id):
            return err
        data = await self._youtube_request("GET", "/captions", params={"part": "snippet", "videoId": video_id})
        if _is_error(data):
            return _error_response(data, "Could not list captions.", "Check that video_id is correct and that you own this video (caption metadata requires ownership).")
        items = data.get("items") or []
        return {
            "success": True,
            "response": f"Found {len(items)} caption track(s).",
            "captions": [_caption_summary(c) for c in items],
        }

    @tool(
        description=(
            "Update a caption track's name, language, and/or draft status. Only the fields you "
            "provide are changed. (Replacing the caption file's text content is not supported "
            "by this tool.)"
        ),
        params={
            "caption_id": "The ID of the caption track to update (from list_captions). Required.",
            "name": "New display name for the caption track, e.g. 'English (auto-generated)'.",
            "language": "New BCP-47 language code for the track, e.g. 'en'.",
            "is_draft": "If true, marks the caption track as a draft (not visible to viewers); if false, publishes it.",
        },
    )
    async def update_caption(self, caption_id: str, name: Optional[str] = None, language: Optional[str] = None, is_draft: Optional[bool] = None) -> Dict:
        if err := _missing_params_error(caption_id=caption_id):
            return err
        if all(v is None for v in (name, language, is_draft)):
            return {"success": False, "response": "Provide at least one field to update (name, language, is_draft)."}
        current = await self._youtube_request("GET", "/captions", params={"part": "snippet", "id": caption_id})
        if _is_error(current) or not (current.get("items") or []):
            return _error_response(current, "Could not load the current caption track to update.", "Check that caption_id is correct and that you own the video — call list_captions to confirm.")
        snippet = current["items"][0].get("snippet") or {}
        if name is not None:
            snippet["name"] = name
        if language is not None:
            snippet["language"] = language
        if is_draft is not None:
            snippet["isDraft"] = bool(is_draft)
        body = {"id": caption_id, "snippet": snippet}
        data = await self._youtube_request("PUT", "/captions", params={"part": "snippet"}, json_body=body)
        if _is_error(data):
            return _error_response(data, "Could not update the caption track.", "Check that caption_id is correct and that you own the video.")
        return {"success": True, "response": f"Caption track {caption_id} updated.", "caption": _caption_summary(data)}

    @tool(
        description="Permanently delete a caption track. This action cannot be undone.",
        params={"caption_id": "The ID of the caption track to delete (from list_captions). Required."},
    )
    async def delete_caption(self, caption_id: str) -> Dict:
        if err := _missing_params_error(caption_id=caption_id):
            return err
        data = await self._youtube_request("DELETE", "/captions", params={"id": caption_id})
        if _is_error(data):
            return _error_response(data, "Could not delete the caption track.", "Check that caption_id is correct and that you own the video.")
        return {"success": True, "response": f"Caption track {caption_id} deleted.", "caption_id": caption_id}


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|/(?:embed|shorts|v)/|youtu\.be/)([A-Za-z0-9_-]{11})")
_PLAYLIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_CHANNEL_ID_RE = re.compile(r"/channel/(UC[A-Za-z0-9_-]+)")
_HANDLE_RE = re.compile(r"youtube\.com/(@[A-Za-z0-9_.-]+)")


def _extract_video_id(url: str) -> Optional[str]:
    """Extract an 11-character YouTube video ID from a URL, or pass through a bare ID."""
    url = url.strip()
    match = _VIDEO_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def _extract_playlist_id(url: str) -> Optional[str]:
    """Extract a playlist ID from a YouTube URL's `list` query parameter, or pass through a bare ID."""
    url = url.strip()
    match = _PLAYLIST_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"(PL|UU|FL|LL|RD)[A-Za-z0-9_-]{10,}", url):
        return url
    return None


def _extract_channel_id(url: str) -> Optional[str]:
    """Extract a channel ID (starting with 'UC') from a /channel/ URL, or pass through a bare ID."""
    url = url.strip()
    match = _CHANNEL_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", url):
        return url
    return None


def _extract_handle(url: str) -> Optional[str]:
    """Extract an @handle from a youtube.com/@handle URL."""
    url = url.strip()
    match = _HANDLE_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"@[A-Za-z0-9_.-]+", url):
        return url
    return None


def _search_result_summary(item: Dict) -> Dict:
    item_id = item.get("id") or {}
    snippet = item.get("snippet") or {}
    video_id = item_id.get("videoId")
    return {
        "kind": item_id.get("kind"),
        "video_id": video_id,
        "channel_id": item_id.get("channelId") if item_id.get("kind") == "youtube#channel" else snippet.get("channelId"),
        "playlist_id": item_id.get("playlistId"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "channelTitle": snippet.get("channelTitle"),
        "publishedAt": snippet.get("publishedAt"),
        "Embeded_Video": _video_placeholder(video_id, snippet.get("title"), snippet.get("channelTitle")),
    }


def _video_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    statistics = item.get("statistics") or {}
    status = item.get("status") or {}
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "channelId": snippet.get("channelId"),
        "channelTitle": snippet.get("channelTitle"),
        "publishedAt": snippet.get("publishedAt"),
        "tags": snippet.get("tags"),
        "categoryId": snippet.get("categoryId"),
        "duration": content_details.get("duration"),
        "viewCount": statistics.get("viewCount"),
        "likeCount": statistics.get("likeCount"),
        "commentCount": statistics.get("commentCount"),
        "privacyStatus": status.get("privacyStatus"),
        "Embeded_Video": _video_placeholder(item.get("id"), snippet.get("title"), snippet.get("channelTitle")),
    }


def _channel_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    content_details = item.get("contentDetails") or {}
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "customUrl": snippet.get("customUrl"),
        "publishedAt": snippet.get("publishedAt"),
        "subscriberCount": statistics.get("subscriberCount"),
        "videoCount": statistics.get("videoCount"),
        "viewCount": statistics.get("viewCount"),
        "uploadsPlaylistId": ((content_details.get("relatedPlaylists") or {}).get("uploads")),
    }


def _playlist_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    status = item.get("status") or {}
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "channelId": snippet.get("channelId"),
        "itemCount": content_details.get("itemCount"),
        "privacyStatus": status.get("privacyStatus"),
    }


def _playlist_item_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    resource_id = snippet.get("resourceId") or {}
    video_id = resource_id.get("videoId") or content_details.get("videoId")
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "videoId": video_id,
        "position": snippet.get("position"),
        "note": snippet.get("note"),
        "playlistId": snippet.get("playlistId"),
        "Embeded_Video": _video_placeholder(video_id, snippet.get("title"), snippet.get("channelTitle")),
    }


def _comment_thread_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    top_level = (snippet.get("topLevelComment") or {})
    top_snippet = top_level.get("snippet") or {}
    return {
        "id": item.get("id"),
        "topLevelCommentId": top_level.get("id"),
        "videoId": snippet.get("videoId"),
        "channelId": snippet.get("channelId"),
        "author": top_snippet.get("authorDisplayName"),
        "text": top_snippet.get("textDisplay"),
        "likeCount": top_snippet.get("likeCount"),
        "publishedAt": top_snippet.get("publishedAt"),
        "totalReplyCount": snippet.get("totalReplyCount"),
    }


def _comment_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    return {
        "id": item.get("id"),
        "parentId": snippet.get("parentId"),
        "author": snippet.get("authorDisplayName"),
        "text": snippet.get("textDisplay"),
        "likeCount": snippet.get("likeCount"),
        "publishedAt": snippet.get("publishedAt"),
    }


def _subscription_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    resource_id = snippet.get("resourceId") or {}
    content_details = item.get("contentDetails") or {}
    return {
        "id": item.get("id"),
        "channelId": resource_id.get("channelId"),
        "title": snippet.get("title"),
        "publishedAt": snippet.get("publishedAt"),
        "newItemCount": content_details.get("newItemCount"),
    }


def _caption_summary(item: Dict) -> Dict:
    snippet = item.get("snippet") or {}
    return {
        "id": item.get("id"),
        "videoId": snippet.get("videoId"),
        "language": snippet.get("language"),
        "name": snippet.get("name"),
        "trackKind": snippet.get("trackKind"),
        "isDraft": snippet.get("isDraft"),
    }
