"""
Cal.com functions class for functions_wrapper plugin.

Each public @tool method is one Cal.com action exposed to the LLM (mirrors the
structure of gmail_functions.py / google_calendar_functions.py). Covers the
user-level and team-level Cal.com v2 API scopes: bookings, event types,
availability slots, schedules, calendars, profile, out-of-office, webhooks,
and team bookings/event-types/memberships. Org-level admin scopes (roles,
attributes, delegation credentials, managed orgs) are out of scope -- those
require an Enterprise org-owner connection and aren't useful for a typical
connected Cal.com account.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"].
The Cal.com OAuth connection backing this tool needs the following scopes:
BOOKING_READ BOOKING_WRITE EVENT_TYPE_READ EVENT_TYPE_WRITE SCHEDULE_READ
SCHEDULE_WRITE APPS_READ PROFILE_READ PROFILE_WRITE WEBHOOK_READ WEBHOOK_WRITE
TEAM_BOOKING_READ TEAM_EVENT_TYPE_READ TEAM_MEMBERSHIP_READ TEAM_PROFILE_READ

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider --
all natural-language-to-tool-call reasoning is done by the outer ReAct brain
(MCPAgentTool), the same way it is done for every other functions_wrapper tool.
Tool docstrings/param descriptions below are written to make that translation
reliable.

Cal.com's v2 API is versioned per-endpoint via a required `cal-api-version`
header (not a single global API version) -- each endpoint below passes the
exact version its Cal.com docs page specifies at the time this was written.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

CALCOM_API_BASE = "https://api.cal.com/v2"

# Per-endpoint cal-api-version header values (Cal.com versions endpoints
# individually, not globally -- see each method's _calcom_request call).
BOOKINGS_LIST_VERSION = "2026-05-01"
BOOKINGS_ACTIONS_VERSION = "2026-02-25"  # get/create/cancel/reschedule/confirm/decline/mark-absent
BOOKINGS_GUESTS_VERSION = "2024-08-13"
EVENT_TYPES_VERSION = "2024-06-14"
SLOTS_VERSION = "2024-09-04"
SCHEDULES_VERSION = "2024-06-11"

BOOKING_STATUSES = ("upcoming", "recurring", "past", "cancelled", "unconfirmed")
SORT_DIRECTIONS = ("asc", "desc")
WEBHOOK_TRIGGERS = (
    "BOOKING_CREATED", "BOOKING_PAYMENT_INITIATED", "BOOKING_PAID", "BOOKING_RESCHEDULED",
    "BOOKING_REQUESTED", "BOOKING_CANCELLED", "BOOKING_REJECTED", "BOOKING_NO_SHOW_UPDATED",
    "BOOKING_LOCATION_UPDATED", "FORM_SUBMITTED", "MEETING_ENDED", "MEETING_STARTED",
    "RECORDING_READY", "INSTANT_MEETING", "INSTANT_MEETING_ACCEPTED",
    "RECORDING_TRANSCRIPTION_GENERATED", "OOO_CREATED",
)
OOO_REASONS = ("unspecified", "vacation", "travel", "sick", "public_holiday")
SCHEDULE_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class CalcomFunctions(ConnectedServiceToolAgent):
    """
    Cal.com tool functions. Each @tool method is a Cal.com capability.
    FunctionsWrapperAgentTool sets _current_query / _step_results before each call.
    """

    TOOL_DESCRIPTION = (
        "Cal.com: list/get/create/cancel/reschedule/confirm/decline bookings, add guests, "
        "check available time slots, manage event types, schedules, and out-of-office entries, "
        "view connected calendars and busy times, read/update profile, manage webhooks, and "
        "list teams and view team bookings/event types/memberships."
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
        raise NotImplementedError("CalcomFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("CalcomFunctions: no access_token in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("CalcomFunctions initialized")

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

    async def _calcom_request(
        self,
        method: str,
        path: str,
        *,
        api_version: Optional[str] = None,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        retry_401: bool = True,
    ) -> Dict[str, Any]:
        """
        Returns {"ok": bool, "data": Any, "error": Optional[str], "status_code": Optional[int]}.
        "data" is the unwrapped `data` field of Cal.com's {"status": ..., "data": ...}
        response envelope (or the raw body if it doesn't follow that shape).
        """
        url = f"{CALCOM_API_BASE}{path}" if path.startswith("/") else f"{CALCOM_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        extra_headers = {"cal-api-version": api_version} if api_version else {}
        try:
            if json_body is not None:
                r = await self._httpx_client.request(method, url, json=json_body, params=params, headers=extra_headers)
            else:
                r = await self._httpx_client.request(method, url, params=params, headers=extra_headers)
        except httpx.RequestError as e:
            return {"ok": False, "error": f"Request to Cal.com API failed: {e}", "status_code": None}

        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._calcom_request(
                method, path, api_version=api_version, json_body=json_body, params=params, retry_401=False
            )
        if r.status_code >= 400:
            logger.warning("Cal.com API %s %s -> %s %s", method, path, r.status_code, (r.text or "")[:500])
            return {"ok": False, "error": (r.text or "")[:500], "status_code": r.status_code}

        if r.status_code == 204 or not r.content:
            return {"ok": True, "data": None, "status_code": r.status_code}
        try:
            body = r.json()
        except ValueError:
            return {"ok": True, "data": None, "status_code": r.status_code}
        data = body.get("data") if isinstance(body, dict) and "data" in body else body
        return {"ok": True, "data": data, "status_code": r.status_code}

    # ------------------------------------------------------------------
    # @tool public methods — Bookings
    # ------------------------------------------------------------------

    @tool(
        description=(
            "List/search bookings for the connected Cal.com account. Filter by status, attendee, "
            "event type, team, or a date range. Cursor-based pagination via the returned cursor. "
            "Next: use get_booking for full detail on one result, or cancel_booking/reschedule_booking/"
            "confirm_booking/decline_booking/mark_booking_absence to act on a result."
        ),
        params={
            "status": "Filter to a single status: one of upcoming, recurring, past, cancelled, unconfirmed. Omit to list across all statuses.",
            "attendee_email": "Filter bookings by the attendee's email address.",
            "attendee_name": "Filter bookings by the attendee's name.",
            "event_type_id": "Filter bookings by a single Cal.com event type id.",
            "team_id": "Filter bookings by a team id the user is part of.",
            "after_start": "Only bookings whose start is after this ISO 8601 datetime, e.g. '2025-03-07T10:00:00.000Z'.",
            "before_end": "Only bookings whose end is before this ISO 8601 datetime.",
            "sort_start": "Sort by start time: 'asc' or 'desc'.",
            "cursor": "Opaque pagination cursor from a previous response's pagination.nextCursor. Omit for the first page.",
            "limit": "Max number of bookings to return, 1-100 (default 50).",
        },
    )
    async def list_bookings(
        self,
        status: Optional[str] = None,
        attendee_email: Optional[str] = None,
        attendee_name: Optional[str] = None,
        event_type_id: Optional[int] = None,
        team_id: Optional[int] = None,
        after_start: Optional[str] = None,
        before_end: Optional[str] = None,
        sort_start: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 50), 100))}
        if status and status in BOOKING_STATUSES:
            params["status"] = status
        if attendee_email:
            params["attendeeEmail"] = attendee_email
        if attendee_name:
            params["attendeeName"] = attendee_name
        if event_type_id:
            params["eventTypeId"] = event_type_id
        if team_id:
            params["teamId"] = team_id
        if after_start:
            params["afterStart"] = after_start
        if before_end:
            params["beforeEnd"] = before_end
        if sort_start in SORT_DIRECTIONS:
            params["sortStart"] = sort_start
        if cursor:
            params["cursor"] = cursor

        result = await self._calcom_request("GET", "/bookings", api_version=BOOKINGS_LIST_VERSION, params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list bookings: {result['error']}"}
        data = result["data"] or {}
        bookings = data.get("bookings") if isinstance(data, dict) else data
        bookings = bookings or []
        response = f"Found {len(bookings)} booking(s)." if bookings else "No bookings found for the given criteria."
        return {
            "success": True,
            "response": response,
            "bookings": bookings,
            "pagination": data.get("pagination") if isinstance(data, dict) else None,
        }

    @tool(
        description="Fetch full detail for one booking by its uid. Next: cancel_booking/reschedule_booking/confirm_booking/decline_booking/mark_booking_absence/add_booking_guests.",
        params={"booking_uid": "The booking's uid, obtained from list_bookings or create_booking."},
    )
    async def get_booking(self, booking_uid: str) -> Dict:
        result = await self._calcom_request("GET", f"/bookings/{booking_uid}", api_version=BOOKINGS_ACTIONS_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Fetched booking {booking_uid}.", "booking": result["data"]}

    @tool(
        description=(
            "Create a new booking for an event type. Identify the event type either by event_type_id, "
            "or by event_type_slug + username (individual) / event_type_slug + team_slug (team event). "
            "The start time must be in UTC ISO 8601 format. Next: get_booking to re-fetch it, "
            "reschedule_booking/cancel_booking to change it."
        ),
        params={
            "start": "Start time of the booking in ISO 8601 UTC, e.g. '2026-08-13T09:00:00Z'.",
            "attendee_name": "The attendee's full name.",
            "attendee_email": "The attendee's email address.",
            "attendee_timezone": "The attendee's IANA timezone, e.g. 'America/New_York'.",
            "attendee_phone_number": "The attendee's phone number in international format, e.g. '+19876543210'. Required if the event type has SMS reminders enabled.",
            "attendee_language": "Attendee's language/locale code, e.g. 'en', 'es', 'fr'.",
            "event_type_id": "The id of the event type being booked. Provide this, or event_type_slug + username/team_slug.",
            "event_type_slug": "The event type's slug. Requires username (individual) or team_slug (team) too.",
            "username": "Username of the event type owner, used with event_type_slug for an individual event type.",
            "team_slug": "Slug of the team that owns the event type, used with event_type_slug for a team event type.",
            "organization_slug": "Organization slug, only needed alongside event_type_slug + username/team_slug when that user/team is within an organization.",
            "guests": "Optional list of guest email addresses to invite in addition to the attendee.",
            "location": "Optional location object for the booking, e.g. {\"type\": \"link\", \"link\": \"https://...\"} or {\"type\": \"address\", \"address\": \"...\"} or {\"type\": \"integration\", \"integration\": \"cal-video\"}.",
            "metadata": "Optional free-form key/value metadata to store on the booking (string values only, max 50 keys).",
            "booking_fields_responses": "Optional object of custom booking-field slug -> response value, for event types with custom booking fields.",
        },
    )
    async def create_booking(
        self,
        start: str,
        attendee_name: str,
        attendee_email: str,
        attendee_timezone: str,
        attendee_phone_number: Optional[str] = None,
        attendee_language: Optional[str] = None,
        event_type_id: Optional[int] = None,
        event_type_slug: Optional[str] = None,
        username: Optional[str] = None,
        team_slug: Optional[str] = None,
        organization_slug: Optional[str] = None,
        guests: Optional[List[str]] = None,
        location: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        booking_fields_responses: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        attendee: Dict[str, Any] = {"name": attendee_name, "email": attendee_email, "timeZone": attendee_timezone}
        if attendee_phone_number:
            attendee["phoneNumber"] = attendee_phone_number
        if attendee_language:
            attendee["language"] = attendee_language

        body: Dict[str, Any] = {"start": start, "attendee": attendee}
        if event_type_id:
            body["eventTypeId"] = event_type_id
        if event_type_slug:
            body["eventTypeSlug"] = event_type_slug
        if username:
            body["username"] = username
        if team_slug:
            body["teamSlug"] = team_slug
        if organization_slug:
            body["organizationSlug"] = organization_slug
        if guests:
            body["guests"] = guests
        if location:
            body["location"] = location
        if metadata:
            body["metadata"] = metadata
        if booking_fields_responses:
            body["bookingFieldsResponses"] = booking_fields_responses

        result = await self._calcom_request("POST", "/bookings", api_version=BOOKINGS_ACTIONS_VERSION, json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to create booking: {result['error']}"}
        return {"success": True, "response": f"Booking created for {attendee_name} at {start}.", "booking": result["data"]}

    @tool(
        description="Cancel an existing booking.",
        params={
            "booking_uid": "The booking's uid to cancel.",
            "cancellation_reason": "Optional reason for the cancellation.",
            "cancel_subsequent_bookings": "For a recurring booking only: if true, also cancel this and every recurrence after it.",
        },
    )
    async def cancel_booking(
        self, booking_uid: str, cancellation_reason: Optional[str] = None, cancel_subsequent_bookings: bool = False
    ) -> Dict:
        body: Dict[str, Any] = {}
        if cancellation_reason:
            body["cancellationReason"] = cancellation_reason
        if cancel_subsequent_bookings:
            body["cancelSubsequentBookings"] = True
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/cancel", api_version=BOOKINGS_ACTIONS_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to cancel booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Booking {booking_uid} cancelled.", "booking": result["data"]}

    @tool(
        description="Reschedule an existing booking to a new start time.",
        params={
            "booking_uid": "The booking's uid to reschedule.",
            "start": "New start time in ISO 8601 UTC, e.g. '2026-08-13T10:00:00Z'.",
            "rescheduling_reason": "Optional reason for the reschedule.",
            "rescheduled_by": "Optional email of the person rescheduling -- only needed for a booking that requires confirmation; if the event type owner's email is given the rescheduled booking is auto-confirmed.",
        },
    )
    async def reschedule_booking(
        self, booking_uid: str, start: str, rescheduling_reason: Optional[str] = None, rescheduled_by: Optional[str] = None
    ) -> Dict:
        body: Dict[str, Any] = {"start": start}
        if rescheduling_reason:
            body["reschedulingReason"] = rescheduling_reason
        if rescheduled_by:
            body["rescheduledBy"] = rescheduled_by
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/reschedule", api_version=BOOKINGS_ACTIONS_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to reschedule booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Booking {booking_uid} rescheduled to {start}.", "booking": result["data"]}

    @tool(
        description="Confirm a booking that requires manual confirmation (event types with 'requires confirmation' enabled).",
        params={"booking_uid": "The booking's uid to confirm."},
    )
    async def confirm_booking(self, booking_uid: str) -> Dict:
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/confirm", api_version=BOOKINGS_ACTIONS_VERSION, json_body={}
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to confirm booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Booking {booking_uid} confirmed.", "booking": result["data"]}

    @tool(
        description="Decline a booking that requires manual confirmation.",
        params={
            "booking_uid": "The booking's uid to decline.",
            "reason": "Optional reason for declining, e.g. 'Host has to take another call'.",
        },
    )
    async def decline_booking(self, booking_uid: str, reason: Optional[str] = None) -> Dict:
        body: Dict[str, Any] = {"reason": reason} if reason else {}
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/decline", api_version=BOOKINGS_ACTIONS_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to decline booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Booking {booking_uid} declined.", "booking": result["data"]}

    @tool(
        description="Mark a booking's host and/or attendee(s) as absent (no-show).",
        params={
            "booking_uid": "The booking's uid.",
            "host_absent": "Whether the host was absent.",
            "attendee_email": "Email of a specific attendee to mark absent/present. Requires attendee_absent too.",
            "attendee_absent": "Whether the attendee identified by attendee_email was absent. Only used together with attendee_email.",
        },
    )
    async def mark_booking_absence(
        self,
        booking_uid: str,
        host_absent: Optional[bool] = None,
        attendee_email: Optional[str] = None,
        attendee_absent: Optional[bool] = None,
    ) -> Dict:
        body: Dict[str, Any] = {}
        if host_absent is not None:
            body["host"] = host_absent
        if attendee_email is not None:
            body["attendees"] = [{"email": attendee_email, "absent": bool(attendee_absent)}]
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/mark-absent", api_version=BOOKINGS_ACTIONS_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to mark absence for booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Absence recorded for booking {booking_uid}.", "booking": result["data"]}

    @tool(
        description="Add one or more guests (by email) to an existing booking.",
        params={
            "booking_uid": "The booking's uid.",
            "guests": "List of guests to add, each an object like {\"email\": \"a@b.com\", \"name\": \"A B\", \"timeZone\": \"America/New_York\"}. name and timeZone are optional. Maximum 100 guests per request.",
        },
    )
    async def add_booking_guests(self, booking_uid: str, guests: List[Dict[str, Any]]) -> Dict:
        result = await self._calcom_request(
            "POST", f"/bookings/{booking_uid}/guests", api_version=BOOKINGS_GUESTS_VERSION, json_body={"guests": guests}
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to add guests to booking {booking_uid}: {result['error']}"}
        return {"success": True, "response": f"Added {len(guests)} guest(s) to booking {booking_uid}.", "booking": result["data"]}

    # ------------------------------------------------------------------
    # @tool public methods — Event Types
    # ------------------------------------------------------------------

    @tool(
        description="List event types. Omit all filters to list the authenticated user's own event types.",
        params={
            "username": "Only event types owned by this username.",
            "event_slug": "Slug of a specific event type to return. Requires username too.",
            "usernames": "Comma-separated usernames to get a dynamic event type for multiple users, e.g. 'alice,bob'.",
            "org_slug": "Slug of the organization the user belongs to, if relevant.",
            "org_id": "Id of the organization, alternative to org_slug.",
        },
    )
    async def list_event_types(
        self,
        username: Optional[str] = None,
        event_slug: Optional[str] = None,
        usernames: Optional[str] = None,
        org_slug: Optional[str] = None,
        org_id: Optional[int] = None,
    ) -> Dict:
        params: Dict[str, Any] = {}
        if username:
            params["username"] = username
        if event_slug:
            params["eventSlug"] = event_slug
        if usernames:
            params["usernames"] = usernames
        if org_slug:
            params["orgSlug"] = org_slug
        if org_id:
            params["orgId"] = org_id
        result = await self._calcom_request("GET", "/event-types", api_version=EVENT_TYPES_VERSION, params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list event types: {result['error']}"}
        event_types = result["data"] or []
        response = f"Found {len(event_types)} event type(s)." if event_types else "No event types found."
        return {"success": True, "response": response, "event_types": event_types}

    @tool(
        description="Fetch full detail for one event type by its id.",
        params={"event_type_id": "The event type's id, obtained from list_event_types."},
    )
    async def get_event_type(self, event_type_id: int) -> Dict:
        result = await self._calcom_request("GET", f"/event-types/{event_type_id}", api_version=EVENT_TYPES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch event type {event_type_id}: {result['error']}"}
        return {"success": True, "response": f"Fetched event type {event_type_id}.", "event_type": result["data"]}

    @tool(
        description=(
            "Create a new event type for the authenticated user. Only the most commonly used fields "
            "are named parameters here; pass anything else Cal.com's Create Event Type API supports "
            "(e.g. bookingLimitsCount, recurrence, seats, color, confirmationPolicy) via extra_fields."
        ),
        params={
            "title": "Event type title, e.g. 'Intro Call'.",
            "slug": "URL slug for the event type, e.g. 'intro-call'.",
            "length_in_minutes": "Duration of the event in minutes.",
            "description": "Optional description shown on the booking page.",
            "locations": "Optional list of location objects, e.g. [{\"type\": \"link\", \"link\": \"https://...\"}] or [{\"type\": \"integration\", \"integration\": \"cal-video\"}] or [{\"type\": \"address\", \"address\": \"...\"}]. Defaults to Cal Video if omitted.",
            "schedule_id": "Optional schedule id (from list_schedules) this event type should check availability against, instead of the user's default schedule.",
            "disable_guests": "If true, the booker can't add guest emails when booking.",
            "minimum_booking_notice": "Minimum number of minutes before the event that a booking can be made.",
            "before_event_buffer": "Minutes blocked on the calendar before each booking of this event.",
            "after_event_buffer": "Minutes blocked on the calendar after each booking of this event.",
            "length_in_minutes_options": "Optional list of alternate durations (minutes) the booker can pick from; must include length_in_minutes.",
            "metadata": "Optional free-form key/value metadata to store on the event type.",
            "extra_fields": "Optional object merged as-is into the request body for any other Cal.com event-type field not listed above.",
        },
    )
    async def create_event_type(
        self,
        title: str,
        slug: str,
        length_in_minutes: int,
        description: Optional[str] = None,
        locations: Optional[List[Dict[str, Any]]] = None,
        schedule_id: Optional[int] = None,
        disable_guests: Optional[bool] = None,
        minimum_booking_notice: Optional[int] = None,
        before_event_buffer: Optional[int] = None,
        after_event_buffer: Optional[int] = None,
        length_in_minutes_options: Optional[List[int]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"title": title, "slug": slug, "lengthInMinutes": length_in_minutes}
        if description:
            body["description"] = description
        if locations:
            body["locations"] = locations
        if schedule_id:
            body["scheduleId"] = schedule_id
        if disable_guests is not None:
            body["disableGuests"] = disable_guests
        if minimum_booking_notice is not None:
            body["minimumBookingNotice"] = minimum_booking_notice
        if before_event_buffer is not None:
            body["beforeEventBuffer"] = before_event_buffer
        if after_event_buffer is not None:
            body["afterEventBuffer"] = after_event_buffer
        if length_in_minutes_options:
            body["lengthInMinutesOptions"] = length_in_minutes_options
        if metadata:
            body["metadata"] = metadata
        if extra_fields:
            body.update(extra_fields)

        result = await self._calcom_request("POST", "/event-types", api_version=EVENT_TYPES_VERSION, json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to create event type: {result['error']}"}
        return {"success": True, "response": f"Event type '{title}' created.", "event_type": result["data"]}

    @tool(
        description="Update fields on an existing event type. Only pass the fields you want to change.",
        params={
            "event_type_id": "The event type's id to update.",
            "title": "New title.",
            "slug": "New URL slug.",
            "length_in_minutes": "New duration in minutes.",
            "description": "New description.",
            "locations": "New list of location objects, replacing the existing ones -- see create_event_type for the shape.",
            "schedule_id": "New schedule id to check availability against.",
            "disable_guests": "Whether the booker can add guest emails.",
            "minimum_booking_notice": "Minimum minutes of notice required before a booking.",
            "before_event_buffer": "Minutes blocked before each booking.",
            "after_event_buffer": "Minutes blocked after each booking.",
            "hidden": "If true, hides this event type from the public booking page.",
            "metadata": "New free-form key/value metadata, replacing the existing metadata.",
            "extra_fields": "Optional object merged as-is into the request body for any other Cal.com event-type field.",
        },
    )
    async def update_event_type(
        self,
        event_type_id: int,
        title: Optional[str] = None,
        slug: Optional[str] = None,
        length_in_minutes: Optional[int] = None,
        description: Optional[str] = None,
        locations: Optional[List[Dict[str, Any]]] = None,
        schedule_id: Optional[int] = None,
        disable_guests: Optional[bool] = None,
        minimum_booking_notice: Optional[int] = None,
        before_event_buffer: Optional[int] = None,
        after_event_buffer: Optional[int] = None,
        hidden: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        body: Dict[str, Any] = {}
        if title:
            body["title"] = title
        if slug:
            body["slug"] = slug
        if length_in_minutes is not None:
            body["lengthInMinutes"] = length_in_minutes
        if description is not None:
            body["description"] = description
        if locations is not None:
            body["locations"] = locations
        if schedule_id is not None:
            body["scheduleId"] = schedule_id
        if disable_guests is not None:
            body["disableGuests"] = disable_guests
        if minimum_booking_notice is not None:
            body["minimumBookingNotice"] = minimum_booking_notice
        if before_event_buffer is not None:
            body["beforeEventBuffer"] = before_event_buffer
        if after_event_buffer is not None:
            body["afterEventBuffer"] = after_event_buffer
        if hidden is not None:
            body["hidden"] = hidden
        if metadata is not None:
            body["metadata"] = metadata
        if extra_fields:
            body.update(extra_fields)

        result = await self._calcom_request(
            "PATCH", f"/event-types/{event_type_id}", api_version=EVENT_TYPES_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to update event type {event_type_id}: {result['error']}"}
        return {"success": True, "response": f"Event type {event_type_id} updated.", "event_type": result["data"]}

    @tool(
        description="Permanently delete an event type.",
        params={"event_type_id": "The event type's id to delete."},
    )
    async def delete_event_type(self, event_type_id: int) -> Dict:
        result = await self._calcom_request("DELETE", f"/event-types/{event_type_id}", api_version=EVENT_TYPES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to delete event type {event_type_id}: {result['error']}"}
        return {"success": True, "response": f"Event type {event_type_id} deleted.", "event_type": result["data"]}

    # ------------------------------------------------------------------
    # @tool public methods — Availability
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Get available booking time slots for an event type within a date range. Identify the "
            "event type either by event_type_id, or by event_type_slug + username (individual) / "
            "event_type_slug + team_slug (team). Next: create_booking with one of the returned start times."
        ),
        params={
            "start": "Start of the range to check, ISO 8601, e.g. '2026-09-05' (defaults to 00:00:00) or '2026-09-05T09:00:00Z'.",
            "end": "End of the range to check, ISO 8601, e.g. '2026-09-06' (defaults to 23:59:59).",
            "event_type_id": "The event type's id. Provide this, or event_type_slug + username/team_slug.",
            "event_type_slug": "The event type's slug. Requires username or team_slug too.",
            "username": "Username of the event type owner, used with event_type_slug for an individual event type.",
            "team_slug": "Slug of the team that owns the event type, used with event_type_slug for a team event type.",
            "organization_slug": "Organization slug, only needed alongside event_type_slug + username/team_slug within an organization.",
            "timezone": "IANA timezone the returned slots should be expressed in. Defaults to UTC.",
            "duration": "Only for event types with multiple allowed durations: the desired slot length in minutes.",
            "booking_uid_to_reschedule": "When checking slots to reschedule an existing booking, its uid -- ensures the original slot appears as available.",
        },
    )
    async def get_available_slots(
        self,
        start: str,
        end: str,
        event_type_id: Optional[int] = None,
        event_type_slug: Optional[str] = None,
        username: Optional[str] = None,
        team_slug: Optional[str] = None,
        organization_slug: Optional[str] = None,
        timezone: Optional[str] = None,
        duration: Optional[int] = None,
        booking_uid_to_reschedule: Optional[str] = None,
    ) -> Dict:
        params: Dict[str, Any] = {"start": start, "end": end, "format": "range"}
        if event_type_id:
            params["eventTypeId"] = event_type_id
        if event_type_slug:
            params["eventTypeSlug"] = event_type_slug
        if username:
            params["username"] = username
        if team_slug:
            params["teamSlug"] = team_slug
        if organization_slug:
            params["organizationSlug"] = organization_slug
        if timezone:
            params["timeZone"] = timezone
        if duration:
            params["duration"] = duration
        if booking_uid_to_reschedule:
            params["bookingUidToReschedule"] = booking_uid_to_reschedule

        result = await self._calcom_request("GET", "/slots", api_version=SLOTS_VERSION, params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch available slots: {result['error']}"}
        slots = result["data"] or {}
        total = sum(len(v) for v in slots.values()) if isinstance(slots, dict) else 0
        response = f"Found {total} available slot(s) across {len(slots)} day(s)." if total else "No available slots in that range."
        return {"success": True, "response": response, "slots": slots}

    # ------------------------------------------------------------------
    # @tool public methods — Schedules
    # ------------------------------------------------------------------

    @tool(description="List all availability schedules for the authenticated user.", params={})
    async def list_schedules(self) -> Dict:
        result = await self._calcom_request("GET", "/schedules", api_version=SCHEDULES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list schedules: {result['error']}"}
        schedules = result["data"] or []
        response = f"Found {len(schedules)} schedule(s)." if schedules else "No schedules found."
        return {"success": True, "response": response, "schedules": schedules}

    @tool(
        description="Fetch full detail for one schedule by its id.",
        params={"schedule_id": "The schedule's id, obtained from list_schedules."},
    )
    async def get_schedule(self, schedule_id: int) -> Dict:
        result = await self._calcom_request("GET", f"/schedules/{schedule_id}", api_version=SCHEDULES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch schedule {schedule_id}: {result['error']}"}
        return {"success": True, "response": f"Fetched schedule {schedule_id}.", "schedule": result["data"]}

    @tool(description="Get the authenticated user's default schedule.", params={})
    async def get_default_schedule(self) -> Dict:
        result = await self._calcom_request("GET", "/schedules/default", api_version=SCHEDULES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch default schedule: {result['error']}"}
        if not result["data"]:
            return {"success": True, "response": "No default schedule is set.", "schedule": None}
        return {"success": True, "response": "Fetched default schedule.", "schedule": result["data"]}

    @tool(
        description=(
            "Create a new availability schedule. Event types can be pointed at a non-default schedule "
            "via update_event_type's schedule_id, so availability is checked against that specific "
            "schedule instead of the default one."
        ),
        params={
            "name": "Name for the schedule, e.g. 'Working Hours'.",
            "timezone": "IANA timezone used to calculate available times, e.g. 'Europe/Rome'.",
            "is_default": "Whether this becomes the user's default schedule.",
            "availability": "List of {\"days\": [\"Monday\", ...], \"startTime\": \"09:00\", \"endTime\": \"17:00\"} blocks. Days are full names (Monday..Sunday); times are 24-hour HH:MM. Defaults to Monday-Friday 09:00-17:00 if omitted.",
            "overrides": "Optional list of date-specific overrides: [{\"date\": \"2026-05-20\", \"startTime\": \"18:00\", \"endTime\": \"21:00\"}].",
        },
    )
    async def create_schedule(
        self,
        name: str,
        timezone: str,
        is_default: bool,
        availability: Optional[List[Dict[str, Any]]] = None,
        overrides: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"name": name, "timeZone": timezone, "isDefault": is_default}
        if availability:
            body["availability"] = availability
        if overrides:
            body["overrides"] = overrides
        result = await self._calcom_request("POST", "/schedules", api_version=SCHEDULES_VERSION, json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to create schedule: {result['error']}"}
        return {"success": True, "response": f"Schedule '{name}' created.", "schedule": result["data"]}

    @tool(
        description="Update an existing schedule's name, timezone, availability, overrides, or default status.",
        params={
            "schedule_id": "The schedule's id to update.",
            "name": "New name for the schedule.",
            "timezone": "New IANA timezone.",
            "is_default": "Whether this becomes the user's default schedule.",
            "availability": "New list of availability blocks, replacing the existing ones -- see create_schedule for the shape.",
            "overrides": "New list of date-specific overrides, replacing the existing ones.",
        },
    )
    async def update_schedule(
        self,
        schedule_id: int,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
        is_default: Optional[bool] = None,
        availability: Optional[List[Dict[str, Any]]] = None,
        overrides: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if timezone:
            body["timeZone"] = timezone
        if is_default is not None:
            body["isDefault"] = is_default
        if availability is not None:
            body["availability"] = availability
        if overrides is not None:
            body["overrides"] = overrides
        result = await self._calcom_request(
            "PATCH", f"/schedules/{schedule_id}", api_version=SCHEDULES_VERSION, json_body=body
        )
        if not result["ok"]:
            return {"success": False, "response": f"Failed to update schedule {schedule_id}: {result['error']}"}
        return {"success": True, "response": f"Schedule {schedule_id} updated.", "schedule": result["data"]}

    @tool(
        description="Permanently delete a schedule.",
        params={"schedule_id": "The schedule's id to delete."},
    )
    async def delete_schedule(self, schedule_id: int) -> Dict:
        result = await self._calcom_request("DELETE", f"/schedules/{schedule_id}", api_version=SCHEDULES_VERSION)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to delete schedule {schedule_id}: {result['error']}"}
        return {"success": True, "response": f"Schedule {schedule_id} deleted."}

    # ------------------------------------------------------------------
    # @tool public methods — Calendars
    # ------------------------------------------------------------------

    @tool(description="List calendars connected to the authenticated user's account, plus the destination calendar new events are written to.", params={})
    async def list_calendars(self) -> Dict:
        result = await self._calcom_request("GET", "/calendars")
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list calendars: {result['error']}"}
        data = result["data"] or {}
        connected = data.get("connectedCalendars") or [] if isinstance(data, dict) else []
        response = f"Found {len(connected)} connected calendar(s)." if connected else "No calendars connected."
        return {"success": True, "response": response, "calendars": data}

    @tool(
        description="Get busy time blocks across one or more connected calendars within a date range.",
        params={
            "date_from": "Start date for the query, e.g. '2026-08-01'.",
            "date_to": "End date for the query, e.g. '2026-08-31'.",
            "calendars_to_load": "List of calendars to check, each {\"credentialId\": <int>, \"externalId\": \"<calendar email or id>\"}. Obtain credentialId/externalId from list_calendars.",
            "timezone": "IANA timezone the busy-time query and results should use. Defaults to UTC.",
        },
    )
    async def get_busy_times(
        self, date_from: str, date_to: str, calendars_to_load: List[Dict[str, Any]], timezone: Optional[str] = None
    ) -> Dict:
        params: Dict[str, Any] = {"dateFrom": date_from, "dateTo": date_to}
        if timezone:
            params["timeZone"] = timezone
        for i, cal in enumerate(calendars_to_load or []):
            params[f"calendarsToLoad[{i}][credentialId]"] = cal.get("credentialId")
            params[f"calendarsToLoad[{i}][externalId]"] = cal.get("externalId")

        result = await self._calcom_request("GET", "/calendars/busy-times", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch busy times: {result['error']}"}
        busy_times = result["data"] or []
        response = f"Found {len(busy_times)} busy period(s)." if busy_times else "No busy periods found in that range."
        return {"success": True, "response": response, "busy_times": busy_times}

    # ------------------------------------------------------------------
    # @tool public methods — Profile
    # ------------------------------------------------------------------

    @tool(description="Get the authenticated user's Cal.com profile (username, email, timezone, locale, default schedule, etc).", params={})
    async def get_my_profile(self) -> Dict:
        result = await self._calcom_request("GET", "/me")
        if not result["ok"]:
            return {"success": False, "response": f"Failed to fetch profile: {result['error']}"}
        return {"success": True, "response": "Fetched profile.", "profile": result["data"]}

    @tool(
        description="Update the authenticated user's Cal.com profile. Only pass the fields you want to change. Changing email requires verification and won't take effect immediately.",
        params={
            "name": "New display name.",
            "email": "New email address (requires verification before it becomes primary).",
            "time_format": "12 or 24 -- the user's preferred time format.",
            "week_start": "Day the week starts on: one of Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday.",
            "timezone": "New IANA timezone, e.g. 'Europe/Rome'.",
            "locale": "New locale/language code, e.g. 'en', 'es', 'fr'.",
            "default_schedule_id": "Id of the schedule to use as the new default.",
            "bio": "New profile bio text.",
            "avatar_url": "URL of a new avatar image.",
        },
    )
    async def update_my_profile(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        time_format: Optional[int] = None,
        week_start: Optional[str] = None,
        timezone: Optional[str] = None,
        locale: Optional[str] = None,
        default_schedule_id: Optional[int] = None,
        bio: Optional[str] = None,
        avatar_url: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        if email:
            body["email"] = email
        if time_format in (12, 24):
            body["timeFormat"] = time_format
        if week_start in SCHEDULE_DAYS:
            body["weekStart"] = week_start
        if timezone:
            body["timeZone"] = timezone
        if locale:
            body["locale"] = locale
        if default_schedule_id:
            body["defaultScheduleId"] = default_schedule_id
        if bio is not None:
            body["bio"] = bio
        if avatar_url:
            body["avatarUrl"] = avatar_url

        result = await self._calcom_request("PATCH", "/me", json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to update profile: {result['error']}"}
        return {"success": True, "response": "Profile updated.", "profile": result["data"]}

    # ------------------------------------------------------------------
    # @tool public methods — Out of Office
    # ------------------------------------------------------------------

    @tool(
        description="List the authenticated user's out-of-office entries.",
        params={
            "limit": "Max number of entries to return, 1-250 (default 250).",
            "skip": "Number of entries to skip, for pagination.",
        },
    )
    async def list_out_of_office(self, limit: int = 250, skip: int = 0) -> Dict:
        params = {"take": max(1, min(int(limit or 250), 250)), "skip": max(0, int(skip or 0))}
        result = await self._calcom_request("GET", "/me/ooo", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list out-of-office entries: {result['error']}"}
        entries = result["data"] or []
        response = f"Found {len(entries)} out-of-office entr{'y' if len(entries) == 1 else 'ies'}." if entries else "No out-of-office entries found."
        return {"success": True, "response": response, "out_of_office": entries}

    @tool(
        description="Create an out-of-office period for the authenticated user, during which they won't be bookable (and bookings can optionally be redirected to a covering colleague).",
        params={
            "start": "Start of the out-of-office period, ISO 8601 UTC, e.g. '2026-08-01T00:00:00.000Z'.",
            "end": "End of the out-of-office period, ISO 8601 UTC.",
            "reason": "Optional reason: one of unspecified, vacation, travel, sick, public_holiday.",
            "notes": "Optional free-text notes, e.g. 'Vacation in Hawaii'.",
            "to_user_id": "Optional user id of a colleague to cover bookings during this period.",
        },
    )
    async def create_out_of_office(
        self, start: str, end: str, reason: Optional[str] = None, notes: Optional[str] = None, to_user_id: Optional[int] = None
    ) -> Dict:
        body: Dict[str, Any] = {"start": start, "end": end}
        if reason in OOO_REASONS:
            body["reason"] = reason
        if notes:
            body["notes"] = notes
        if to_user_id:
            body["toUserId"] = to_user_id
        result = await self._calcom_request("POST", "/me/ooo", json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to create out-of-office entry: {result['error']}"}
        return {"success": True, "response": f"Out-of-office entry created from {start} to {end}.", "out_of_office": result["data"]}

    @tool(
        description="Delete an out-of-office entry.",
        params={"ooo_id": "The out-of-office entry's id, obtained from list_out_of_office."},
    )
    async def delete_out_of_office(self, ooo_id: int) -> Dict:
        result = await self._calcom_request("DELETE", f"/me/ooo/{ooo_id}")
        if not result["ok"]:
            return {"success": False, "response": f"Failed to delete out-of-office entry {ooo_id}: {result['error']}"}
        return {"success": True, "response": f"Out-of-office entry {ooo_id} deleted."}

    # ------------------------------------------------------------------
    # @tool public methods — Webhooks
    # ------------------------------------------------------------------

    @tool(
        description="List webhooks configured for the authenticated user's account.",
        params={
            "limit": "Max number of webhooks to return, 1-250 (default 250).",
            "skip": "Number of webhooks to skip, for pagination.",
        },
    )
    async def list_webhooks(self, limit: int = 250, skip: int = 0) -> Dict:
        params = {"take": max(1, min(int(limit or 250), 250)), "skip": max(0, int(skip or 0))}
        result = await self._calcom_request("GET", "/webhooks", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list webhooks: {result['error']}"}
        webhooks = result["data"] or []
        response = f"Found {len(webhooks)} webhook(s)." if webhooks else "No webhooks configured."
        return {"success": True, "response": response, "webhooks": webhooks}

    @tool(
        description=(
            "Create a webhook that Cal.com will POST to when the given event(s) happen. For "
            "workflow-triggered automation prefer the dedicated Cal.com trigger nodes instead of "
            "creating a webhook manually here -- use this for one-off integrations the user asks for by name."
        ),
        params={
            "subscriber_url": "The URL Cal.com should POST event payloads to.",
            "triggers": f"List of event names to subscribe to, from: {', '.join(WEBHOOK_TRIGGERS)}.",
            "active": "Whether the webhook is active immediately (default true).",
            "secret": "Optional shared secret Cal.com signs the payload with (verify via the x-cal-signature-256 header).",
            "payload_template": "Optional custom JSON payload template overriding the default body shape.",
        },
    )
    async def create_webhook(
        self,
        subscriber_url: str,
        triggers: List[str],
        active: bool = True,
        secret: Optional[str] = None,
        payload_template: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"subscriberUrl": subscriber_url, "triggers": triggers, "active": active}
        if secret:
            body["secret"] = secret
        if payload_template:
            body["payloadTemplate"] = payload_template
        result = await self._calcom_request("POST", "/webhooks", json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to create webhook: {result['error']}"}
        return {"success": True, "response": f"Webhook created for {subscriber_url}.", "webhook": result["data"]}

    @tool(
        description="Update an existing webhook's URL, triggers, active status, secret, or payload template.",
        params={
            "webhook_id": "The webhook's id to update.",
            "subscriber_url": "New URL Cal.com should POST to.",
            "triggers": f"New list of event names to subscribe to, replacing the existing ones. From: {', '.join(WEBHOOK_TRIGGERS)}.",
            "active": "Whether the webhook is active.",
            "secret": "New shared secret.",
            "payload_template": "New custom JSON payload template.",
        },
    )
    async def update_webhook(
        self,
        webhook_id: str,
        subscriber_url: Optional[str] = None,
        triggers: Optional[List[str]] = None,
        active: Optional[bool] = None,
        secret: Optional[str] = None,
        payload_template: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {}
        if subscriber_url:
            body["subscriberUrl"] = subscriber_url
        if triggers:
            body["triggers"] = triggers
        if active is not None:
            body["active"] = active
        if secret:
            body["secret"] = secret
        if payload_template:
            body["payloadTemplate"] = payload_template
        result = await self._calcom_request("PATCH", f"/webhooks/{webhook_id}", json_body=body)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to update webhook {webhook_id}: {result['error']}"}
        return {"success": True, "response": f"Webhook {webhook_id} updated.", "webhook": result["data"]}

    @tool(
        description="Delete a webhook.",
        params={"webhook_id": "The webhook's id to delete."},
    )
    async def delete_webhook(self, webhook_id: str) -> Dict:
        result = await self._calcom_request("DELETE", f"/webhooks/{webhook_id}")
        if not result["ok"]:
            return {"success": False, "response": f"Failed to delete webhook {webhook_id}: {result['error']}"}
        return {"success": True, "response": f"Webhook {webhook_id} deleted."}

    # ------------------------------------------------------------------
    # @tool public methods — Team
    # ------------------------------------------------------------------

    @tool(description="List teams the authenticated user belongs to.", params={})
    async def list_teams(self) -> Dict:
        result = await self._calcom_request("GET", "/teams")
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list teams: {result['error']}"}
        teams = result["data"] or []
        response = f"Found {len(teams)} team(s)." if teams else "No teams found."
        return {"success": True, "response": response, "teams": teams}

    @tool(
        description="List bookings for a team the authenticated user belongs to.",
        params={
            "team_id": "The team's id.",
            "status": "Filter to a single status: one of upcoming, recurring, past, cancelled, unconfirmed.",
            "attendee_email": "Filter by the attendee's email address.",
            "after_start": "Only bookings whose start is after this ISO 8601 datetime.",
            "before_end": "Only bookings whose end is before this ISO 8601 datetime.",
            "limit": "Max number of bookings to return, 1-100 (default 50).",
        },
    )
    async def get_team_bookings(
        self,
        team_id: int,
        status: Optional[str] = None,
        attendee_email: Optional[str] = None,
        after_start: Optional[str] = None,
        before_end: Optional[str] = None,
        limit: int = 50,
    ) -> Dict:
        params: Dict[str, Any] = {"take": max(1, min(int(limit or 50), 100))}
        if status and status in BOOKING_STATUSES:
            params["status"] = status
        if attendee_email:
            params["attendeeEmail"] = attendee_email
        if after_start:
            params["afterStart"] = after_start
        if before_end:
            params["beforeEnd"] = before_end
        result = await self._calcom_request("GET", f"/teams/{team_id}/bookings", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list bookings for team {team_id}: {result['error']}"}
        bookings = result["data"] or []
        response = f"Found {len(bookings)} booking(s) for team {team_id}." if bookings else f"No bookings found for team {team_id}."
        return {"success": True, "response": response, "bookings": bookings}

    @tool(
        description="List event types belonging to a team.",
        params={
            "team_id": "The team's id.",
            "event_slug": "Slug of a specific team event type to return.",
        },
    )
    async def get_team_event_types(self, team_id: int, event_slug: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {}
        if event_slug:
            params["eventSlug"] = event_slug
        result = await self._calcom_request("GET", f"/teams/{team_id}/event-types", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list event types for team {team_id}: {result['error']}"}
        event_types = result["data"] or []
        response = f"Found {len(event_types)} event type(s) for team {team_id}." if event_types else f"No event types found for team {team_id}."
        return {"success": True, "response": response, "event_types": event_types}

    @tool(
        description="List members of a team.",
        params={
            "team_id": "The team's id.",
            "emails": "Optional comma-separated list of email addresses to filter to (max 20).",
            "limit": "Max number of memberships to return, 1-250 (default 250).",
        },
    )
    async def list_team_memberships(self, team_id: int, emails: Optional[str] = None, limit: int = 250) -> Dict:
        params: Dict[str, Any] = {"take": max(1, min(int(limit or 250), 250))}
        if emails:
            params["emails"] = emails
        result = await self._calcom_request("GET", f"/teams/{team_id}/memberships", params=params)
        if not result["ok"]:
            return {"success": False, "response": f"Failed to list memberships for team {team_id}: {result['error']}"}
        memberships = result["data"] or []
        response = f"Found {len(memberships)} member(s) in team {team_id}." if memberships else f"No members found for team {team_id}."
        return {"success": True, "response": response, "memberships": memberships}
