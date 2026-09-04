"""
Google Meet functions class for functions_wrapper plugin.

Each public @tool method maps to one Google Meet REST API v2 action.
Reference: https://developers.google.com/workspace/meet/api/reference/rest/v2

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

MEET_API_BASE = "https://meet.googleapis.com/v2"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE_RECORDS = 100
MAX_PAGE_SIZE_PARTICIPANTS = 250
_REQUEST_MAX_RETRIES = 3
_REQUEST_BASE_DELAY = 1.0
_REQUEST_MAX_DELAY = 30.0


class GoogleMeetFunctions(ConnectedServiceToolAgent):
    """
    Google Meet tool functions. Each @tool method is a Meet API v2 capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Google Meet: create/get/update meeting spaces, end active conferences, "
        "list/get conference records, participants, participant sessions, "
        "recordings, transcripts, transcript entries, and smart notes."
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
        raise NotImplementedError("GoogleMeetFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GoogleMeetFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GoogleMeetFunctions initialized")

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

    async def _meet_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
        _retry_count: int = 0,
    ) -> Optional[Dict]:
        url = (
            f"{MEET_API_BASE}{path}"
            if path.startswith("/")
            else f"{MEET_API_BASE}/{path}"
        )
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        if json_body is not None:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        else:
            r = await self._httpx_client.request(method, url, params=params)
        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._meet_request(
                method, path, json_body=json_body, params=params, retry_401=False
            )
        if r.status_code in (429, 500, 502, 503, 504) and _retry_count < _REQUEST_MAX_RETRIES:
            delay = min(_REQUEST_BASE_DELAY * (2 ** _retry_count), _REQUEST_MAX_DELAY)
            logger.warning(
                "Meet API %s %s -> %s; retrying in %.1fs (%d/%d)",
                method, path, r.status_code, delay, _retry_count + 1, _REQUEST_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            return await self._meet_request(
                method, path, json_body=json_body, params=params,
                retry_401=False, _retry_count=_retry_count + 1,
            )
        if r.status_code >= 400:
            logger.warning("Meet API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            if json_body is not None:
                try:
                    logger.warning("Meet API request body: %s", json.dumps(json_body, default=str)[:2000])
                except Exception:
                    pass
            return None
        if r.status_code == 204 or not r.content:
            return {}
        try:
            data = r.json()
            try:
                snippet = json.dumps(data, default=str)
                if len(snippet) > 3000:
                    snippet = snippet[:3000] + "... (truncated)"
                logger.info("Meet API %s %s -> %s response: %s", method, path, r.status_code, snippet)
            except Exception:
                pass
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # @tool public methods — Spaces
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Create a new Google Meet meeting space. A space is a persistent virtual location "
            "where conferences are hosted; it is created without an active conference until someone joins. "
            "Optionally configure access control (who can join), moderation, and automatic "
            "recording/transcription/smart-notes generation. "
            "Returns the new space's resource name, meeting URI (join link), and meeting code."
        ),
        params={
            "access_type": (
                "Who is allowed to join the space without knocking. One of: "
                "'OPEN' — anyone with the join info can join directly; "
                "'TRUSTED' — members of the host's organization and people explicitly invited can join directly; "
                "'RESTRICTED' — only people/groups explicitly granted access can join directly, others must knock; "
                "'ACCESS_TYPE_UNSPECIFIED' — use the organization's default setting. "
                "If omitted, the organization's default is used."
            ),
            "entry_point_access": (
                "Which entry points (Meet apps/clients) are allowed to join the space. One of: "
                "'ALL' — all entry points are permitted (default); "
                "'CREATOR_APP_ONLY' — only entry points owned by the creator's app can join. "
                "Setting this requires the meetings.space.created scope and cannot be changed after space creation."
            ),
            "moderation": (
                "Whether host moderation is enabled for the space. One of: "
                "'ON' — moderation is enabled, enabling moderationRestrictions; "
                "'OFF' — moderation is disabled. "
                "If omitted, the organization's default is used."
            ),
            "moderation_restrictions": (
                "Object describing restrictions applied to participants when moderation is ON. Fields: "
                "chat_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION') — who can send in-meeting chat messages; "
                "reaction_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION') — who can send reactions; "
                "present_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION') — who can present their screen; "
                "default_join_as_viewer_type ('ON' | 'OFF') — whether participants join as viewers "
                "(without interaction privileges) by default. "
                "Example: {\"chat_restriction\": \"HOSTS_ONLY\", \"reaction_restriction\": \"NO_RESTRICTION\", "
                "\"present_restriction\": \"HOSTS_ONLY\", \"default_join_as_viewer_type\": \"OFF\"}."
            ),
            "attendance_report_generation_type": (
                "Whether an attendance report should be generated for conferences held in this space. One of: "
                "'GENERATE_REPORT' — generate an attendance report and email it to the space organizer; "
                "'DO_NOT_GENERATE' — do not generate a report (default)."
            ),
            "artifact_config": (
                "Object configuring which artifacts are generated automatically when a conference starts. Fields: "
                "recording_config (object with auto_recording_generation: 'ON' | 'OFF'); "
                "transcription_config (object with auto_transcription_generation: 'ON' | 'OFF'); "
                "smart_notes_config (object with auto_smart_notes_generation: 'ON' | 'OFF'). "
                "Example — auto-record and auto-transcribe, but no smart notes: "
                "{\"recording_config\": {\"auto_recording_generation\": \"ON\"}, "
                "\"transcription_config\": {\"auto_transcription_generation\": \"ON\"}, "
                "\"smart_notes_config\": {\"auto_smart_notes_generation\": \"OFF\"}}."
            ),
        },
    )
    async def create_space(
        self,
        access_type: Optional[str] = None,
        entry_point_access: Optional[str] = None,
        moderation: Optional[str] = None,
        moderation_restrictions: Optional[Dict[str, Any]] = None,
        attendance_report_generation_type: Optional[str] = None,
        artifact_config: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        config, _ = _build_space_config(
            access_type=access_type,
            entry_point_access=entry_point_access,
            moderation=moderation,
            moderation_restrictions=moderation_restrictions,
            attendance_report_generation_type=attendance_report_generation_type,
            artifact_config=artifact_config,
        )
        body: Dict[str, Any] = {"config": config} if config else {}
        created = await self._meet_request("POST", "/spaces", json_body=body)
        if not created or not created.get("name"):
            return {"success": False, "response": "Could not create the meeting space."}
        return {
            "success": True,
            "response": _format_space_created_response(created),
            "space": created,
        }

    @tool(
        description=(
            "Retrieve details of a Google Meet meeting space. "
            "Returns the space's resource name, meeting URI (join link), meeting code, full configuration "
            "(access type, moderation, artifact settings), phone dial-in access numbers, and the "
            "active conference record name if a conference is currently in progress."
        ),
        params={
            "name": (
                "The space to retrieve. Accepts either the full resource name "
                "(e.g. 'spaces/abcd-efgh-ijk') or just the space ID / meeting code "
                "(e.g. 'abcd-efgh-ijk'). Get this from create_space, list_conference_records, "
                "or the meeting URL."
            ),
        },
    )
    async def get_space(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        resource = _normalize_resource_name(name, "spaces")
        data = await self._meet_request("GET", f"/{resource}")
        if not data:
            return {"success": False, "response": "Space not found.", "name": name}
        return {"success": True, "response": _format_space_summary(data), "space": data}

    @tool(
        description=(
            "Update (patch) the configuration of an existing Google Meet meeting space. "
            "Only the fields you provide are changed; fields you omit remain unchanged. "
            "Use this to change access control, moderation, restrictions, attendance reports, "
            "or automatic recording/transcription/smart-notes settings. "
            "Returns the updated space resource."
        ),
        params={
            "name": (
                "The space to update. Accepts either the full resource name "
                "(e.g. 'spaces/abcd-efgh-ijk') or just the space ID. Required."
            ),
            "access_type": (
                "New access type. One of: 'OPEN', 'TRUSTED', 'RESTRICTED', 'ACCESS_TYPE_UNSPECIFIED'. "
                "Controls who can join the space without knocking."
            ),
            "entry_point_access": (
                "New entry point access. One of: 'ALL', 'CREATOR_APP_ONLY'. "
                "Note: this field cannot be changed after the space is created."
            ),
            "moderation": (
                "New moderation setting. One of: 'ON', 'OFF'."
            ),
            "moderation_restrictions": (
                "Object with updated restrictions (only used when moderation is ON). Fields: "
                "chat_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION'), "
                "reaction_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION'), "
                "present_restriction ('HOSTS_ONLY' | 'NO_RESTRICTION'), "
                "default_join_as_viewer_type ('ON' | 'OFF'). "
                "Example: {\"chat_restriction\": \"NO_RESTRICTION\"}."
            ),
            "attendance_report_generation_type": (
                "New attendance report setting. One of: 'GENERATE_REPORT', 'DO_NOT_GENERATE'."
            ),
            "artifact_config": (
                "Object with updated auto-generation settings. Fields: "
                "recording_config.auto_recording_generation ('ON' | 'OFF'), "
                "transcription_config.auto_transcription_generation ('ON' | 'OFF'), "
                "smart_notes_config.auto_smart_notes_generation ('ON' | 'OFF'). "
                "Example: {\"recording_config\": {\"auto_recording_generation\": \"OFF\"}}."
            ),
        },
    )
    async def update_space(
        self,
        name: str,
        access_type: Optional[str] = None,
        entry_point_access: Optional[str] = None,
        moderation: Optional[str] = None,
        moderation_restrictions: Optional[Dict[str, Any]] = None,
        attendance_report_generation_type: Optional[str] = None,
        artifact_config: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        config, mask_paths = _build_space_config(
            access_type=access_type,
            entry_point_access=entry_point_access,
            moderation=moderation,
            moderation_restrictions=moderation_restrictions,
            attendance_report_generation_type=attendance_report_generation_type,
            artifact_config=artifact_config,
        )
        if not config or not mask_paths:
            return {
                "success": False,
                "response": (
                    "Provide at least one field to update (access_type, entry_point_access, "
                    "moderation, moderation_restrictions, attendance_report_generation_type, "
                    "artifact_config)."
                ),
            }
        resource = _normalize_resource_name(name, "spaces")
        body = {"config": config}
        params = {"updateMask": ",".join(mask_paths)}
        updated = await self._meet_request("PATCH", f"/{resource}", json_body=body, params=params)
        if not updated:
            return {"success": False, "response": "Could not update the meeting space."}
        return {"success": True, "response": _format_space_summary(updated), "space": updated}

    @tool(
        description=(
            "Terminate the currently active conference in a Google Meet space, disconnecting all "
            "participants. The space itself is not deleted and can be reused for future conferences. "
            "Returns confirmation that the active conference was ended. If no conference is currently "
            "active, this call has no effect."
        ),
        params={
            "name": (
                "The space whose active conference should be ended. Accepts either the full "
                "resource name (e.g. 'spaces/abcd-efgh-ijk') or just the space ID. Required."
            ),
        },
    )
    async def end_active_conference(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        resource = _normalize_resource_name(name, "spaces")
        result = await self._meet_request("POST", f"/{resource}:endActiveConference")
        if result is None:
            return {"success": False, "response": "Could not end the active conference."}
        return {"success": True, "response": "Active conference ended.", "name": name}

    # ------------------------------------------------------------------
    # @tool public methods — Conference records
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List Google Meet conference records (past and ongoing conferences). "
            "Conference records are retained for 30 days after a conference ends. "
            "Results are ordered by start time, most recent first. "
            "Returns each conference record's resource name, the space it was held in, "
            "and its start/end timestamps. "
            "Conference record names returned here are required by get_conference_record, "
            "list_participants, list_recordings, list_transcripts, and list_smart_notes."
        ),
        params={
            "filter": (
                "Optional filter expression (EBNF syntax) to narrow results. "
                "Filterable fields: space.meeting_code, space.name, start_time, end_time. "
                "Examples: "
                "'space.name = \"spaces/abcdEFGh12\"' — conferences held in a specific space; "
                "'space.meeting_code = \"abc-mnop-xyz\"' — conferences with a specific meeting code; "
                "'start_time>=\"2026-01-01T00:00:00.000Z\" AND start_time<=\"2026-01-02T00:00:00.000Z\"' "
                "— conferences that started within a date range; "
                "'end_time IS NULL' — currently ongoing conferences (no end time yet)."
            ),
            "page_size": (
                "Maximum number of conference records to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_conference_records call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_conference_records(
        self,
        filter: Optional[str] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if filter:
            params["filter"] = filter
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", "/conferenceRecords", params=params)
        if not data:
            return {"success": True, "response": "No conference records found.", "conference_records": [], "count": 0}
        items = data.get("conferenceRecords") or []
        return {
            "success": True,
            "response": f"Found {len(items)} conference record(s).",
            "conference_records": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single Google Meet conference record by its resource name. "
            "Returns the space it was held in, and start/end/expiry timestamps. "
            "Get the conference record name from list_conference_records."
        ),
        params={
            "name": (
                "The conference record to retrieve. Accepts either the full resource name "
                "(e.g. 'conferenceRecords/abcdEFGh12') or just the ID (e.g. 'abcdEFGh12'). Required."
            ),
        },
    )
    async def get_conference_record(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        resource = _normalize_resource_name(name, "conferenceRecords")
        data = await self._meet_request("GET", f"/{resource}")
        if not data:
            return {"success": False, "response": "Conference record not found.", "name": name}
        return {"success": True, "response": _format_conference_record_summary(data), "conference_record": data}

    # ------------------------------------------------------------------
    # @tool public methods — Participants
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the participants of a Google Meet conference record. "
            "Each participant represents one signed-in user, anonymous user, or phone caller "
            "who joined the conference (possibly across multiple join/leave sessions). "
            "Returns each participant's resource name, display name, user type, and "
            "earliest join / latest leave timestamps. "
            "Participant names returned here are required by get_participant and "
            "list_participant_sessions."
        ),
        params={
            "conference_record": (
                "The conference record whose participants to list. Accepts either the full "
                "resource name (e.g. 'conferenceRecords/abcdEFGh12') or just the ID. Required. "
                "Get this from list_conference_records."
            ),
            "filter": (
                "Optional filter expression (EBNF syntax) to narrow results. "
                "Filterable fields: earliest_start_time, latest_end_time. "
                "Example: 'latest_end_time IS NULL' — only participants currently in the "
                "ongoing conference (haven't left yet)."
            ),
            "page_size": (
                "Maximum number of participants to return per page. Default 100, maximum 250. "
                "Values above 250 are coerced to 250."
            ),
            "page_token": (
                "Pagination token from a previous list_participants call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_participants(
        self,
        conference_record: str,
        filter: Optional[str] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not conference_record:
            return {"success": False, "response": "conference_record is required."}
        parent = _normalize_resource_name(conference_record, "conferenceRecords")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, 100, MAX_PAGE_SIZE_PARTICIPANTS)}
        if filter:
            params["filter"] = filter
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/participants", params=params)
        if not data:
            return {"success": True, "response": "No participants found.", "participants": [], "count": 0}
        items = data.get("participants") or []
        out = [_participant_summary(p) for p in items]
        return {
            "success": True,
            "response": f"Found {len(out)} participant(s).",
            "participants": items,
            "count": len(out),
            "next_page_token": data.get("nextPageToken"),
            "total_size": data.get("totalSize"),
        }

    @tool(
        description=(
            "Retrieve a single participant of a Google Meet conference record by resource name. "
            "Returns the participant's display name, user type (signed-in user, anonymous user, "
            "or phone user), and earliest join / latest leave timestamps. "
            "Get the participant name from list_participants."
        ),
        params={
            "name": (
                "Full resource name of the participant, in the format "
                "'conferenceRecords/{conferenceRecord}/participants/{participant}'. "
                "Get this from list_participants. Required."
            ),
        },
    )
    async def get_participant(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Participant not found.", "name": name}
        return {"success": True, "response": _participant_summary(data), "participant": data}

    @tool(
        description=(
            "List the individual join/leave sessions of a participant in a Google Meet conference. "
            "A participant can have multiple sessions if they joined and left the conference "
            "multiple times (e.g. dropped connection and rejoined). "
            "Returns each session's resource name and start/end timestamps. "
            "Get the participant name from list_participants."
        ),
        params={
            "participant": (
                "Full resource name of the participant whose sessions to list, in the format "
                "'conferenceRecords/{conferenceRecord}/participants/{participant}'. Required."
            ),
            "page_size": (
                "Maximum number of sessions to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_participant_sessions call's "
                "nextPageToken, used to fetch the next page of results."
            ),
        },
    )
    async def list_participant_sessions(
        self,
        participant: str,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not participant:
            return {"success": False, "response": "participant is required."}
        parent = participant.lstrip("/")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/participantSessions", params=params)
        if not data:
            return {"success": True, "response": "No participant sessions found.", "participant_sessions": [], "count": 0}
        items = data.get("participantSessions") or []
        return {
            "success": True,
            "response": f"Found {len(items)} participant session(s).",
            "participant_sessions": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single join/leave session of a conference participant by resource name. "
            "Returns the session's start and end timestamps (end is unset if the session is "
            "still active). Get the session name from list_participant_sessions."
        ),
        params={
            "name": (
                "Full resource name of the participant session, in the format "
                "'conferenceRecords/{conferenceRecord}/participants/{participant}/"
                "participantSessions/{participantSession}'. Required."
            ),
        },
    )
    async def get_participant_session(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Participant session not found.", "name": name}
        return {
            "success": True,
            "response": f"Session {data.get('name')}: {data.get('startTime', '')} - {data.get('endTime', 'ongoing')}.",
            "participant_session": data,
        }

    # ------------------------------------------------------------------
    # @tool public methods — Recordings
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the recordings generated for a Google Meet conference record. "
            "Most conferences have at most one recording. "
            "Returns each recording's resource name, state (STARTED, ENDED, or FILE_GENERATED), "
            "start/end timestamps, and the Google Drive destination (file ID and playback URL) "
            "once the recording file has been generated. "
            "Get the conference record name from list_conference_records."
        ),
        params={
            "conference_record": (
                "The conference record whose recordings to list. Accepts either the full "
                "resource name (e.g. 'conferenceRecords/abcdEFGh12') or just the ID. Required."
            ),
            "page_size": (
                "Maximum number of recordings to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_recordings call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_recordings(
        self,
        conference_record: str,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not conference_record:
            return {"success": False, "response": "conference_record is required."}
        parent = _normalize_resource_name(conference_record, "conferenceRecords")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/recordings", params=params)
        if not data:
            return {"success": True, "response": "No recordings found.", "recordings": [], "count": 0}
        items = data.get("recordings") or []
        return {
            "success": True,
            "response": f"Found {len(items)} recording(s).",
            "recordings": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single recording resource for a Google Meet conference by resource name. "
            "Returns its state (STARTED, ENDED, or FILE_GENERATED), start/end timestamps, and "
            "(once FILE_GENERATED) the Google Drive file ID and playback URL. "
            "Get the recording name from list_recordings."
        ),
        params={
            "name": (
                "Full resource name of the recording, in the format "
                "'conferenceRecords/{conferenceRecord}/recordings/{recording}'. Required."
            ),
        },
    )
    async def get_recording(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Recording not found.", "name": name}
        return {"success": True, "response": _recording_summary(data), "recording": data}

    # ------------------------------------------------------------------
    # @tool public methods — Transcripts
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the transcripts generated for a Google Meet conference record. "
            "Most conferences have at most one transcript. "
            "Returns each transcript's resource name, state (STARTED, ENDED, or FILE_GENERATED), "
            "start/end timestamps, and the Google Docs destination (document ID and export URI) "
            "once the transcript file has been generated. "
            "Transcript names returned here are required by get_transcript and "
            "list_transcript_entries. "
            "Get the conference record name from list_conference_records."
        ),
        params={
            "conference_record": (
                "The conference record whose transcripts to list. Accepts either the full "
                "resource name (e.g. 'conferenceRecords/abcdEFGh12') or just the ID. Required."
            ),
            "page_size": (
                "Maximum number of transcripts to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_transcripts call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_transcripts(
        self,
        conference_record: str,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not conference_record:
            return {"success": False, "response": "conference_record is required."}
        parent = _normalize_resource_name(conference_record, "conferenceRecords")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/transcripts", params=params)
        if not data:
            return {"success": True, "response": "No transcripts found.", "transcripts": [], "count": 0}
        items = data.get("transcripts") or []
        return {
            "success": True,
            "response": f"Found {len(items)} transcript(s).",
            "transcripts": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single transcript resource for a Google Meet conference by resource name. "
            "Returns its state (STARTED, ENDED, or FILE_GENERATED), start/end timestamps, and "
            "(once FILE_GENERATED) the Google Docs document ID and export URI. "
            "Get the transcript name from list_transcripts."
        ),
        params={
            "name": (
                "Full resource name of the transcript, in the format "
                "'conferenceRecords/{conferenceRecord}/transcripts/{transcript}'. Required."
            ),
        },
    )
    async def get_transcript(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Transcript not found.", "name": name}
        return {"success": True, "response": _transcript_summary(data), "transcript": data}

    @tool(
        description=(
            "List the individual spoken-text entries of a Google Meet transcript, in chronological "
            "order. Each entry corresponds to one continuous utterance by a single participant. "
            "Returns each entry's resource name, the participant who spoke, the transcribed text "
            "(up to 10,000 words), the language code, and start/end timestamps. "
            "Get the transcript name from list_transcripts."
        ),
        params={
            "transcript": (
                "Full resource name of the transcript whose entries to list, in the format "
                "'conferenceRecords/{conferenceRecord}/transcripts/{transcript}'. Required."
            ),
            "page_size": (
                "Maximum number of transcript entries to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_transcript_entries call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_transcript_entries(
        self,
        transcript: str,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not transcript:
            return {"success": False, "response": "transcript is required."}
        parent = transcript.lstrip("/")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/entries", params=params)
        if not data:
            return {"success": True, "response": "No transcript entries found.", "transcript_entries": [], "count": 0}
        items = data.get("transcriptEntries") or []
        return {
            "success": True,
            "response": f"Found {len(items)} transcript entry(ies).",
            "transcript_entries": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single transcript entry (one spoken utterance) by resource name. "
            "Returns the speaking participant, the transcribed text, language code, and "
            "start/end timestamps. Get the entry name from list_transcript_entries."
        ),
        params={
            "name": (
                "Full resource name of the transcript entry, in the format "
                "'conferenceRecords/{conferenceRecord}/transcripts/{transcript}/entries/{entry}'. "
                "Required."
            ),
        },
    )
    async def get_transcript_entry(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Transcript entry not found.", "name": name}
        text = (data.get("text") or "")[:300]
        return {
            "success": True,
            "response": f"[{data.get('startTime', '')}] {data.get('participant', '')}: {text}",
            "transcript_entry": data,
        }

    # ------------------------------------------------------------------
    # @tool public methods — Smart notes
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List the AI-generated smart notes (summary, action items, etc.) produced for a "
            "Google Meet conference record. "
            "Returns each smart notes resource's name, state (STARTED, ENDED, or FILE_GENERATED), "
            "start/end timestamps, and the Google Docs destination once generated. "
            "Get the conference record name from list_conference_records."
        ),
        params={
            "conference_record": (
                "The conference record whose smart notes to list. Accepts either the full "
                "resource name (e.g. 'conferenceRecords/abcdEFGh12') or just the ID. Required."
            ),
            "page_size": (
                "Maximum number of smart notes resources to return per page. Default 25, maximum 100. "
                "Values above 100 are coerced to 100."
            ),
            "page_token": (
                "Pagination token from a previous list_smart_notes call's nextPageToken, "
                "used to fetch the next page of results."
            ),
        },
    )
    async def list_smart_notes(
        self,
        conference_record: str,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict:
        if not conference_record:
            return {"success": False, "response": "conference_record is required."}
        parent = _normalize_resource_name(conference_record, "conferenceRecords")
        params: Dict[str, Any] = {"pageSize": _clamp_page_size(page_size, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE_RECORDS)}
        if page_token:
            params["pageToken"] = page_token
        data = await self._meet_request("GET", f"/{parent}/smartNotes", params=params)
        if not data:
            return {"success": True, "response": "No smart notes found.", "smart_notes": [], "count": 0}
        items = data.get("smartNotes") or []
        return {
            "success": True,
            "response": f"Found {len(items)} smart notes resource(s).",
            "smart_notes": items,
            "count": len(items),
            "next_page_token": data.get("nextPageToken"),
        }

    @tool(
        description=(
            "Retrieve a single smart notes resource for a Google Meet conference by resource name. "
            "Returns its state (STARTED, ENDED, or FILE_GENERATED), start/end timestamps, and "
            "(once FILE_GENERATED) the Google Docs document ID and export URI containing the "
            "AI-generated summary and action items. "
            "Get the smart notes name from list_smart_notes."
        ),
        params={
            "name": (
                "Full resource name of the smart notes resource, in the format "
                "'conferenceRecords/{conferenceRecord}/smartNotes/{smartNotes}'. Required."
            ),
        },
    )
    async def get_smart_note(self, name: str) -> Dict:
        if not name:
            return {"success": False, "response": "name is required."}
        data = await self._meet_request("GET", f"/{name.lstrip('/')}")
        if not data:
            return {"success": False, "response": "Smart notes resource not found.", "name": name}
        return {"success": True, "response": _artifact_summary("Smart notes", data), "smart_notes": data}


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

def _clamp_page_size(value: Optional[Any], default: int, maximum: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    if n <= 0:
        n = default
    return min(n, maximum)


def _normalize_resource_name(value: str, prefix: str) -> str:
    v = (value or "").strip().strip("/")
    if v.startswith(f"{prefix}/"):
        return v
    return f"{prefix}/{v}"


def _snake_to_camel(key: str) -> str:
    parts = key.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _convert_keys_to_camel(value: Any) -> Any:
    if isinstance(value, dict):
        return {_snake_to_camel(k) if isinstance(k, str) else k: _convert_keys_to_camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_keys_to_camel(v) for v in value]
    return value


def _build_space_config(
    *,
    access_type: Optional[str] = None,
    entry_point_access: Optional[str] = None,
    moderation: Optional[str] = None,
    moderation_restrictions: Optional[Dict[str, Any]] = None,
    attendance_report_generation_type: Optional[str] = None,
    artifact_config: Optional[Dict[str, Any]] = None,
) -> "tuple[Dict[str, Any], List[str]]":
    """
    Build a SpaceConfig dict from flat snake_case parameters, plus the list of
    'config.*' field paths that were provided (for use as updateMask in patch).
    """
    config: Dict[str, Any] = {}
    mask_paths: List[str] = []

    if access_type is not None:
        config["accessType"] = str(access_type)
        mask_paths.append("config.accessType")
    if entry_point_access is not None:
        config["entryPointAccess"] = str(entry_point_access)
        mask_paths.append("config.entryPointAccess")
    if moderation is not None:
        config["moderation"] = str(moderation)
        mask_paths.append("config.moderation")
    if moderation_restrictions is not None and isinstance(moderation_restrictions, dict):
        config["moderationRestrictions"] = _convert_keys_to_camel(moderation_restrictions)
        mask_paths.append("config.moderationRestrictions")
    if attendance_report_generation_type is not None:
        config["attendanceReportGenerationType"] = str(attendance_report_generation_type)
        mask_paths.append("config.attendanceReportGenerationType")
    if artifact_config is not None and isinstance(artifact_config, dict):
        config["artifactConfig"] = _convert_keys_to_camel(artifact_config)
        mask_paths.append("config.artifactConfig")

    return config, mask_paths


def _format_space_summary(space: Dict) -> str:
    parts = [f"Meeting space: {space.get('name')}."]
    if space.get("meetingUri"):
        parts.append(f"Join link: {space.get('meetingUri')}.")
    if space.get("meetingCode"):
        parts.append(f"Meeting code: {space.get('meetingCode')}.")
    config = space.get("config") or {}
    if config.get("accessType"):
        parts.append(f"Access type: {config.get('accessType')}.")
    if config.get("moderation"):
        parts.append(f"Moderation: {config.get('moderation')}.")
    active = space.get("activeConference") or {}
    if active.get("conferenceRecord"):
        parts.append(f"Active conference: {active.get('conferenceRecord')}.")
    return " ".join(parts)


def _format_space_created_response(space: Dict) -> str:
    parts = [f"Meeting space created: {space.get('name')}."]
    if space.get("meetingUri"):
        parts.append(f"Join link: {space.get('meetingUri')}.")
    if space.get("meetingCode"):
        parts.append(f"Meeting code: {space.get('meetingCode')}.")
    return " ".join(parts)


def _format_conference_record_summary(record: Dict) -> str:
    parts = [f"Conference record: {record.get('name')}."]
    if record.get("space"):
        parts.append(f"Space: {record.get('space')}.")
    if record.get("startTime"):
        parts.append(f"Started: {record.get('startTime')}.")
    if record.get("endTime"):
        parts.append(f"Ended: {record.get('endTime')}.")
    else:
        parts.append("Status: ongoing.")
    return " ".join(parts)


def _participant_summary(p: Dict) -> Dict:
    display_name = None
    user_type = None
    if p.get("signedinUser"):
        display_name = (p.get("signedinUser") or {}).get("displayName")
        user_type = "signedinUser"
    elif p.get("anonymousUser"):
        display_name = (p.get("anonymousUser") or {}).get("displayName")
        user_type = "anonymousUser"
    elif p.get("phoneUser"):
        display_name = (p.get("phoneUser") or {}).get("displayName")
        user_type = "phoneUser"
    return {
        "name": p.get("name"),
        "displayName": display_name,
        "userType": user_type,
        "earliestStartTime": p.get("earliestStartTime"),
        "latestEndTime": p.get("latestEndTime"),
    }


def _recording_summary(r: Dict) -> str:
    parts = [f"Recording {r.get('name')}: state={r.get('state')}."]
    drive = r.get("driveDestination") or {}
    if drive.get("exportUri"):
        parts.append(f"Playback: {drive.get('exportUri')}.")
    return " ".join(parts)


def _transcript_summary(t: Dict) -> str:
    parts = [f"Transcript {t.get('name')}: state={t.get('state')}."]
    docs = t.get("docsDestination") or {}
    if docs.get("exportUri"):
        parts.append(f"Document: {docs.get('exportUri')}.")
    return " ".join(parts)


def _artifact_summary(label: str, a: Dict) -> str:
    parts = [f"{label} {a.get('name')}: state={a.get('state')}."]
    docs = a.get("docsDestination") or {}
    if docs.get("exportUri"):
        parts.append(f"Document: {docs.get('exportUri')}.")
    return " ".join(parts)
