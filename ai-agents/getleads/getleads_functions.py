"""
GetLeads.io functions class for functions_wrapper plugin.

Implements two surfaces of the GetLeads.io REST API
(https://www.getleads.io/docs/):

Person enrichment — 3 ways (#person-enrichment--3-ways):
  1. From work email        -> POST /api/v1/enrich/from-email
  2. From LinkedIn profile   -> POST /api/v1/enrich/from-linkedin
  3. From name + company     -> POST /api/v1/enrich/from-person

Database search (#database-search) — the ~402M+ row contact index:
  - POST /api/v1/contacts/search               (filtered search, paginated)
  - POST /api/v1/contacts/search/count          (free match count)
  - POST /api/v1/contacts/search/export         (async CSV export, S3)
  - GET  /api/v1/contacts/search/export/{id}    (poll export job status)
  - GET  /api/v1/contacts/filter-values         (enum values for filters)

Each @tool method is one API action exposed to the LLM (mirrors the
structure of gmail_functions.py / whatsapp_business_functions.py). Credentials
arrive via ConnectedServiceToolAgent; the connected GetLeads.io API key
(provider.json's "api_key" required field) is sent as a Bearer token, one of
the two forms GetLeads.io accepts (Authorization: Bearer ... or X-API-Key).

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider
-- all natural-language-to-tool-call reasoning is done by the outer ReAct
brain (MCPAgentTool), the same way as every other functions_wrapper tool.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

GETLEADS_API_BASE = "https://app.getleads.io"
BATCH_MAX_ITEMS = 100
DEFAULT_LIMIT_PER_ITEM = 1
MAX_LIMIT_PER_ITEM = 10

DEFAULT_SEARCH_LIMIT = 1000
MAX_SEARCH_LIMIT = 50000
MAX_EXPORT_ROWS = 50000
FILTER_VALUES_FIELDS = (
    "seniority", "job_functions", "company_size", "revenue", "regions", "continents",
    "countries", "headquarters_countries", "industries", "personas", "entity_types", "email_status",
)

DATABASE_SEARCH_FILTERS_REFERENCE = """Filter object for GetLeads.io contact database search (all fields optional, AND-combined):
Company targeting: domains (string[], website domains), company_name (string, substring), email_domain (string, comma-separated OK), domain_list_id (string, saved list id).
Role: job_titles (string[], whole-word match -- include both acronym and spelled-out form e.g. ["CTO","Chief Technology Officer"]), seniority (string[]: C-Team, VP, Director, Manager, Staff, Other), job_functions (string[], department), personas (string[], buyer persona).
Company firmographics: industries (string[], LinkedIn industries), company_size_min/company_size_max (number, employees), revenue (string[]: "<$1M","$1M to <$10M","$10M to <$50M","$50M to <$100M","$100M to <$1B","$1B+"), headquarters_countries (string[]), company_description (string, substring), entity_types (string[]), technologies (string[]), has_mobile_app/has_web_app (boolean).
Company numeric ranges (band/range overlap): employees_min, employees_max, revenue_min, revenue_max (USD), followers_min, followers_max, founded_year_min, founded_year_max, total_funding_min, total_funding_max (USD), monthly_traffic_min, monthly_traffic_max.
Person location: countries, regions (NORAM/EMEA/APAC/LATAM), continents, cities (substring), states (substring) -- all string[].
Job location (office, not home): job_location_country, job_location_state, job_location_city -- all string[].
Person identity: first_name, last_name (substring), email_address (exact), linkedin_url (substring), person_description (substring bio), skills (substring) -- all string.
Email quality: email_status (string[]: VALID, CATCH_ALL, INVALID -- use ["VALID"] for deliverable emails), require_email (boolean).
Presence: require_phone (boolean, default false).
Excludes: exclude_domains, exclude_countries, exclude_headquarters_countries, exclude_industries, exclude_job_titles -- all string[].
Use get_filter_values to see allowed enum values for list fields before filtering on them."""


class GetLeadsFunctions(ConnectedServiceToolAgent):
    """
    GetLeads.io person-enrichment tool functions. Each @tool method is one
    enrichment path (work email, LinkedIn URL, or name+company).
    """

    TOOL_DESCRIPTION = (
        "GetLeads.io: enrich people by work email, LinkedIn profile URL, or name + company/domain "
        "to find their LinkedIn profile, work email, name, title, company, and other B2B contact fields."
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
        raise NotImplementedError("GetLeadsFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool")

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("GetLeadsFunctions: no access_token (api_key) in credentials")
        self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())
        logger.debug("GetLeadsFunctions initialized")

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

    async def _gl_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry_401: bool = True,
    ) -> Dict:
        url = f"{GETLEADS_API_BASE}{path}" if path.startswith("/") else f"{GETLEADS_API_BASE}/{path}"
        if not self._httpx_client:
            self._httpx_client = httpx.AsyncClient(timeout=30.0, headers=self._headers())

        try:
            r = await self._httpx_client.request(method, url, json=json_body, params=params)
        except httpx.HTTPError as e:
            logger.warning("GetLeads API %s %s -> transport error: %s", method, path, e)
            return {"_error": True, "status_code": 0, "message": str(e)}

        if r.status_code == 401 and retry_401 and await self._refresh_access_token():
            self._httpx_client.headers["Authorization"] = f"Bearer {self.access_token}"
            return await self._gl_request(method, path, json_body=json_body, params=params, retry_401=False)

        try:
            data = r.json()
        except Exception:
            data = None

        if r.status_code >= 400:
            message = (data or {}).get("message") if isinstance(data, dict) else None
            message = message or (r.text or "")[:500]
            credits_remaining = (data or {}).get("creditsRemaining") if isinstance(data, dict) else None
            logger.warning("GetLeads API %s %s -> %s %s", method, path, r.status_code, message)
            return {
                "_error": True,
                "status_code": r.status_code,
                "message": message,
                "creditsRemaining": credits_remaining,
            }

        return data if isinstance(data, dict) else {}

    @staticmethod
    def _err(data: Dict, action: str) -> Optional[Dict]:
        if not isinstance(data, dict) or not data.get("_error"):
            return None
        detail = f"{action} failed: {data.get('message')} (HTTP {data.get('status_code')})"
        out: Dict[str, Any] = {"success": False, "response": detail}
        if data.get("creditsRemaining") is not None:
            out["credits_remaining"] = data.get("creditsRemaining")
        return out

    @staticmethod
    def _summarize(results: List[Dict], *, label_key: str) -> str:
        total = len(results)
        succeeded = sum(1 for r in results if r.get("success"))
        if total == 0:
            return "No items were enriched."
        if succeeded == 0:
            return f"None of the {total} item(s) could be enriched."
        sample = "; ".join(str(r.get(label_key) or "") for r in results[:5] if r.get("success"))
        suffix = f" ({sample})" if sample else ""
        return f"Enriched {succeeded} of {total} item(s){suffix}."

    # ------------------------------------------------------------------
    # @tool public methods — one per GetLeads.io person-enrichment path
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Enrich one or more people by their work email address. Returns each person's LinkedIn "
            "profile URL, name, title, company, and other provider fields when a match is found. "
            "Use this when you already know a person's work email. Pass every email in a single call "
            "(up to 100) rather than calling this once per email -- it is a batch endpoint. "
            "Costs 1 credit per email where a match is found."
        ),
        params={"emails": "List of work email addresses to enrich (1-100 items)."},
    )
    async def enrich_from_email(self, emails: List[str]) -> Dict:
        emails = [e.strip() for e in (emails or []) if isinstance(e, str) and e.strip()]
        if not emails:
            return {"success": False, "response": "Provide at least one work email."}
        if len(emails) > BATCH_MAX_ITEMS:
            return {"success": False, "response": f"Provide at most {BATCH_MAX_ITEMS} emails per call."}

        data = await self._gl_request(
            "POST", "/api/v1/enrich/from-email", json_body={"items": [{"email": e} for e in emails]}
        )
        err = self._err(data, "Email enrichment")
        if err:
            return err

        results = data.get("results") or []
        return {
            "success": True,
            "response": self._summarize(results, label_key="email"),
            "results": results,
            "credits_remaining": data.get("creditsRemaining"),
        }

    @tool(
        description=(
            "Enrich one or more people by their public LinkedIn profile URL. Returns each person's "
            "work email and other provider fields when a match is found. Use this when you already "
            "have someone's LinkedIn profile URL. Pass every URL in a single call (up to 100) rather "
            "than calling this once per URL -- it is a batch endpoint. "
            "Costs 1 credit per URL where a match is found."
        ),
        params={
            "linkedin_urls": "List of public LinkedIn profile URLs to enrich (1-100 items).",
            "limit_per_item": "Number of matches to return per URL, 1-10 (default 1).",
        },
    )
    async def enrich_from_linkedin(self, linkedin_urls: List[str], limit_per_item: int = DEFAULT_LIMIT_PER_ITEM) -> Dict:
        linkedin_urls = [u.strip() for u in (linkedin_urls or []) if isinstance(u, str) and u.strip()]
        if not linkedin_urls:
            return {"success": False, "response": "Provide at least one LinkedIn profile URL."}
        if len(linkedin_urls) > BATCH_MAX_ITEMS:
            return {"success": False, "response": f"Provide at most {BATCH_MAX_ITEMS} LinkedIn URLs per call."}
        limit_per_item = min(max(1, int(limit_per_item or DEFAULT_LIMIT_PER_ITEM)), MAX_LIMIT_PER_ITEM)

        data = await self._gl_request(
            "POST",
            "/api/v1/enrich/from-linkedin",
            json_body={
                "items": [{"linkedin_url": u} for u in linkedin_urls],
                "limit_per_item": limit_per_item,
            },
        )
        err = self._err(data, "LinkedIn enrichment")
        if err:
            return err

        results = data.get("results") or []
        return {
            "success": True,
            "response": self._summarize(results, label_key="linkedinUrl"),
            "results": results,
            "credits_remaining": data.get("creditsRemaining"),
        }

    @tool(
        description=(
            "Enrich one or more people by first name, last name, and either company name or email "
            "domain. Returns each person's work email, LinkedIn profile URL, and other provider fields "
            "when a match is found. Use this when you know someone's name and where they work but not "
            "their email or LinkedIn URL. Pass every person in a single call (up to 100) rather than "
            "calling this once per person -- it is a batch endpoint. "
            "Costs 1 credit per person where a match is found."
        ),
        params={
            "people": (
                "List of people to enrich. Each item is an object with 'first_name' (string, required), "
                "'last_name' (string, required), and at least one of 'company_name' (string) or "
                "'email_domain' (string), e.g. {\"first_name\": \"Jane\", \"last_name\": \"Doe\", "
                "\"company_name\": \"Acme Inc\"}."
            ),
        },
    )
    async def enrich_from_person(self, people: List[Dict[str, Any]]) -> Dict:
        if not people:
            return {"success": False, "response": "Provide at least one person (first_name, last_name, and company_name or email_domain)."}
        if len(people) > BATCH_MAX_ITEMS:
            return {"success": False, "response": f"Provide at most {BATCH_MAX_ITEMS} people per call."}

        items: List[Dict[str, str]] = []
        for i, p in enumerate(people):
            if not isinstance(p, dict):
                return {"success": False, "response": f"Item {i} must be an object with first_name, last_name, and company_name or email_domain."}
            first_name = str(p.get("first_name") or "").strip()
            last_name = str(p.get("last_name") or "").strip()
            company_name = str(p.get("company_name") or "").strip()
            email_domain = str(p.get("email_domain") or "").strip()
            if not first_name or not last_name or not (company_name or email_domain):
                return {
                    "success": False,
                    "response": f"Item {i} needs first_name, last_name, and company_name or email_domain.",
                }
            item: Dict[str, str] = {"first_name": first_name, "last_name": last_name}
            if company_name:
                item["company_name"] = company_name
            if email_domain:
                item["email_domain"] = email_domain
            items.append(item)

        data = await self._gl_request("POST", "/api/v1/enrich/from-person", json_body={"items": items})
        err = self._err(data, "Person enrichment")
        if err:
            return err

        results = data.get("results") or []
        return {
            "success": True,
            "response": self._summarize(results, label_key="email"),
            "results": results,
            "credits_remaining": data.get("creditsRemaining"),
        }

    # ------------------------------------------------------------------
    # @tool public methods — Database search (~402M+ row contact index)
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(filters, dict):
            return {}
        return {k: v for k, v in filters.items() if v is not None and v != "" and v != []}

    @tool(
        description=(
            "Search the GetLeads.io contact database (~402M+ rows) with structured filters. Returns "
            "matching contacts (people), paginated. Use count_contacts first on a broad query to see "
            "how many rows match before spending credits on search, and use export_contacts instead of "
            "this for large pulls (over a few hundred rows). Costs 1 credit per record returned, 0 if "
            "none. All filter fields are combined with AND logic."
        ),
        params={
            "filters": DATABASE_SEARCH_FILTERS_REFERENCE,
            "limit": "Max rows to return this page, 1-50000 (default 1000). Keep small for exploration; use export_contacts for bulk pulls.",
            "offset": "Rows to skip for pagination (default 0). Use next_offset from a prior response.",
            "max_per_company": "Optional cap on contacts per company, 1-50. Use for diverse results across many domains.",
            "columns": "Optional list of output columns (display label or internal name). Omit for a sensible default set.",
            "where_sql": "Optional advanced raw SQL WHERE predicate over internal column names, AND-combined with filters (e.g. \"MONTHLY_GOOGLE_ADSPEND_ORG > 0\"). Invalid syntax returns an error.",
        },
    )
    async def search_contacts(
        self,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
        max_per_company: Optional[int] = None,
        columns: Optional[List[str]] = None,
        where_sql: Optional[str] = None,
    ) -> Dict:
        limit = min(max(1, int(limit or DEFAULT_SEARCH_LIMIT)), MAX_SEARCH_LIMIT)
        offset = max(0, int(offset or 0))

        body: Dict[str, Any] = {"filters": self._clean_filters(filters), "limit": limit, "offset": offset}
        if max_per_company is not None:
            body["max_per_company"] = min(max(1, int(max_per_company)), 50)
        if columns:
            body["columns"] = columns
        if where_sql:
            body["where_sql"] = where_sql

        data = await self._gl_request("POST", "/api/v1/contacts/search", json_body=body)
        err = self._err(data, "Contact search")
        if err:
            return err

        contacts = data.get("contacts") or []
        has_more = bool(data.get("has_more"))
        response = f"Found {len(contacts)} contact(s) this page" + (", more available" if has_more else "") + "."
        result: Dict[str, Any] = {
            "success": True,
            "response": response,
            "contacts": contacts,
            "has_more": has_more,
            "total_available": data.get("total_available"),
            "credits_remaining": data.get("creditsRemaining"),
        }
        if has_more and data.get("next_offset") is not None:
            result["next_offset"] = data.get("next_offset")
        return result

    @tool(
        description=(
            "Get the exact count of contacts matching a filter set, without returning any records or "
            "spending credits (free, 0 credits). Use this before search_contacts or export_contacts to "
            "size a query. An empty filters object returns the size of the entire contact database."
        ),
        params={
            "filters": DATABASE_SEARCH_FILTERS_REFERENCE,
            "where_sql": "Optional advanced raw SQL WHERE predicate, AND-combined with filters.",
        },
    )
    async def count_contacts(self, filters: Optional[Dict[str, Any]] = None, where_sql: Optional[str] = None) -> Dict:
        body: Dict[str, Any] = {"filters": self._clean_filters(filters)}
        if where_sql:
            body["where_sql"] = where_sql

        data = await self._gl_request("POST", "/api/v1/contacts/search/count", json_body=body)
        err = self._err(data, "Contact count")
        if err:
            return err

        return {
            "success": True,
            "response": data.get("message") or f"{data.get('total_matching', 0)} matching contact(s).",
            "total_matching": data.get("total_matching"),
            "exportable_rows": data.get("exportable_rows"),
            "export_capped": data.get("export_capped"),
            "max_export_rows": data.get("max_export_rows"),
        }

    @tool(
        description=(
            "Start an asynchronous export of contact search matches to a downloadable CSV (on S3). "
            "Same filters as search_contacts. Returns an export_id immediately (job queued) -- poll "
            "get_export_status with that id until job_status is 'completed', then use its export_url "
            "(valid 24 hours). Costs 1 credit per row exported, so call count_contacts first and pass "
            "confirmed=true only once the row count and cost are acceptable; omit max_rows to export "
            "all matches up to plan/credit/50000-row limits."
        ),
        params={
            "filters": DATABASE_SEARCH_FILTERS_REFERENCE,
            "confirmed": "Must be explicitly set to true to start the export (safety confirmation before spending credits). Defaults to false.",
            "max_per_company": "Optional cap on contacts per company, 1-50.",
            "max_rows": "Optional cap on rows this export writes, 1-50000. Omit to export all matches up to plan/credit/50000-row limits.",
            "where_sql": "Optional advanced raw SQL WHERE predicate, AND-combined with filters.",
        },
    )
    async def export_contacts(
        self,
        filters: Optional[Dict[str, Any]] = None,
        confirmed: bool = False,
        max_per_company: Optional[int] = None,
        max_rows: Optional[int] = None,
        where_sql: Optional[str] = None,
    ) -> Dict:
        if not confirmed:
            return {
                "success": False,
                "response": (
                    "Export not started. This spends 1 credit per row exported -- call count_contacts first to "
                    "see how many rows would be exported, then call export_contacts again with confirmed=true."
                ),
            }

        body: Dict[str, Any] = {"filters": self._clean_filters(filters), "confirmed": True}
        if max_per_company is not None:
            body["max_per_company"] = min(max(1, int(max_per_company)), 50)
        if max_rows is not None:
            body["max_rows"] = min(max(1, int(max_rows)), MAX_EXPORT_ROWS)
        if where_sql:
            body["where_sql"] = where_sql

        data = await self._gl_request("POST", "/api/v1/contacts/search/export", json_body=body)
        err = self._err(data, "Contact export")
        if err:
            return err

        return {
            "success": True,
            "response": data.get("message") or f"Export {data.get('export_id')} started ({data.get('job_status')}).",
            "export_id": data.get("export_id"),
            "job_status": data.get("job_status"),
            "rows_available": data.get("rows_available"),
            "export_row_cap": data.get("export_row_cap"),
            "rows_capped_by_credits": data.get("rows_capped_by_credits"),
        }

    @tool(
        description=(
            "Poll the status of a contact export started by export_contacts. While job_status is "
            "'queued' or 'running', wait a few seconds and call again. When job_status is 'completed', "
            "the response includes export_url -- a presigned CSV download link valid for 24 hours."
        ),
        params={"export_id": "The export_id returned by export_contacts."},
    )
    async def get_export_status(self, export_id: str) -> Dict:
        export_id = (export_id or "").strip()
        if not export_id:
            return {"success": False, "response": "export_id is required."}

        data = await self._gl_request("GET", f"/api/v1/contacts/search/export/{export_id}")
        err = self._err(data, "Export status check")
        if err:
            return err

        job_status = data.get("job_status")
        response = f"Export {export_id} is {job_status}."
        if job_status == "completed" and data.get("export_url"):
            response += f" Download: {data.get('export_url')} (rows_exported={data.get('rows_exported')})."
        return {
            "success": True,
            "response": response,
            "export_id": export_id,
            "job_status": job_status,
            "export_url": data.get("export_url"),
            "expires_in_seconds": data.get("expires_in_seconds"),
            "rows_exported": data.get("rows_exported"),
            "rows_available": data.get("rows_available"),
            "credits_remaining": data.get("creditsRemaining"),
        }

    @tool(
        description=(
            "Get the allowed enum values for one of the GetLeads.io contact database's list filter "
            "fields (e.g. what strings are valid for seniority or industries), so search_contacts / "
            "count_contacts / export_contacts filters use exact matching values instead of guesses."
        ),
        params={
            "field": (
                "Which filter field to get allowed values for. One of: "
                + ", ".join(FILTER_VALUES_FIELDS) + "."
            ),
        },
    )
    async def get_filter_values(self, field: str) -> Dict:
        field = (field or "").strip()
        if not field:
            return {"success": False, "response": f"field is required. One of: {', '.join(FILTER_VALUES_FIELDS)}."}

        data = await self._gl_request("GET", "/api/v1/contacts/filter-values", params={"field": field})
        err = self._err(data, "Filter values lookup")
        if err:
            return err

        values = data.get("values") or data.get("data") or []
        return {
            "success": True,
            "response": f"{len(values)} allowed value(s) for '{field}'.",
            "field": field,
            "values": values,
        }
