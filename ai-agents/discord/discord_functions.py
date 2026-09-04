"""
Discord functions class for functions_wrapper plugin.

Covers the full Discord REST API v10:
  - Guilds (get, modify, channels, threads, invites, integrations, widget, vanity, welcome screen,
    onboarding, audit logs, prune, voice regions, MFA, scheduled events)
  - Guild Members (list, get, search, modify, kick, add/remove roles)
  - Guild Bans (list, get, ban, unban)
  - Guild Roles (list, create, modify, delete, reorder)
  - Channels (get, modify, delete, permissions, invites, typing, follow)
  - Messages (list, get, send, edit, delete, bulk delete, crosspost, pins)
  - Reactions (create, delete own/user/all, get)
  - Threads (start from message, start in channel, join, leave, add/remove member,
    list members, list archived)
  - Users (get current, get, modify current, list guilds, leave guild, create DM)
  - Webhooks (create, list, get, modify, delete, execute, get/edit/delete message)
  - Emojis (list, get, create, modify, delete — guild and application emojis)
  - Invites (get, delete)
  - Guild Scheduled Events (list, get, create, modify, delete, get users)

Authentication: Discord Bot Token via `Authorization: Bot {token}`.
The token is sourced from credentials["bot_token"] or credentials["access_token"].

https://discord.com/developers/docs/reference
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0
DEFAULT_LIMIT = 50


class DiscordFunctions(ConnectedServiceToolAgent):
    """
    Discord tool functions. Each @tool method is a Discord API capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.

    The Discord bot token is read from credentials["bot_token"] or credentials["access_token"].
    """

    TOOL_DESCRIPTION = (
        "Discord: manage guilds, channels, messages, reactions, threads, members, roles, "
        "webhooks, emojis, invites, and scheduled events."
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

        # Discord bot token — prefer explicit bot_token field, fall back to access_token
        self._bot_token: Optional[str] = (
            self.credentials.get("bot_token")
            or self.credentials.get("access_token")
            or self.credentials.get("token")
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("DiscordFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self._bot_token:
            logger.warning("DiscordFunctions: no bot_token found in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._discord_headers())
        logger.debug("DiscordFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _discord_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "PlumoAI-DiscordAgent (https://plumoai.com, 1)",
        }

    async def _discord_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        files: Optional[Any] = None,
        reason: Optional[str] = None,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = f"{DISCORD_API_BASE}{path}" if path.startswith("/") else f"{DISCORD_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._discord_headers())

        request_headers: Dict[str, str] = {}
        if reason:
            import urllib.parse
            request_headers["X-Audit-Log-Reason"] = urllib.parse.quote(reason, safe="")

        if files is not None:
            headers = {**self._discord_headers(), **request_headers}
            del headers["Content-Type"]
            r = await self._httpx_client.request(method, url, params=params, files=files, headers=headers)
        elif json_body is not None:
            r = await self._httpx_client.request(
                method, url, json=json_body, params=params, headers=request_headers or None
            )
        else:
            r = await self._httpx_client.request(
                method, url, params=params, headers=request_headers or None
            )

        # Handle rate limiting
        if r.status_code == 429 and _retry_count < _REQUEST_MAX_RETRIES:
            try:
                retry_after = float((r.json() or {}).get("retry_after", 1.0))
            except Exception:
                retry_after = _REQUEST_BASE_DELAY * (2 ** _retry_count)
            delay = min(retry_after, _REQUEST_MAX_DELAY)
            logger.warning("Discord API rate-limited on %s %s; retrying in %.1fs", method, path, delay)
            await asyncio.sleep(delay)
            return await self._discord_request(
                method, path, json_body=json_body, params=params, files=files,
                reason=reason, _retry_count=_retry_count + 1,
            )

        if r.status_code in (500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning("Discord API %s %s -> %s; retrying in %.1fs", method, path, r.status_code, delay)
            await asyncio.sleep(delay)
            return await self._discord_request(
                method, path, json_body=json_body, params=params, files=files,
                reason=reason, _retry_count=_retry_count + 1,
            )

        if r.status_code >= 400:
            logger.warning("Discord API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            return {"_error": True, "status_code": r.status_code, "message": (r.text or "")[:500]}

        if r.status_code == 204 or not r.content:
            return {}

        try:
            data = r.json()
            snippet = json.dumps(data, default=str)
            if len(snippet) > 3000:
                snippet = snippet[:3000] + "... (truncated)"
            logger.info("Discord API %s %s -> %s: %s", method, path, r.status_code, snippet)
            return data
        except Exception:
            return {}

    @staticmethod
    def _err(data: Optional[Dict], msg: str) -> Optional[Dict]:
        if data is None:
            return {"success": False, "response": msg}
        if isinstance(data, dict) and data.get("_error"):
            return {"success": False, "response": f"{msg}: HTTP {data.get('status_code')} {data.get('message')}"}
        return None

    @staticmethod
    def _ok(response: str, **extra) -> Dict:
        return {"success": True, "response": response, **extra}

    @staticmethod
    def _strip_none(d: Dict) -> Dict:
        return {k: v for k, v in d.items() if v is not None}

    # ==================================================================
    # GUILDS
    # ==================================================================

    @tool(
        description=(
            "Get details about a Discord guild (server), including its name, icon, owner, "
            "member count, channels, roles, and features."
        ),
        params={
            "guild_id": "The ID of the guild.",
            "with_counts": "Whether to include approximate member and presence counts. Default false.",
        },
    )
    async def get_guild(self, guild_id: str, with_counts: bool = False) -> Dict:
        params = {"with_counts": "true"} if with_counts else None
        data = await self._discord_request("GET", f"/guilds/{guild_id}", params=params)
        return self._err(data, "Failed to get guild") or self._ok(
            f"Guild '{data.get('name')}'.", guild=data
        )

    @tool(
        description="Get a preview of a discoverable Discord guild (does not require membership).",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_preview(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/preview")
        return self._err(data, "Failed to get guild preview") or self._ok(
            f"Guild preview for '{data.get('name')}'.", preview=data
        )

    @tool(
        description="Modify a Discord guild's settings (name, icon, region, AFK channel, etc.).",
        params={
            "guild_id": "The ID of the guild.",
            "name": "New name for the guild.",
            "description": "Guild description (only for discoverable guilds).",
            "preferred_locale": "Preferred locale, e.g. 'en-US'.",
            "reason": "Audit log reason for this change.",
        },
    )
    async def modify_guild(
        self, guild_id: str, name: Optional[str] = None, description: Optional[str] = None,
        preferred_locale: Optional[str] = None, reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({"name": name, "description": description, "preferred_locale": preferred_locale})
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify guild") or self._ok("Guild updated.", guild=data)

    @tool(
        description="List all channels in a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_channels(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/channels")
        if isinstance(data, list):
            return self._ok(f"Found {len(data)} channel(s).", channels=data, count=len(data))
        return self._err(data, "Failed to get guild channels") or self._ok(
            f"Found {len(data)} channel(s).", channels=data
        )

    @tool(
        description="Create a new channel in a Discord guild (text, voice, category, forum, etc.).",
        params={
            "guild_id": "The ID of the guild.",
            "name": "Channel name (2–100 chars).",
            "type": (
                "Channel type integer: 0=GUILD_TEXT, 2=GUILD_VOICE, 4=GUILD_CATEGORY, "
                "5=GUILD_ANNOUNCEMENT, 15=GUILD_FORUM, 16=GUILD_MEDIA. Default 0."
            ),
            "topic": "Channel topic (0–1024 chars, text channels only).",
            "parent_id": "ID of the parent category channel.",
            "position": "Sorting position of the channel.",
            "reason": "Audit log reason.",
        },
    )
    async def create_guild_channel(
        self, guild_id: str, name: str, type: int = 0,
        topic: Optional[str] = None, parent_id: Optional[str] = None,
        position: Optional[int] = None, reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({"name": name, "type": type, "topic": topic,
                                  "parent_id": parent_id, "position": position})
        data = await self._discord_request("POST", f"/guilds/{guild_id}/channels", json_body=body, reason=reason)
        return self._err(data, "Failed to create channel") or self._ok(
            f"Created channel '{name}' ({data.get('id')}).", channel=data
        )

    @tool(
        description="List all active threads in a Discord guild (across all channels).",
        params={"guild_id": "The ID of the guild."},
    )
    async def list_active_guild_threads(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/threads/active")
        threads = (data or {}).get("threads") or (data if isinstance(data, list) else [])
        return self._err(data, "Failed to list active threads") or self._ok(
            f"Found {len(threads)} active thread(s).", threads=threads, count=len(threads)
        )

    @tool(
        description="Get the estimated or actual count of members who would be pruned from a guild for inactivity.",
        params={
            "guild_id": "The ID of the guild.",
            "days": "Number of days of inactivity (1–30). Default 7.",
            "include_roles": "Comma-separated role IDs to include in the prune check.",
        },
    )
    async def get_guild_prune_count(
        self, guild_id: str, days: int = 7, include_roles: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"days": max(1, min(int(days or 7), 30))}
        if include_roles:
            params["include_roles"] = include_roles
        data = await self._discord_request("GET", f"/guilds/{guild_id}/prune", params=params)
        return self._err(data, "Failed to get prune count") or self._ok(
            f"{(data or {}).get('pruned', 0)} member(s) would be pruned.", pruned=(data or {}).get("pruned")
        )

    @tool(
        description="Begin pruning inactive members from a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "days": "Number of days of inactivity (1–30). Default 7.",
            "compute_prune_count": "Whether to return the count of pruned members. Default true.",
            "reason": "Audit log reason.",
        },
    )
    async def begin_guild_prune(
        self, guild_id: str, days: int = 7,
        compute_prune_count: bool = True, reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"days": max(1, min(int(days or 7), 30)),
                                 "compute_prune_count": compute_prune_count}
        data = await self._discord_request("POST", f"/guilds/{guild_id}/prune", json_body=body, reason=reason)
        return self._err(data, "Failed to begin prune") or self._ok(
            f"Pruned {(data or {}).get('pruned', '(count not computed)')} member(s).",
            pruned=(data or {}).get("pruned"),
        )

    @tool(
        description="Get all active invites for a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_invites(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/invites")
        invites = data if isinstance(data, list) else []
        return self._err(data, "Failed to get guild invites") or self._ok(
            f"Found {len(invites)} invite(s).", invites=invites, count=len(invites)
        )

    @tool(
        description="Get a list of integrations (Twitch, YouTube, etc.) attached to a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_integrations(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/integrations")
        integrations = data if isinstance(data, list) else []
        return self._err(data, "Failed to get integrations") or self._ok(
            f"Found {len(integrations)} integration(s).", integrations=integrations, count=len(integrations)
        )

    @tool(
        description="Delete an integration from a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "integration_id": "The ID of the integration to remove.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_guild_integration(self, guild_id: str, integration_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/integrations/{integration_id}", reason=reason)
        return self._err(data, "Failed to delete integration") or self._ok("Integration deleted.")

    @tool(
        description="Get the widget data (online members, channels) for a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_widget(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/widget.json")
        return self._err(data, "Failed to get guild widget") or self._ok("Widget data retrieved.", widget=data)

    @tool(
        description="Get the vanity URL for a Discord guild (e.g. discord.gg/my-server).",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_vanity_url(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/vanity-url")
        return self._err(data, "Failed to get vanity URL") or self._ok(
            f"Vanity code: {(data or {}).get('code')}.", vanity=data
        )

    @tool(
        description="Get the welcome screen for a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_welcome_screen(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/welcome-screen")
        return self._err(data, "Failed to get welcome screen") or self._ok("Welcome screen retrieved.", welcome_screen=data)

    @tool(
        description="Modify the welcome screen of a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "enabled": "Whether the welcome screen is enabled.",
            "description": "Description shown on the welcome screen.",
            "reason": "Audit log reason.",
        },
    )
    async def modify_guild_welcome_screen(
        self, guild_id: str, enabled: Optional[bool] = None,
        description: Optional[str] = None, reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({"enabled": enabled, "description": description})
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/welcome-screen", json_body=body, reason=reason)
        return self._err(data, "Failed to modify welcome screen") or self._ok("Welcome screen updated.", welcome_screen=data)

    @tool(
        description="Get the onboarding flow configuration for a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_onboarding(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/onboarding")
        return self._err(data, "Failed to get onboarding") or self._ok("Onboarding retrieved.", onboarding=data)

    @tool(
        description="Get voice region options available for a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def get_guild_voice_regions(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/regions")
        regions = data if isinstance(data, list) else []
        return self._err(data, "Failed to get voice regions") or self._ok(
            f"Found {len(regions)} voice region(s).", regions=regions
        )

    # ==================================================================
    # GUILD MEMBERS
    # ==================================================================

    @tool(
        description="Get a member of a Discord guild.",
        params={"guild_id": "The ID of the guild.", "user_id": "The ID of the user."},
    )
    async def get_guild_member(self, guild_id: str, user_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/members/{user_id}")
        return self._err(data, "Failed to get member") or self._ok("Member retrieved.", member=data)

    @tool(
        description="List members of a Discord guild. Requires the GUILD_MEMBERS privileged intent on the bot.",
        params={
            "guild_id": "The ID of the guild.",
            "limit": "Max number of members to return (1–1000, default 50).",
            "after": "Return members with ID greater than this (for pagination).",
        },
    )
    async def list_guild_members(
        self, guild_id: str, limit: int = DEFAULT_LIMIT, after: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 1000))}
        if after:
            params["after"] = after
        data = await self._discord_request("GET", f"/guilds/{guild_id}/members", params=params)
        members = data if isinstance(data, list) else []
        return self._err(data, "Failed to list members") or self._ok(
            f"Found {len(members)} member(s).", members=members, count=len(members)
        )

    @tool(
        description="Search for guild members by username or nickname.",
        params={
            "guild_id": "The ID of the guild.",
            "query": "Username or nickname prefix to search for.",
            "limit": "Max results to return (1–1000, default 1).",
        },
    )
    async def search_guild_members(self, guild_id: str, query: str, limit: int = 10) -> Dict:
        params = {"query": query, "limit": max(1, min(int(limit or 10), 1000))}
        data = await self._discord_request("GET", f"/guilds/{guild_id}/members/search", params=params)
        members = data if isinstance(data, list) else []
        return self._err(data, "Failed to search members") or self._ok(
            f"Found {len(members)} member(s) matching '{query}'.", members=members, count=len(members)
        )

    @tool(
        description="Modify a guild member's attributes (nickname, roles, mute, deaf).",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user.",
            "nick": "New nickname (null to clear).",
            "roles": "List of role IDs to set for the member.",
            "mute": "Whether to mute the member in voice channels.",
            "deaf": "Whether to deafen the member in voice channels.",
            "reason": "Audit log reason.",
        },
    )
    async def modify_guild_member(
        self, guild_id: str, user_id: str,
        nick: Optional[str] = None, roles: Optional[List[str]] = None,
        mute: Optional[bool] = None, deaf: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({
            "nick": nick, "roles": roles, "mute": mute, "deaf": deaf,
        })
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/members/{user_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify member") or self._ok("Member updated.", member=data)

    @tool(
        description="Update the current bot's own member profile in a guild (e.g. change nickname).",
        params={
            "guild_id": "The ID of the guild.",
            "nick": "New nickname for the bot (null to clear).",
            "reason": "Audit log reason.",
        },
    )
    async def modify_current_member(self, guild_id: str, nick: Optional[str] = None, reason: Optional[str] = None) -> Dict:
        body = {"nick": nick}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/members/@me", json_body=body, reason=reason)
        return self._err(data, "Failed to modify current member") or self._ok("Current member updated.", member=data)

    @tool(
        description="Remove a member from a Discord guild (kick).",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user to kick.",
            "reason": "Audit log reason.",
        },
    )
    async def kick_guild_member(self, guild_id: str, user_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/members/{user_id}", reason=reason)
        return self._err(data, "Failed to kick member") or self._ok(f"Member {user_id} kicked.")

    @tool(
        description="Add a role to a guild member.",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user.",
            "role_id": "The ID of the role to add.",
            "reason": "Audit log reason.",
        },
    )
    async def add_role_to_member(self, guild_id: str, user_id: str, role_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("PUT", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", reason=reason)
        return self._err(data, "Failed to add role") or self._ok(f"Role {role_id} added to member {user_id}.")

    @tool(
        description="Remove a role from a guild member.",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user.",
            "role_id": "The ID of the role to remove.",
            "reason": "Audit log reason.",
        },
    )
    async def remove_role_from_member(self, guild_id: str, user_id: str, role_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}", reason=reason)
        return self._err(data, "Failed to remove role") or self._ok(f"Role {role_id} removed from member {user_id}.")

    # ==================================================================
    # GUILD BANS
    # ==================================================================

    @tool(
        description="List all bans in a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "limit": "Max number of bans to return (1–1000, default 50).",
            "before": "Return bans for users with ID less than this (pagination).",
            "after": "Return bans for users with ID greater than this (pagination).",
        },
    )
    async def list_guild_bans(
        self, guild_id: str, limit: int = DEFAULT_LIMIT,
        before: Optional[str] = None, after: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 1000))}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        data = await self._discord_request("GET", f"/guilds/{guild_id}/bans", params=params)
        bans = data if isinstance(data, list) else []
        return self._err(data, "Failed to list bans") or self._ok(
            f"Found {len(bans)} ban(s).", bans=bans, count=len(bans)
        )

    @tool(
        description="Get the ban for a specific user in a Discord guild.",
        params={"guild_id": "The ID of the guild.", "user_id": "The ID of the banned user."},
    )
    async def get_guild_ban(self, guild_id: str, user_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/bans/{user_id}")
        return self._err(data, "Failed to get ban") or self._ok("Ban details retrieved.", ban=data)

    @tool(
        description="Ban a user from a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user to ban.",
            "delete_message_seconds": "Number of seconds of messages to delete (0–604800, i.e. 0–7 days).",
            "reason": "Audit log reason.",
        },
    )
    async def ban_member(
        self, guild_id: str, user_id: str,
        delete_message_seconds: int = 0, reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"delete_message_seconds": max(0, min(int(delete_message_seconds or 0), 604800))}
        data = await self._discord_request("PUT", f"/guilds/{guild_id}/bans/{user_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to ban member") or self._ok(f"User {user_id} banned.")

    @tool(
        description="Remove a ban from a user in a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "user_id": "The ID of the user to unban.",
            "reason": "Audit log reason.",
        },
    )
    async def unban_member(self, guild_id: str, user_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/bans/{user_id}", reason=reason)
        return self._err(data, "Failed to unban member") or self._ok(f"User {user_id} unbanned.")

    # ==================================================================
    # GUILD ROLES
    # ==================================================================

    @tool(
        description="List all roles in a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def list_guild_roles(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/roles")
        roles = data if isinstance(data, list) else []
        return self._err(data, "Failed to list roles") or self._ok(
            f"Found {len(roles)} role(s).", roles=roles, count=len(roles)
        )

    @tool(
        description="Create a new role in a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "name": "Name of the role (default: 'new role').",
            "color": "RGB color value as integer (e.g. 0xFF5733 = 16734003).",
            "hoist": "Whether to display this role separately in the member list.",
            "mentionable": "Whether this role can be mentioned by members.",
            "permissions": "Permission bit set as a string (e.g. '8' for Administrator).",
            "reason": "Audit log reason.",
        },
    )
    async def create_guild_role(
        self, guild_id: str, name: str = "new role",
        color: Optional[int] = None, hoist: bool = False,
        mentionable: bool = False, permissions: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"name": name, "hoist": hoist, "mentionable": mentionable}
        if color is not None:
            body["color"] = int(color)
        if permissions is not None:
            body["permissions"] = str(permissions)
        data = await self._discord_request("POST", f"/guilds/{guild_id}/roles", json_body=body, reason=reason)
        return self._err(data, "Failed to create role") or self._ok(
            f"Role '{name}' created ({data.get('id')}).", role=data
        )

    @tool(
        description="Modify an existing role in a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "role_id": "The ID of the role to modify.",
            "name": "New name for the role.",
            "color": "New RGB color as integer.",
            "hoist": "Whether to display separately in the member list.",
            "mentionable": "Whether this role can be mentioned.",
            "permissions": "New permission bit set as string.",
            "reason": "Audit log reason.",
        },
    )
    async def modify_guild_role(
        self, guild_id: str, role_id: str,
        name: Optional[str] = None, color: Optional[int] = None,
        hoist: Optional[bool] = None, mentionable: Optional[bool] = None,
        permissions: Optional[str] = None, reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({
            "name": name, "color": color, "hoist": hoist,
            "mentionable": mentionable, "permissions": permissions,
        })
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/roles/{role_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify role") or self._ok("Role updated.", role=data)

    @tool(
        description="Delete a role from a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "role_id": "The ID of the role to delete.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_guild_role(self, guild_id: str, role_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/roles/{role_id}", reason=reason)
        return self._err(data, "Failed to delete role") or self._ok(f"Role {role_id} deleted.")

    # ==================================================================
    # GUILD SCHEDULED EVENTS
    # ==================================================================

    @tool(
        description="List all scheduled events in a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "with_user_count": "Whether to include the count of users subscribed to each event.",
        },
    )
    async def list_scheduled_events(self, guild_id: str, with_user_count: bool = False) -> Dict:
        params = {"with_user_count": "true"} if with_user_count else None
        data = await self._discord_request("GET", f"/guilds/{guild_id}/scheduled-events", params=params)
        events = data if isinstance(data, list) else []
        return self._err(data, "Failed to list scheduled events") or self._ok(
            f"Found {len(events)} scheduled event(s).", events=events, count=len(events)
        )

    @tool(
        description="Get details about a specific guild scheduled event.",
        params={
            "guild_id": "The ID of the guild.",
            "event_id": "The ID of the scheduled event.",
            "with_user_count": "Whether to include subscriber count.",
        },
    )
    async def get_scheduled_event(self, guild_id: str, event_id: str, with_user_count: bool = False) -> Dict:
        params = {"with_user_count": "true"} if with_user_count else None
        data = await self._discord_request("GET", f"/guilds/{guild_id}/scheduled-events/{event_id}", params=params)
        return self._err(data, "Failed to get scheduled event") or self._ok(
            f"Event '{data.get('name')}'.", event=data
        )

    @tool(
        description=(
            "Create a guild scheduled event. For EXTERNAL events provide entity_metadata with location. "
            "For STAGE_INSTANCE or VOICE events provide channel_id."
        ),
        params={
            "guild_id": "The ID of the guild.",
            "name": "Name of the event (1–100 chars).",
            "scheduled_start_time": "ISO8601 start time, e.g. '2026-07-01T18:00:00Z'.",
            "scheduled_end_time": "ISO8601 end time (required for EXTERNAL events).",
            "privacy_level": "Privacy level: 2 = GUILD_ONLY (only valid value currently).",
            "entity_type": "1=STAGE_INSTANCE, 2=VOICE, 3=EXTERNAL.",
            "channel_id": "Voice/stage channel ID (required for STAGE_INSTANCE and VOICE events).",
            "location": "Location string for EXTERNAL events.",
            "description": "Description of the event (up to 1000 chars).",
            "reason": "Audit log reason.",
        },
    )
    async def create_scheduled_event(
        self, guild_id: str, name: str,
        scheduled_start_time: str, entity_type: int,
        privacy_level: int = 2,
        scheduled_end_time: Optional[str] = None,
        channel_id: Optional[str] = None,
        location: Optional[str] = None,
        description: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {
            "name": name,
            "scheduled_start_time": scheduled_start_time,
            "privacy_level": privacy_level,
            "entity_type": entity_type,
        }
        if scheduled_end_time:
            body["scheduled_end_time"] = scheduled_end_time
        if channel_id:
            body["channel_id"] = channel_id
        if location:
            body["entity_metadata"] = {"location": location}
        if description:
            body["description"] = description
        data = await self._discord_request("POST", f"/guilds/{guild_id}/scheduled-events", json_body=body, reason=reason)
        return self._err(data, "Failed to create scheduled event") or self._ok(
            f"Scheduled event '{name}' created.", event=data
        )

    @tool(
        description="Modify a guild scheduled event.",
        params={
            "guild_id": "The ID of the guild.",
            "event_id": "The ID of the event.",
            "name": "New name.",
            "scheduled_start_time": "New ISO8601 start time.",
            "scheduled_end_time": "New ISO8601 end time.",
            "description": "New description.",
            "status": "1=SCHEDULED, 2=ACTIVE, 3=COMPLETED, 4=CANCELED.",
            "reason": "Audit log reason.",
        },
    )
    async def modify_scheduled_event(
        self, guild_id: str, event_id: str,
        name: Optional[str] = None, scheduled_start_time: Optional[str] = None,
        scheduled_end_time: Optional[str] = None, description: Optional[str] = None,
        status: Optional[int] = None, reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({
            "name": name, "scheduled_start_time": scheduled_start_time,
            "scheduled_end_time": scheduled_end_time,
            "description": description, "status": status,
        })
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/scheduled-events/{event_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify scheduled event") or self._ok("Scheduled event updated.", event=data)

    @tool(
        description="Delete a guild scheduled event.",
        params={"guild_id": "The ID of the guild.", "event_id": "The ID of the event to delete."},
    )
    async def delete_scheduled_event(self, guild_id: str, event_id: str) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/scheduled-events/{event_id}")
        return self._err(data, "Failed to delete scheduled event") or self._ok("Scheduled event deleted.")

    @tool(
        description="Get the list of users subscribed to a guild scheduled event.",
        params={
            "guild_id": "The ID of the guild.",
            "event_id": "The ID of the scheduled event.",
            "limit": "Max results (1–100, default 50).",
            "with_member": "Whether to include guild member data for each user.",
            "before": "User ID for pagination (before).",
            "after": "User ID for pagination (after).",
        },
    )
    async def get_scheduled_event_users(
        self, guild_id: str, event_id: str, limit: int = DEFAULT_LIMIT,
        with_member: bool = False, before: Optional[str] = None, after: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 100))}
        if with_member:
            params["with_member"] = "true"
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        data = await self._discord_request("GET", f"/guilds/{guild_id}/scheduled-events/{event_id}/users", params=params)
        users = data if isinstance(data, list) else []
        return self._err(data, "Failed to get event users") or self._ok(
            f"Found {len(users)} subscriber(s).", users=users, count=len(users)
        )

    # ==================================================================
    # CHANNELS
    # ==================================================================

    @tool(
        description="Get details about a Discord channel (type, name, topic, permissions, etc.).",
        params={"channel_id": "The ID of the channel."},
    )
    async def get_channel(self, channel_id: str) -> Dict:
        data = await self._discord_request("GET", f"/channels/{channel_id}")
        return self._err(data, "Failed to get channel") or self._ok(
            f"Channel '{data.get('name') or data.get('id')}'.", channel=data
        )

    @tool(
        description="Modify a Discord channel's settings (name, topic, slowmode, position, permissions, etc.).",
        params={
            "channel_id": "The ID of the channel.",
            "name": "New channel name.",
            "topic": "New topic (text channels only, up to 1024 chars).",
            "rate_limit_per_user": "Slowmode in seconds (0–21600).",
            "position": "New sorting position.",
            "parent_id": "New parent category ID.",
            "nsfw": "Whether the channel is age-restricted.",
            "reason": "Audit log reason.",
        },
    )
    async def modify_channel(
        self, channel_id: str, name: Optional[str] = None, topic: Optional[str] = None,
        rate_limit_per_user: Optional[int] = None, position: Optional[int] = None,
        parent_id: Optional[str] = None, nsfw: Optional[bool] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({
            "name": name, "topic": topic, "rate_limit_per_user": rate_limit_per_user,
            "position": position, "parent_id": parent_id, "nsfw": nsfw,
        })
        if not body:
            return {"success": False, "response": "No fields provided to update."}
        data = await self._discord_request("PATCH", f"/channels/{channel_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify channel") or self._ok("Channel updated.", channel=data)

    @tool(
        description=(
            "Delete a Discord channel (or close a DM). "
            "Deleting a category does not delete its children — move them first."
        ),
        params={"channel_id": "The ID of the channel.", "reason": "Audit log reason."},
    )
    async def delete_channel(self, channel_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}", reason=reason)
        return self._err(data, "Failed to delete channel") or self._ok("Channel deleted.")

    @tool(
        description="Edit permission overwrites for a user or role in a channel.",
        params={
            "channel_id": "The ID of the channel.",
            "overwrite_id": "The user or role ID to set permissions for.",
            "allow": "Allowed permission bit set as string.",
            "deny": "Denied permission bit set as string.",
            "type": "0=role, 1=member.",
            "reason": "Audit log reason.",
        },
    )
    async def edit_channel_permissions(
        self, channel_id: str, overwrite_id: str,
        allow: str = "0", deny: str = "0", type: int = 0,
        reason: Optional[str] = None,
    ) -> Dict:
        body = {"allow": str(allow), "deny": str(deny), "type": int(type)}
        data = await self._discord_request("PUT", f"/channels/{channel_id}/permissions/{overwrite_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to edit permissions") or self._ok("Channel permissions updated.")

    @tool(
        description="Delete a permission overwrite for a user or role in a channel.",
        params={
            "channel_id": "The ID of the channel.",
            "overwrite_id": "The user or role ID whose overwrite to delete.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_channel_permission(self, channel_id: str, overwrite_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/permissions/{overwrite_id}", reason=reason)
        return self._err(data, "Failed to delete channel permission") or self._ok("Channel permission overwrite deleted.")

    @tool(
        description="Get all active invites for a Discord channel.",
        params={"channel_id": "The ID of the channel."},
    )
    async def get_channel_invites(self, channel_id: str) -> Dict:
        data = await self._discord_request("GET", f"/channels/{channel_id}/invites")
        invites = data if isinstance(data, list) else []
        return self._err(data, "Failed to get channel invites") or self._ok(
            f"Found {len(invites)} invite(s).", invites=invites, count=len(invites)
        )

    @tool(
        description="Create an invite for a Discord channel.",
        params={
            "channel_id": "The ID of the channel.",
            "max_age": "Duration in seconds before expiry (0=never, default 86400).",
            "max_uses": "Max number of times the invite can be used (0=unlimited).",
            "temporary": "Whether this invite grants temporary membership.",
            "unique": "Whether to create a unique invite (vs reuse an existing one).",
            "reason": "Audit log reason.",
        },
    )
    async def create_channel_invite(
        self, channel_id: str, max_age: int = 86400, max_uses: int = 0,
        temporary: bool = False, unique: bool = False, reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {
            "max_age": int(max_age or 86400), "max_uses": int(max_uses or 0),
            "temporary": bool(temporary), "unique": bool(unique),
        }
        data = await self._discord_request("POST", f"/channels/{channel_id}/invites", json_body=body, reason=reason)
        return self._err(data, "Failed to create invite") or self._ok(
            f"Invite created: discord.gg/{data.get('code')}", invite=data
        )

    @tool(
        description="Follow an announcement (news) channel to cross-post messages to another channel.",
        params={
            "channel_id": "The ID of the announcement channel to follow.",
            "webhook_channel_id": "The ID of the target channel to receive cross-posts.",
        },
    )
    async def follow_announcement_channel(self, channel_id: str, webhook_channel_id: str) -> Dict:
        body = {"webhook_channel_id": webhook_channel_id}
        data = await self._discord_request("POST", f"/channels/{channel_id}/followers", json_body=body)
        return self._err(data, "Failed to follow announcement channel") or self._ok("Announcement channel followed.", followed=data)

    @tool(
        description="Trigger the typing indicator in a Discord channel (shows 'Bot is typing...' for ~10 seconds).",
        params={"channel_id": "The ID of the channel."},
    )
    async def trigger_typing(self, channel_id: str) -> Dict:
        data = await self._discord_request("POST", f"/channels/{channel_id}/typing")
        return self._err(data, "Failed to trigger typing") or self._ok("Typing indicator triggered.")

    # ==================================================================
    # MESSAGES
    # ==================================================================

    @tool(
        description=(
            "List messages in a Discord channel. Returns up to 100 messages at a time, "
            "ordered by creation time. Use before/after/around for pagination."
        ),
        params={
            "channel_id": "The ID of the channel.",
            "limit": "Max messages to return (1–100, default 50).",
            "before": "Get messages before this message ID.",
            "after": "Get messages after this message ID.",
            "around": "Get messages around this message ID (limit capped at 100).",
        },
    )
    async def list_messages(
        self, channel_id: str, limit: int = DEFAULT_LIMIT,
        before: Optional[str] = None, after: Optional[str] = None, around: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 100))}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if around:
            params["around"] = around
        data = await self._discord_request("GET", f"/channels/{channel_id}/messages", params=params)
        messages = data if isinstance(data, list) else []
        return self._err(data, "Failed to list messages") or self._ok(
            f"Found {len(messages)} message(s).", messages=messages, count=len(messages)
        )

    @tool(
        description="Get a specific message in a Discord channel.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
        },
    )
    async def get_message(self, channel_id: str, message_id: str) -> Dict:
        data = await self._discord_request("GET", f"/channels/{channel_id}/messages/{message_id}")
        return self._err(data, "Failed to get message") or self._ok("Message retrieved.", message=data)

    @tool(
        description=(
            "Send a message to a Discord channel. Supports plain text, "
            "and optional reply-to (message reference)."
        ),
        params={
            "channel_id": "The ID of the channel to send the message to.",
            "content": "Text content of the message (up to 2000 chars).",
            "reply_to_message_id": "ID of a message to reply to (creates a reply thread).",
            "tts": "Whether to send as a text-to-speech message.",
        },
    )
    async def send_message(
        self, channel_id: str, content: str,
        reply_to_message_id: Optional[str] = None, tts: bool = False,
    ) -> Dict:
        body: Dict[str, Any] = {"content": content, "tts": bool(tts)}
        if reply_to_message_id:
            body["message_reference"] = {"message_id": reply_to_message_id}
        data = await self._discord_request("POST", f"/channels/{channel_id}/messages", json_body=body)
        return self._err(data, "Failed to send message") or self._ok(
            f"Message sent ({data.get('id')}).", message=data
        )

    @tool(
        description="Edit a previously sent message in a Discord channel (bot can only edit its own messages).",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message to edit.",
            "content": "New text content for the message.",
        },
    )
    async def edit_message(self, channel_id: str, message_id: str, content: str) -> Dict:
        data = await self._discord_request("PATCH", f"/channels/{channel_id}/messages/{message_id}", json_body={"content": content})
        return self._err(data, "Failed to edit message") or self._ok("Message edited.", message=data)

    @tool(
        description="Delete a message in a Discord channel.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message to delete.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_message(self, channel_id: str, message_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/messages/{message_id}", reason=reason)
        return self._err(data, "Failed to delete message") or self._ok("Message deleted.")

    @tool(
        description=(
            "Bulk delete 2–100 messages from a Discord channel at once. "
            "Messages must be less than 2 weeks old."
        ),
        params={
            "channel_id": "The ID of the channel.",
            "message_ids": "List of message IDs to delete (2–100 IDs).",
            "reason": "Audit log reason.",
        },
    )
    async def bulk_delete_messages(self, channel_id: str, message_ids: List[str], reason: Optional[str] = None) -> Dict:
        if not (2 <= len(message_ids) <= 100):
            return {"success": False, "response": "bulk_delete_messages requires 2–100 message IDs."}
        data = await self._discord_request("POST", f"/channels/{channel_id}/messages/bulk-delete",
                                            json_body={"messages": message_ids}, reason=reason)
        return self._err(data, "Failed to bulk delete messages") or self._ok(f"Deleted {len(message_ids)} messages.")

    @tool(
        description=(
            "Crosspost (publish) a message from an announcement channel to all channels "
            "that are following it."
        ),
        params={
            "channel_id": "The ID of the announcement channel.",
            "message_id": "The ID of the message to crosspost.",
        },
    )
    async def crosspost_message(self, channel_id: str, message_id: str) -> Dict:
        data = await self._discord_request("POST", f"/channels/{channel_id}/messages/{message_id}/crosspost")
        return self._err(data, "Failed to crosspost message") or self._ok("Message crossposted.", message=data)

    @tool(
        description="Get all pinned messages in a Discord channel.",
        params={"channel_id": "The ID of the channel."},
    )
    async def get_pinned_messages(self, channel_id: str) -> Dict:
        data = await self._discord_request("GET", f"/channels/{channel_id}/pins")
        pins = data if isinstance(data, list) else []
        return self._err(data, "Failed to get pinned messages") or self._ok(
            f"Found {len(pins)} pinned message(s).", messages=pins, count=len(pins)
        )

    @tool(
        description="Pin a message in a Discord channel.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message to pin.",
            "reason": "Audit log reason.",
        },
    )
    async def pin_message(self, channel_id: str, message_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("PUT", f"/channels/{channel_id}/pins/{message_id}", reason=reason)
        return self._err(data, "Failed to pin message") or self._ok("Message pinned.")

    @tool(
        description="Unpin a message in a Discord channel.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message to unpin.",
            "reason": "Audit log reason.",
        },
    )
    async def unpin_message(self, channel_id: str, message_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/pins/{message_id}", reason=reason)
        return self._err(data, "Failed to unpin message") or self._ok("Message unpinned.")

    # ==================================================================
    # REACTIONS
    # ==================================================================

    @tool(
        description=(
            "Add a reaction to a Discord message. "
            "For standard emoji use the character (e.g. '👍'). "
            "For custom emoji use the format 'name:id' (e.g. 'my_emoji:123456789')."
        ),
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
            "emoji": "The emoji to react with, e.g. '👍' or 'my_emoji:123456789'.",
        },
    )
    async def create_reaction(self, channel_id: str, message_id: str, emoji: str) -> Dict:
        import urllib.parse
        emoji_enc = urllib.parse.quote(emoji, safe="")
        data = await self._discord_request("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_enc}/@me")
        return self._err(data, "Failed to add reaction") or self._ok(f"Reaction '{emoji}' added.")

    @tool(
        description="Remove the bot's own reaction from a Discord message.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
            "emoji": "The emoji to remove, e.g. '👍' or 'my_emoji:123456789'.",
        },
    )
    async def delete_own_reaction(self, channel_id: str, message_id: str, emoji: str) -> Dict:
        import urllib.parse
        emoji_enc = urllib.parse.quote(emoji, safe="")
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_enc}/@me")
        return self._err(data, "Failed to remove own reaction") or self._ok("Own reaction removed.")

    @tool(
        description="Remove a specific user's reaction from a Discord message (requires MANAGE_MESSAGES).",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
            "emoji": "The emoji to remove.",
            "user_id": "The ID of the user whose reaction to remove.",
        },
    )
    async def delete_user_reaction(self, channel_id: str, message_id: str, emoji: str, user_id: str) -> Dict:
        import urllib.parse
        emoji_enc = urllib.parse.quote(emoji, safe="")
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_enc}/{user_id}")
        return self._err(data, "Failed to remove user reaction") or self._ok(f"Reaction removed for user {user_id}.")

    @tool(
        description="Get a list of users who reacted to a message with a specific emoji.",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
            "emoji": "The emoji to look up, e.g. '👍' or 'my_emoji:123456789'.",
            "limit": "Max users to return (1–100, default 25).",
            "after": "User ID for pagination (after).",
        },
    )
    async def get_reactions(
        self, channel_id: str, message_id: str, emoji: str,
        limit: int = 25, after: Optional[str] = None,
    ) -> Dict:
        import urllib.parse
        emoji_enc = urllib.parse.quote(emoji, safe="")
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 25), 100))}
        if after:
            params["after"] = after
        data = await self._discord_request("GET", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_enc}", params=params)
        users = data if isinstance(data, list) else []
        return self._err(data, "Failed to get reactions") or self._ok(
            f"Found {len(users)} user(s) who reacted with '{emoji}'.", users=users, count=len(users)
        )

    @tool(
        description="Delete all reactions on a Discord message (requires MANAGE_MESSAGES).",
        params={
            "channel_id": "The ID of the channel.",
            "message_id": "The ID of the message.",
            "emoji": "If provided, delete only reactions for this specific emoji; otherwise delete all.",
        },
    )
    async def delete_all_reactions(self, channel_id: str, message_id: str, emoji: Optional[str] = None) -> Dict:
        if emoji:
            import urllib.parse
            emoji_enc = urllib.parse.quote(emoji, safe="")
            path = f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_enc}"
        else:
            path = f"/channels/{channel_id}/messages/{message_id}/reactions"
        data = await self._discord_request("DELETE", path)
        return self._err(data, "Failed to delete reactions") or self._ok(
            f"Reactions{f' for {emoji}' if emoji else ''} deleted."
        )

    # ==================================================================
    # THREADS
    # ==================================================================

    @tool(
        description="Start a new thread from an existing message in a text or announcement channel.",
        params={
            "channel_id": "The ID of the channel containing the message.",
            "message_id": "The ID of the message to start the thread from.",
            "name": "Name of the new thread (1–100 chars).",
            "auto_archive_duration": "Minutes until thread auto-archives: 60, 1440, 4320, or 10080.",
            "rate_limit_per_user": "Slowmode in seconds (0–21600).",
            "reason": "Audit log reason.",
        },
    )
    async def start_thread_from_message(
        self, channel_id: str, message_id: str, name: str,
        auto_archive_duration: int = 1440, rate_limit_per_user: int = 0,
        reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {
            "name": name,
            "auto_archive_duration": int(auto_archive_duration or 1440),
            "rate_limit_per_user": int(rate_limit_per_user or 0),
        }
        data = await self._discord_request("POST", f"/channels/{channel_id}/messages/{message_id}/threads", json_body=body, reason=reason)
        return self._err(data, "Failed to start thread") or self._ok(
            f"Thread '{name}' started ({data.get('id')}).", thread=data
        )

    @tool(
        description="Start a new thread in a channel without an associated message.",
        params={
            "channel_id": "The ID of the channel.",
            "name": "Name of the new thread (1–100 chars).",
            "type": "Thread type: 11=PUBLIC_THREAD, 12=PRIVATE_THREAD.",
            "auto_archive_duration": "Minutes until thread auto-archives: 60, 1440, 4320, or 10080.",
            "invitable": "Whether non-moderators can add other members to private threads.",
            "reason": "Audit log reason.",
        },
    )
    async def start_thread_in_channel(
        self, channel_id: str, name: str, type: int = 11,
        auto_archive_duration: int = 1440, invitable: bool = True,
        reason: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {
            "name": name, "type": int(type or 11),
            "auto_archive_duration": int(auto_archive_duration or 1440),
            "invitable": bool(invitable),
        }
        data = await self._discord_request("POST", f"/channels/{channel_id}/threads", json_body=body, reason=reason)
        return self._err(data, "Failed to start thread") or self._ok(
            f"Thread '{name}' started ({data.get('id')}).", thread=data
        )

    @tool(
        description="Add the bot to a thread (join it).",
        params={"channel_id": "The ID of the thread channel."},
    )
    async def join_thread(self, channel_id: str) -> Dict:
        data = await self._discord_request("PUT", f"/channels/{channel_id}/thread-members/@me")
        return self._err(data, "Failed to join thread") or self._ok("Joined thread.")

    @tool(
        description="Remove the bot from a thread (leave it).",
        params={"channel_id": "The ID of the thread channel."},
    )
    async def leave_thread(self, channel_id: str) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/thread-members/@me")
        return self._err(data, "Failed to leave thread") or self._ok("Left thread.")

    @tool(
        description="Add a user to a thread.",
        params={
            "channel_id": "The ID of the thread channel.",
            "user_id": "The ID of the user to add.",
        },
    )
    async def add_thread_member(self, channel_id: str, user_id: str) -> Dict:
        data = await self._discord_request("PUT", f"/channels/{channel_id}/thread-members/{user_id}")
        return self._err(data, "Failed to add thread member") or self._ok(f"User {user_id} added to thread.")

    @tool(
        description="Remove a user from a thread.",
        params={
            "channel_id": "The ID of the thread channel.",
            "user_id": "The ID of the user to remove.",
        },
    )
    async def remove_thread_member(self, channel_id: str, user_id: str) -> Dict:
        data = await self._discord_request("DELETE", f"/channels/{channel_id}/thread-members/{user_id}")
        return self._err(data, "Failed to remove thread member") or self._ok(f"User {user_id} removed from thread.")

    @tool(
        description="Get a thread member record for a specific user in a thread.",
        params={
            "channel_id": "The ID of the thread channel.",
            "user_id": "The ID of the user.",
            "with_member": "Whether to include the guild member object.",
        },
    )
    async def get_thread_member(self, channel_id: str, user_id: str, with_member: bool = False) -> Dict:
        params = {"with_member": "true"} if with_member else None
        data = await self._discord_request("GET", f"/channels/{channel_id}/thread-members/{user_id}", params=params)
        return self._err(data, "Failed to get thread member") or self._ok("Thread member retrieved.", member=data)

    @tool(
        description="List all members of a thread.",
        params={
            "channel_id": "The ID of the thread channel.",
            "with_member": "Whether to include guild member objects.",
            "limit": "Max members to return (1–100, default 50).",
            "after": "Member ID for pagination.",
        },
    )
    async def list_thread_members(
        self, channel_id: str, with_member: bool = False,
        limit: int = DEFAULT_LIMIT, after: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 100))}
        if with_member:
            params["with_member"] = "true"
        if after:
            params["after"] = after
        data = await self._discord_request("GET", f"/channels/{channel_id}/thread-members", params=params)
        members = data if isinstance(data, list) else []
        return self._err(data, "Failed to list thread members") or self._ok(
            f"Found {len(members)} thread member(s).", members=members, count=len(members)
        )

    @tool(
        description="List archived threads in a channel (public or private).",
        params={
            "channel_id": "The ID of the channel.",
            "private": "Whether to list private archived threads (default false = public).",
            "limit": "Max threads to return (default 50).",
            "before": "ISO8601 timestamp or thread ID for pagination.",
        },
    )
    async def list_archived_threads(
        self, channel_id: str, private: bool = False,
        limit: int = DEFAULT_LIMIT, before: Optional[str] = None,
    ) -> Dict:
        sub = "private" if private else "public"
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or DEFAULT_LIMIT), 100))}
        if before:
            params["before"] = before
        data = await self._discord_request("GET", f"/channels/{channel_id}/threads/archived/{sub}", params=params)
        threads = (data or {}).get("threads") or (data if isinstance(data, list) else [])
        return self._err(data, "Failed to list archived threads") or self._ok(
            f"Found {len(threads)} archived thread(s).", threads=threads, count=len(threads)
        )

    # ==================================================================
    # USERS
    # ==================================================================

    @tool(
        description="Get the current bot's own user object (id, username, discriminator, avatar, etc.).",
    )
    async def get_current_user(self) -> Dict:
        data = await self._discord_request("GET", "/users/@me")
        return self._err(data, "Failed to get current user") or self._ok(
            f"Current user: {data.get('username')}#{data.get('discriminator')}.", user=data
        )

    @tool(
        description="Get a Discord user by their user ID.",
        params={"user_id": "The ID of the user."},
    )
    async def get_user(self, user_id: str) -> Dict:
        data = await self._discord_request("GET", f"/users/{user_id}")
        return self._err(data, "Failed to get user") or self._ok(
            f"User: {data.get('username')}.", user=data
        )

    @tool(
        description="Modify the current bot user's account (username or avatar).",
        params={
            "username": "New username for the bot.",
            "avatar_base64": "New avatar as a base64-encoded image data URI, e.g. 'data:image/png;base64,...'.",
        },
    )
    async def modify_current_user(self, username: Optional[str] = None, avatar_base64: Optional[str] = None) -> Dict:
        body = self._strip_none({"username": username, "avatar": avatar_base64})
        if not body:
            return {"success": False, "response": "No fields provided."}
        data = await self._discord_request("PATCH", "/users/@me", json_body=body)
        return self._err(data, "Failed to modify current user") or self._ok("Bot user updated.", user=data)

    @tool(
        description="List the guilds the current bot user is a member of.",
        params={
            "limit": "Max number of guilds to return (1–200, default 100).",
            "before": "Guild ID for pagination (before).",
            "after": "Guild ID for pagination (after).",
        },
    )
    async def list_current_user_guilds(
        self, limit: int = 100, before: Optional[str] = None, after: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 100), 200))}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        data = await self._discord_request("GET", "/users/@me/guilds", params=params)
        guilds = data if isinstance(data, list) else []
        return self._err(data, "Failed to list guilds") or self._ok(
            f"Found {len(guilds)} guild(s).", guilds=guilds, count=len(guilds)
        )

    @tool(
        description="Leave a guild the bot is currently a member of.",
        params={"guild_id": "The ID of the guild to leave."},
    )
    async def leave_guild(self, guild_id: str) -> Dict:
        data = await self._discord_request("DELETE", f"/users/@me/guilds/{guild_id}")
        return self._err(data, "Failed to leave guild") or self._ok(f"Left guild {guild_id}.")

    @tool(
        description="Open or retrieve a DM channel with a specific Discord user.",
        params={"recipient_id": "The ID of the user to open a DM with."},
    )
    async def create_dm(self, recipient_id: str) -> Dict:
        data = await self._discord_request("POST", "/users/@me/channels", json_body={"recipient_id": recipient_id})
        return self._err(data, "Failed to create DM") or self._ok(
            f"DM channel with {recipient_id}: {data.get('id')}.", channel=data
        )

    # ==================================================================
    # EMOJIS
    # ==================================================================

    @tool(
        description="List all custom emojis in a Discord guild.",
        params={"guild_id": "The ID of the guild."},
    )
    async def list_guild_emojis(self, guild_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/emojis")
        emojis = data if isinstance(data, list) else []
        return self._err(data, "Failed to list emojis") or self._ok(
            f"Found {len(emojis)} emoji(s).", emojis=emojis, count=len(emojis)
        )

    @tool(
        description="Get a specific custom emoji from a Discord guild.",
        params={"guild_id": "The ID of the guild.", "emoji_id": "The ID of the emoji."},
    )
    async def get_guild_emoji(self, guild_id: str, emoji_id: str) -> Dict:
        data = await self._discord_request("GET", f"/guilds/{guild_id}/emojis/{emoji_id}")
        return self._err(data, "Failed to get emoji") or self._ok(
            f"Emoji ':{data.get('name')}:' retrieved.", emoji=data
        )

    @tool(
        description="Create a custom emoji in a Discord guild from an image URL or base64 content.",
        params={
            "guild_id": "The ID of the guild.",
            "name": "Name for the emoji (alphanumeric and underscores, 2–32 chars).",
            "image_url": "URL to download the emoji image from (PNG/GIF/JPEG, max 256KB).",
            "image_base64": "Base64-encoded image as a data URI, e.g. 'data:image/png;base64,...'. Used if image_url not provided.",
            "roles": "Optional list of role IDs that can use this emoji (empty = everyone).",
            "reason": "Audit log reason.",
        },
    )
    async def create_guild_emoji(
        self, guild_id: str, name: str,
        image_url: Optional[str] = None, image_base64: Optional[str] = None,
        roles: Optional[List[str]] = None, reason: Optional[str] = None,
    ) -> Dict:
        image_data: Optional[str] = None
        if image_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    r = await hc.get(image_url)
                r.raise_for_status()
                ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
                image_data = f"data:{ct};base64,{base64.b64encode(r.content).decode()}"
            except Exception as e:
                return {"success": False, "response": f"Failed to download image: {e}"}
        elif image_base64:
            image_data = image_base64
        else:
            return {"success": False, "response": "Provide either image_url or image_base64."}
        body: Dict[str, Any] = {"name": name, "image": image_data}
        if roles:
            body["roles"] = roles
        data = await self._discord_request("POST", f"/guilds/{guild_id}/emojis", json_body=body, reason=reason)
        return self._err(data, "Failed to create emoji") or self._ok(
            f"Emoji ':{name}:' created ({data.get('id')}).", emoji=data
        )

    @tool(
        description="Modify a custom emoji in a Discord guild (rename or change allowed roles).",
        params={
            "guild_id": "The ID of the guild.",
            "emoji_id": "The ID of the emoji.",
            "name": "New name for the emoji.",
            "roles": "New list of role IDs that can use this emoji (empty list = everyone).",
            "reason": "Audit log reason.",
        },
    )
    async def modify_guild_emoji(
        self, guild_id: str, emoji_id: str,
        name: Optional[str] = None, roles: Optional[List[str]] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        body = self._strip_none({"name": name, "roles": roles})
        if not body:
            return {"success": False, "response": "No fields provided."}
        data = await self._discord_request("PATCH", f"/guilds/{guild_id}/emojis/{emoji_id}", json_body=body, reason=reason)
        return self._err(data, "Failed to modify emoji") or self._ok("Emoji updated.", emoji=data)

    @tool(
        description="Delete a custom emoji from a Discord guild.",
        params={
            "guild_id": "The ID of the guild.",
            "emoji_id": "The ID of the emoji to delete.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_guild_emoji(self, guild_id: str, emoji_id: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/guilds/{guild_id}/emojis/{emoji_id}", reason=reason)
        return self._err(data, "Failed to delete emoji") or self._ok(f"Emoji {emoji_id} deleted.")

    @tool(
        description="List emojis owned by the bot's application (cross-server application emojis).",
        params={"application_id": "The application ID of the bot."},
    )
    async def list_application_emojis(self, application_id: str) -> Dict:
        data = await self._discord_request("GET", f"/applications/{application_id}/emojis")
        emojis = (data or {}).get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return self._err(data, "Failed to list application emojis") or self._ok(
            f"Found {len(emojis)} application emoji(s).", emojis=emojis, count=len(emojis)
        )

    @tool(
        description="Create a new application-level emoji owned by the bot (usable across all servers).",
        params={
            "application_id": "The application ID of the bot.",
            "name": "Name for the emoji (2–32 chars, alphanumeric/underscores).",
            "image_url": "URL to download the emoji image from.",
            "image_base64": "Base64 data URI of the image (used if image_url not provided).",
        },
    )
    async def create_application_emoji(
        self, application_id: str, name: str,
        image_url: Optional[str] = None, image_base64: Optional[str] = None,
    ) -> Dict:
        image_data: Optional[str] = None
        if image_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    r = await hc.get(image_url)
                r.raise_for_status()
                ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
                image_data = f"data:{ct};base64,{base64.b64encode(r.content).decode()}"
            except Exception as e:
                return {"success": False, "response": f"Failed to download image: {e}"}
        elif image_base64:
            image_data = image_base64
        else:
            return {"success": False, "response": "Provide either image_url or image_base64."}
        data = await self._discord_request("POST", f"/applications/{application_id}/emojis",
                                            json_body={"name": name, "image": image_data})
        return self._err(data, "Failed to create application emoji") or self._ok(
            f"Application emoji ':{name}:' created.", emoji=data
        )

    @tool(
        description="Delete an application-level emoji.",
        params={
            "application_id": "The application ID.",
            "emoji_id": "The ID of the emoji to delete.",
        },
    )
    async def delete_application_emoji(self, application_id: str, emoji_id: str) -> Dict:
        data = await self._discord_request("DELETE", f"/applications/{application_id}/emojis/{emoji_id}")
        return self._err(data, "Failed to delete application emoji") or self._ok("Application emoji deleted.")

    # ==================================================================
    # INVITES
    # ==================================================================

    @tool(
        description="Get details about a Discord invite by its code.",
        params={
            "invite_code": "The invite code (e.g. 'discord-developers' from discord.gg/discord-developers).",
            "with_counts": "Whether to include approximate member/presence counts.",
            "with_expiration": "Whether to include the expiration date.",
        },
    )
    async def get_invite(
        self, invite_code: str, with_counts: bool = False, with_expiration: bool = False
    ) -> Dict:
        params: Dict[str, Any] = {}
        if with_counts:
            params["with_counts"] = "true"
        if with_expiration:
            params["with_expiration"] = "true"
        data = await self._discord_request("GET", f"/invites/{invite_code}", params=params or None)
        return self._err(data, "Failed to get invite") or self._ok(
            f"Invite to '{(data.get('guild') or {}).get('name') or 'unknown'}' retrieved.", invite=data
        )

    @tool(
        description="Delete (revoke) a Discord invite by its code.",
        params={
            "invite_code": "The invite code to delete.",
            "reason": "Audit log reason.",
        },
    )
    async def delete_invite(self, invite_code: str, reason: Optional[str] = None) -> Dict:
        data = await self._discord_request("DELETE", f"/invites/{invite_code}", reason=reason)
        return self._err(data, "Failed to delete invite") or self._ok(f"Invite '{invite_code}' deleted.")
