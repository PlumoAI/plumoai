"""
Apify functions class for the functions_wrapper plugin.

Each public @tool method is one Apify capability exposed to the LLM: a static,
hardcoded catalog (name/description/schema fixed in code, matching Apify's own
MCP tool list) instead of live tools/list discovery.

Execution is mixed:
  - get_dataset_items calls Apify's plain REST endpoint directly
    (GET /v2/datasets/:datasetId/items -- https://docs.apify.com/api/v2/dataset-items-get),
    which returns a raw JSON array with none of the SSE/chunked-stream framing
    the MCP transport needs. If that REST call fails or times out, it falls
    back to the MCP tool of the same name so a transient issue with this one
    endpoint doesn't fail the whole call.
  - Every other tool forwards to the live mcp.apify.com MCP server via a
    lazily-created, internal MCPAgentTool instance, reusing the same hardened
    JSON-RPC/SSE transport (including its truncated-stream retry) that
    llm_tools/mcp_agent_tool.py already uses successfully for Apify in
    production.

An earlier version of this file called Apify's plain REST API for every
tool. In practice, get_dataset_items (100-item GET) timed out after 60s
against api.apify.com in production while get_actor_run's REST call on the
very same host succeeded fine and every mcp.apify.com call kept working --
so it isn't a blanket network block on api.apify.com, just something
endpoint/response-size-specific with this one call that wasn't diagnosable
from here. Hence the REST-with-MCP-fallback approach for this tool
specifically, MCP-only for the rest.

Credentials arrive via ConnectedServiceToolAgent / app_config["service_credential"]:
  - credentials: {"access_token": <Apify Personal API Token>}

Note on NLU: functions_wrapper tools run in a stdio subprocess (see
llm_tools/functions_runner.py) and do not receive the in-process llm_provider --
all natural-language-to-tool-call reasoning is done by the outer ReAct brain
(MCPAgentTool), the same way it is done for every other functions_wrapper tool.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent
from llm_tools import tool

logger = logging.getLogger(__name__)

# Bare host -- matches the URL the existing generic MCP connection already
# connects to successfully in production (see llm_tools/mcp_agent_tool.py logs:
# "Connecting via JSON-RPC HTTP: https://mcp.apify.com").
APIFY_MCP_URL = "https://mcp.apify.com"
APIFY_API_BASE = "https://api.apify.com/v2"
# Bounded under the 300s stdio "tools/call" budget (see
# llm_tools/mcp_agent_tool.py's stdio timeout) so a repeat of the unexplained
# production hang still fails in time to fall back to the MCP tool, instead
# of eating the whole budget and returning nothing.
DATASET_ITEMS_REST_TIMEOUT = httpx.Timeout(connect=10.0, read=260.0, write=10.0, pool=10.0)

DEFAULT_RUN_WAIT_SECS = 30
MAX_RUN_WAIT_SECS = 45
DEFAULT_DATASET_ITEMS_LIMIT = 20


def _clamp(value: Optional[int], lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _strip_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class ApifyFunctions(ConnectedServiceToolAgent):
    """
    Apify tool functions. Each @tool method is one Apify capability: finding
    Actors in the Store, reading their details/input schema, running them,
    and reading back run status / dataset / key-value-store results.

    Access token (the Apify Personal API Token) is read from
    credentials["access_token"] and forwarded as the Bearer token for the
    internal MCP connection -- no OAuth refresh flow exists for Apify tokens.
    """

    TOOL_DESCRIPTION = (
        "Apify: search the Apify Store for Actors (scrapers, crawlers, AI agents, MCP-server "
        "Actors), fetch an Actor's details and input schema, run any Actor, check/abort a run, "
        "and read results from a run's dataset or key-value store. Also exposes the dedicated "
        "apify/rag-web-browser (Google search + page scrape) and apify/web-fetch (fetch one URL) "
        "Actors directly, plus Apify/Crawlee documentation search and problem reporting."
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
        # Lazily-created nested MCPAgentTool -- most tool methods execute through this.
        self._mcp_delegate: Optional[Any] = None
        # REST client used only by get_dataset_items (see module docstring).
        self._httpx_client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError(
            "ApifyFunctions is tool-only; use @tool methods via FunctionsWrapperAgentTool"
        )

    async def initialize(self) -> None:
        if not self.access_token:
            logger.warning("ApifyFunctions: no access_token (API token) in credentials")
        self._httpx_client = httpx.AsyncClient(
            timeout=DATASET_ITEMS_REST_TIMEOUT,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        logger.debug("ApifyFunctions initialized")

    async def cleanup(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
            self._httpx_client = None
        if self._mcp_delegate is not None:
            try:
                await self._mcp_delegate.cleanup()
            except Exception:
                pass
            self._mcp_delegate = None

    # ------------------------------------------------------------------
    # MCP execution helper -- every @tool method below forwards through this
    # ------------------------------------------------------------------

    async def _mcp_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._mcp_delegate is None:
            from llm_tools.mcp_agent_tool import MCPAgentTool

            self._mcp_delegate = MCPAgentTool(
                mcp_config={
                    "server_type": "http",
                    "url": APIFY_MCP_URL,
                    "headers": {"Authorization": f"Bearer {self.access_token}"},
                },
                llm_provider=None,
                token=self.token,
                company_id=self.company_id,
                user_id=self.user_id,
                agent_id=self.agent_id,
                connected_service_id=self.connected_service_id,
            )
            await self._mcp_delegate.initialize()
        return await self._mcp_delegate._call_tool(tool_name, _strip_none(args))

    @staticmethod
    def _flatten(result: Dict[str, Any]) -> Dict[str, Any]:
        """Turn an MCPAgentTool _call_tool() result into a flat dict.

        Apify's MCP tools return their payload as a single text content block,
        which is either a JSON object (flattened into top-level keys here), a
        JSON array (returned as items/count), or plain prose/Markdown
        (returned as-is under "response"). Handling all three generically
        here means every @tool method below stays a one-line call into this
        helper regardless of which shape that particular Apify tool happens
        to return.
        """
        if not result.get("success"):
            return {"success": False, "response": str(result.get("error") or "MCP call failed")}
        text_parts: List[str] = [
            str(block.get("text") or "")
            for block in (result.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        raw_text = "\n".join(t for t in text_parts if t)
        if not raw_text:
            return {"success": True, "response": "(empty response)"}
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            return {"success": True, "response": raw_text}
        if isinstance(parsed, dict):
            out = dict(parsed)
            out.setdefault("success", True)
            out.setdefault("response", raw_text[:500])
            return out
        if isinstance(parsed, list):
            return {"success": True, "response": f"Retrieved {len(parsed)} item(s).", "items": parsed, "count": len(parsed)}
        return {"success": True, "response": raw_text}

    async def _tool_call(self, mcp_tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._mcp_call(mcp_tool_name, args)
        return self._flatten(result)

    # ------------------------------------------------------------------
    # @tool public methods -- one per Apify capability
    # ------------------------------------------------------------------

    @tool(
        description=(
            "Search the Apify Store to find pre-built Actors (scrapers, crawlers, AI agents, MCP-server "
            "Actors) for a platform or use case. Returns Actor cards: title, full name (username/actor), "
            "URL, description, pricing, usage stats, rating, developer, and categories. Does NOT run any "
            "Actor or fetch data -- use call_actor for that. Prefer broad platform-name keywords (e.g. "
            "'Instagram' rather than 'Instagram scraper'); pass an empty string to browse the most "
            "popular Actors store-wide. For full input schema/README of a specific Actor, follow up with "
            "fetch_actor_details."
        ),
        params={
            "keywords": "Space-separated keywords (1-3 terms, e.g. 'Instagram posts'). Empty string returns the most popular Actors.",
            "limit": "Max Actors to return, 1-10 (default 5).",
            "offset": "Number of results to skip for pagination (default 0).",
        },
    )
    async def search_actors(self, keywords: str = "", limit: int = 5, offset: int = 0) -> Dict:
        return await self._tool_call(
            "search-actors",
            {"keywords": keywords, "limit": _clamp(limit, 1, 10, 5), "offset": _clamp(offset, 0, 10_000, 0)},
        )

    @tool(
        description=(
            "Get detailed information about one Actor by its exact ID or full name "
            "(format 'username/name', e.g. 'apify/rag-web-browser'). Requires an exact name seen from "
            "search_actors -- do not guess a plausible-looking name. Use 'output' to select which "
            "sections to fetch."
        ),
        params={
            "actor": "Actor ID or full name 'username/name', e.g. 'apify/rag-web-browser'.",
            "output": (
                "Object of boolean flags selecting which sections to include. Keys: description, "
                "stats, pricing, rating, metadata, inputSchema, readme, outputSchema, mcpTools. "
                "Omit for the default profile (everything except mcpTools)."
            ),
        },
    )
    async def fetch_actor_details(self, actor: str, output: Optional[Dict[str, bool]] = None) -> Dict:
        if not actor:
            return {"success": False, "response": "actor is required."}
        args: Dict[str, Any] = {"actor": actor}
        if output:
            args["output"] = output
        return await self._tool_call("fetch-actor-details", args)

    @tool(
        description=(
            "Run any Actor from the Apify Store. Requires the exact Actor name (format "
            "'username/name') and an input object matching its input schema -- use "
            "fetch_actor_details with output={inputSchema: true} first if the schema is unknown. "
            "For an MCP-server Actor, use 'actorName:toolName' to call one of its tools directly "
            "(get available tool names from fetch_actor_details with output={mcpTools: true}). "
            "Waits up to waitSecs for completion (default 30s, cap 45s); if the run has not "
            "finished by then, poll it with get_actor_run using the returned runId. Once the run "
            "has a datasetId, use get_dataset_items to read its results."
        ),
        params={
            "actor": "Actor name 'username/name', or 'actorName:toolName' for an MCP-server Actor's tool.",
            "input": "The Actor's input as a JSON object, matching its input schema.",
            "waitSecs": "Seconds to wait for completion, 0-45 (default 30). 0 starts the run and returns immediately.",
            "callOptions": (
                "Optional run config object. Keys: memory (MB, power of 2 128-32768), timeout (seconds, "
                "0=infinite), build (tag/number, default = Actor's default build), maxItems (pay-per-result "
                "cap), maxTotalChargeUsd (pay-per-event cap)."
            ),
        },
    )
    async def call_actor(
        self,
        actor: str,
        input: Dict[str, Any],
        waitSecs: int = DEFAULT_RUN_WAIT_SECS,
        callOptions: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        if not actor:
            return {"success": False, "response": "actor is required."}
        args: Dict[str, Any] = {
            "actor": actor,
            "input": input or {},
            "waitSecs": _clamp(waitSecs, 0, MAX_RUN_WAIT_SECS, DEFAULT_RUN_WAIT_SECS),
        }
        if callOptions:
            args["callOptions"] = callOptions
        return await self._tool_call("call-actor", args)

    @tool(
        description=(
            "Get detailed information about a specific Actor run: status, storages (dataset/key-value "
            "store IDs), stats, and a suggested next step. Pass waitSecs > 0 to block until the run "
            "reaches a terminal status (SUCCEEDED, FAILED, ABORTED, TIMED-OUT) or the cap elapses."
        ),
        params={
            "runId": "The ID of the Actor run.",
            "waitSecs": "Max seconds to wait for a terminal status, 0-45 (default 30). 0 returns immediately.",
        },
    )
    async def get_actor_run(self, runId: str, waitSecs: int = DEFAULT_RUN_WAIT_SECS) -> Dict:
        if not runId:
            return {"success": False, "response": "runId is required."}
        return await self._tool_call(
            "get-actor-run", {"runId": runId, "waitSecs": _clamp(waitSecs, 0, MAX_RUN_WAIT_SECS, DEFAULT_RUN_WAIT_SECS)},
        )

    @tool(
        description=(
            "Get items (rows) from a dataset -- the output produced by an Actor run. Returns the rows "
            "themselves, not metadata or a schema. Default limit is 20. Use clean=true to skip empty "
            "items and hidden (# prefixed) fields."
        ),
        params={
            "datasetId": "Dataset ID or 'username~dataset-name'.",
            "clean": "If true, returns only non-empty items and skips hidden fields.",
            "offset": "Number of items to skip at the start (default 0).",
            "limit": "Max items to return (default 20).",
            "fields": "Comma-separated fields to include, in order. Dot notation for nested objects, e.g. 'metadata.url'.",
            "omit": "Comma-separated fields to exclude.",
            "desc": "If true, returns results newest-to-oldest.",
        },
    )
    async def get_dataset_items(
        self,
        datasetId: str,
        clean: Optional[bool] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        fields: Optional[str] = None,
        omit: Optional[str] = None,
        desc: Optional[bool] = None,
    ) -> Dict:
        if not datasetId:
            return {"success": False, "response": "datasetId is required."}
        mcp_args = {
            "datasetId": datasetId, "clean": clean, "offset": offset or 0,
            "limit": limit or DEFAULT_DATASET_ITEMS_LIMIT, "fields": fields, "omit": omit, "desc": desc,
        }
        rest_result = await self._get_dataset_items_rest(**mcp_args)
        if rest_result is not None:
            return rest_result
        logger.warning("get_dataset_items REST call failed/timed out -- falling back to MCP tool")
        return await self._tool_call("get-dataset-items", mcp_args)

    async def _get_dataset_items_rest(
        self,
        datasetId: str,
        clean: Optional[bool] = None,
        offset: int = 0,
        limit: int = DEFAULT_DATASET_ITEMS_LIMIT,
        fields: Optional[str] = None,
        omit: Optional[str] = None,
        desc: Optional[bool] = None,
    ) -> Optional[Dict]:
        """GET /v2/datasets/:datasetId/items -- https://docs.apify.com/api/v2/dataset-items-get.
        Default format=json returns a raw JSON array. Returns None (not an
        error dict) on any failure/timeout so the caller falls back to MCP."""
        if not self._httpx_client:
            return None
        params = _strip_none({
            "clean": clean, "offset": offset, "limit": limit,
            "fields": fields, "omit": omit, "desc": desc,
        })
        url = f"{APIFY_API_BASE}/datasets/{datasetId}/items"
        try:
            r = await self._httpx_client.get(url, params=params)
        except httpx.HTTPError as e:
            logger.warning("get_dataset_items REST request error: %s", e)
            return None
        if r.status_code in (401, 403):
            logger.warning(
                "get_dataset_items REST call got HTTP %s -- falling back to MCP tool for refresh/retry",
                r.status_code,
            )
            return None
        if r.status_code >= 400:
            try:
                message = r.json().get("error", {}).get("message", r.text)
            except Exception:
                message = r.text
            return {
                "success": False,
                "response": f"Failed to get items from dataset '{datasetId}': {message} (HTTP {r.status_code})",
            }
        try:
            items = r.json()
        except Exception as e:
            logger.warning("get_dataset_items REST response was not valid JSON: %s", e)
            return None
        if not isinstance(items, list):
            logger.warning("get_dataset_items REST response was not a JSON array: %r", type(items))
            return None
        return {
            "success": True,
            "response": f"Retrieved {len(items)} item(s) from dataset {datasetId}.",
            "datasetId": datasetId, "items": items, "count": len(items),
        }

    @tool(
        description=(
            "Get the value stored under a specific key in a key-value store -- a single record, "
            "not a listing of all keys. Requires the exact key name."
        ),
        params={
            "keyValueStoreId": "Key-value store ID or 'username~store-name'.",
            "recordKey": "Key of the record to retrieve.",
        },
    )
    async def get_key_value_store_record(self, keyValueStoreId: str, recordKey: str) -> Dict:
        if not keyValueStoreId or not recordKey:
            return {"success": False, "response": "keyValueStoreId and recordKey are required."}
        return await self._tool_call(
            "get-key-value-store-record", {"keyValueStoreId": keyValueStoreId, "recordKey": recordKey},
        )

    @tool(
        description=(
            "Abort an Actor run that is currently starting or running. Has no effect on a run that "
            "already reached a terminal status (SUCCEEDED, FAILED, ABORTING, ABORTED, TIMED-OUT)."
        ),
        params={
            "runId": "The ID of the Actor run to abort.",
            "gracefully": "If true, aborts gracefully with a 30-second timeout for the Actor to clean up.",
        },
    )
    async def abort_actor_run(self, runId: str, gracefully: Optional[bool] = None) -> Dict:
        if not runId:
            return {"success": False, "response": "runId is required."}
        return await self._tool_call("abort-actor-run", {"runId": runId, "gracefully": gracefully})

    @tool(
        description=(
            "Run the apify/rag-web-browser Actor: queries Google Search, scrapes the top N result "
            "pages with a full browser, and returns their content as Markdown for an LLM/RAG pipeline. "
            "Pass a URL instead of search terms in 'query' to fetch that one page directly. Use for "
            "immediate one-time data retrieval ('today', 'latest', 'current') rather than search_actors."
        ),
        params={
            "query": "Google Search keywords, or a specific URL to fetch directly.",
            "maxResults": "Max top organic results to scrape when query is search terms (default 3). Ignored for a direct URL.",
            "waitSecs": "Max seconds to wait for the run to finish, 0-45 (default 30).",
        },
    )
    async def apify_rag_web_browser(
        self, query: str, maxResults: int = 3, waitSecs: int = DEFAULT_RUN_WAIT_SECS,
    ) -> Dict:
        if not query:
            return {"success": False, "response": "query is required."}
        return await self._tool_call(
            "apify--rag-web-browser",
            {
                "query": query, "maxResults": _clamp(maxResults, 1, 10, 3),
                "outputFormats": ["markdown"],
                "waitSecs": _clamp(waitSecs, 0, MAX_RUN_WAIT_SECS, DEFAULT_RUN_WAIT_SECS),
            },
        )

    @tool(
        description=(
            "Run the apify/web-fetch Actor: downloads one http(s) URL and returns clean Markdown (or "
            "other formats), rendering JavaScript and working around bot blocking. Use when a plain "
            "fetch of a specific URL was blocked, returned an error, or needs JS rendering; also use "
            "when the user gives one URL and wants its verbatim content."
        ),
        params={
            "url": "The http(s) URL to fetch.",
            "formats": "List of output formats to include: text, markdown, html, raw, links. Defaults to markdown.",
            "waitSecs": "Max seconds to wait for the run to finish, 0-45 (default 30).",
        },
    )
    async def apify_web_fetch(
        self, url: str, formats: Optional[List[str]] = None, waitSecs: int = DEFAULT_RUN_WAIT_SECS,
    ) -> Dict:
        if not url:
            return {"success": False, "response": "url is required."}
        return await self._tool_call(
            "apify--web-fetch",
            {
                "url": url, "formats": formats or ["markdown"],
                "waitSecs": _clamp(waitSecs, 0, MAX_RUN_WAIT_SECS, DEFAULT_RUN_WAIT_SECS),
            },
        )

    @tool(
        description=(
            "Search Apify or Crawlee documentation (docs.apify.com / crawlee.dev) using full-text "
            "search. Follow up with fetch_apify_docs on a result URL for the full page content."
        ),
        params={
            "query": "Keywords to search for (not a full sentence/question).",
            "docSource": "Documentation source: 'apify' (default), 'crawlee-js', or 'crawlee-py'.",
            "limit": "Max results to return (default 5, max 20).",
            "offset": "Offset for pagination (default 0).",
        },
    )
    async def search_apify_docs(
        self, query: str, docSource: str = "apify", limit: int = 5, offset: int = 0,
    ) -> Dict:
        if not query:
            return {"success": False, "response": "query is required."}
        return await self._tool_call(
            "search-apify-docs",
            {"query": query, "docSource": docSource, "limit": _clamp(limit, 1, 20, 5), "offset": max(0, offset or 0)},
        )

    @tool(
        description="Fetch the full content of an Apify or Crawlee documentation page by its URL, found via search_apify_docs.",
        params={"url": "Full URL of the documentation page, including protocol."},
    )
    async def fetch_apify_docs(self, url: str) -> Dict:
        if not url:
            return {"success": False, "response": "url is required."}
        return await self._tool_call("fetch-apify-docs", {"url": url})

    @tool(
        description=(
            "Report a problem with an Apify Actor or tool to the Apify team -- call when an Actor/tool "
            "is missing, errors, times out, or returns a confusing/wrong/empty result, or a request "
            "could not be completed with the available tools. Do not include personal data, "
            "credentials, or verbatim private conversation content."
        ),
        params={
            "message": "What happened -- a few sentences, max 2000 characters.",
            "actorId": "Optional. The Actor this problem is about, e.g. 'apify/rag-web-browser'.",
            "actorRunId": "Optional. The Actor run this problem is about.",
        },
    )
    async def report_problem(
        self, message: str, actorId: Optional[str] = None, actorRunId: Optional[str] = None,
    ) -> Dict:
        if not message:
            return {"success": False, "response": "message is required."}
        return await self._tool_call(
            "report-problem", {"message": message[:2000], "actorId": actorId, "actorRunId": actorRunId},
        )
