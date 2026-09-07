# AI Agent + Service Provider Creation Guide (Production)

> **Purpose**: A production-ready, UI-first guide to ship new AI agents and new service providers by **adding folders**—no internal persistence details, no deployment secrets, and no direct API references.

This guide explains how to add **new AI Agents** and (optionally) **new Service Providers** so anyone can enable them by **adding a folder** under:

- `ai-agents/` (AI Agent plugins)
- `service-providers/` (shared authentication providers)

> **Companion doc**: if what you're building is a deterministic, no-LLM workflow-graph building block (transform this list, filter these items, split a batch, run this code, start on a schedule) rather than an integration that reasons over natural language or calls a vendor API on behalf of a connected account, you want a **workflow Node**, not an AI Agent — see the [Workflow Node Creation Guide](NODE_CREATION_GUIDE.md) instead.

It is intentionally **implementation-agnostic**:

- No DB / internal platform logic
- No environment variable names or deployment secrets in this document
- No external paths
- No direct API endpoint references (configure/connect from the UI)

---

### Table of contents

- [What you can build](#what-you-can-build)
- [Folder contract (what must exist on disk)](#1-folder-contract-what-must-exist-on-disk)
- [AI Agent folder layout (supported)](#11-ai-agent-folder-layout-supported)
- [Create a new MCP Agent](#2-create-a-new-mcp-agent)
- [Create a new Function Wrapper Agent (fastest path)](#3-create-a-new-function-wrapper-agent-fastest-path)
- [Create a new Python Agent](#4-create-a-new-python-agent-full-custom-logic)
- [Dependencies inside your agent class](#41-dependencies-inside-your-agent-class-do-not-skip)
- [Multi-step plans & placeholders](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical)
- [Dependent options (dynamic dropdowns)](#5-dependent-options-dynamic-dropdowns)
- [Triggers (event-driven execution)](#6-triggers-event-driven-execution)
- [When does an agent need a Service Provider?](#7-when-does-an-agent-need-a-service-provider-auth)
- [Create a new Service Provider](#8-create-a-new-service-provider-auth-provider-catalog)
- [Drop-in folder enablement checklist](#9-drop-in-folder-enablement-checklist-what-a-user-does)
- [Safe update rules](#10-safe-update-rules-production)
- [Common causes of "plugin not loading"](#11-common-causes-of-plugin-not-loading)
- [Copy-from-real-examples](#12-copy-from-real-examples-already-in-this-repo)

### Quick glossary

- **AI Agent (tool)**: a capability the AI Employee can call (search, email, calendar, database, etc.).
- **Plugin folder**: `ai-agents/<agent_code>/` containing `plugin.json` + code.
- **Service Provider**: a reusable auth provider definition (e.g., Google / Microsoft) stored under `service-providers/<provider_code>/provider.json`.
- **`plugin_id`**: unique ID for the agent plugin (must not collide).
- **`app_codes`**: invocation name(s) for the plugin. In production, an agent should declare **exactly one** code in this array — one plugin folder = one invocable app code.
- **`service_provider_code`**: links an agent to a provider so the UI can show "Connect account". Not every agent that sets this will find a matching `service-providers/<code>/` folder on disk — some are wired up on the platform side. Don't assume the folder must exist; verify before building on top of it.
- **`param_options` / dependent options**: manifest block that tells the UI "this field's valid values depend on another field" (e.g. pick a table, then a pipeline inside that table).
- **Trigger**: an event-driven entry point declared on a plugin (e.g. "on email received", "on booking created") that a workflow can subscribe to and that fires the agent's logic when the external event happens — distinct from calling the agent as a normal tool.

---

## What you can build

### 1) Agent without auth (no external account needed)

Use this for tools like calculators, formatters, pure knowledge-base search, etc.

### 2) Agent with auth (external account needed)

Use this for agents that require the user/company to connect an account (OAuth2 or API token).

This is done by:

- Adding `service_provider_code` in the agent's `plugin.json`
- Ensuring the provider exists under `service-providers/<code>/provider.json` (when the provider is managed locally — see the glossary note above)
- Connecting the provider from the UI (one-time per user/company as your UI supports)

### 3) MCP Agent — hosted MCP server

Use this when the vendor already runs a hosted MCP server (e.g. `https://mcp.example.com`), or when you want to wrap a first-party MCP-style tool server with custom connection logic.

There are two flavors:

- **Generic** (no Python at all): declare `mcp_url` + `server_type` directly in `plugin.json`. The platform's built-in generic MCP tool handles the connection, OAuth token injection, and tool discovery automatically.
- **Custom entrypoint**: add your own `entrypoint.py` when you need custom pre/post-processing, a non-standard connection path, or richer behavior (e.g. dependent-option resolution, custom trigger wiring). The generic path is skipped and your code takes over. In practice, most real `mcp_wrapper` agents in production use the custom-entrypoint flavor — the fully generic path is a fallback for the simplest vendor integrations.

### 4) Function Wrapper Agent — plain Python class, zero boilerplate (fastest path for most new agents)

Use this when you're integrating a vendor's REST/SDK API yourself (not a hosted MCP server) and want to expose a handful of well-defined actions (e.g. "list emails", "send message", "create booking") without hand-writing an agent loop, a `run()` method, or an event stream.

You write **one plain Python class** with `@tool`-decorated async methods. The platform automatically:

- Turns each decorated method into a callable "tool" with a JSON-schema derived from your method signature
- Spawns your class behind a lightweight tool-serving process
- Runs the full ReAct reasoning loop (plan → call the right method(s) → observe → respond) on top of it — you never write that loop yourself

This is the **recommended default for new integrations** — it is less code than a full Python Agent and does not require you to implement `run()`, event streaming, or step-by-step orchestration by hand. See [Section 3](#3-create-a-new-function-wrapper-agent-fastest-path).

### 5) Python Agent — full custom logic

Use this only when your agent needs a bespoke execution loop that doesn't fit the "list of callable actions" shape — for example, an agent that runs its own multi-step reasoning process, manages long-running state, or needs to stream custom event types. See [Section 4](#4-create-a-new-python-agent-full-custom-logic).

---

## 1) Folder contract (what must exist on disk)

### Operator prerequisite (so folders are actually loaded)

> **UI-first rule**: If something doesn't appear, don't debug from code first. Restart the running processes and hard-refresh the UI. Folder-based plugins/providers only show up after reload.

Your deployment must be configured so:

- The **service provider catalog loader** reads the `service-providers/` root
- The **AI agent plugin loader** reads the `ai-agents/` root

After adding or changing folders/files, **restart** the relevant running processes and then **refresh the UI**. (Exact component names depend on your deployment.)

**Containerized/Docker deployments — external mount root:** in addition to the repo-bundled `ai-agents/` (and `service-providers/`) directories, a deployment can also mount an **external** plugin/provider root outside the repo (configured per-deployment). Both roots are scanned and merged, with the repo-bundled folder always taking precedence on a name collision. This is the supported way to drop a new agent or provider folder into a running Docker deployment without rebuilding the image — copy it into the deployment's external mount instead of the repo checkout, then restart/refresh as above. If a folder you added doesn't show up, confirm which root your deployment actually mounts before assuming the plugin itself is broken.

### Required layout — MCP Agent (generic, hosted MCP server, no Python needed)

```text
ai-agents/
  <agent_code>/
    plugin.json        ← declares mcp_url + server_type, no entrypoint field
    icon.svg           ← optional but recommended
```

### Required layout — MCP Agent (custom entrypoint)

```text
ai-agents/
  <agent_code>/
    plugin.json         ← type: mcp_wrapper, entrypoint set
    entrypoint.py
    __init__.py
    (optional) more python modules...
```

### Required layout — Function Wrapper Agent

```text
ai-agents/
  <agent_code>/
    plugin.json                       ← type: functions_wrapper
    entrypoint.py                     ← wires your class into the generic runner
    <agent_code>_functions.py         ← plain class with @tool-decorated methods
    __init__.py
    (optional) trigger_entrypoint.py  ← if this agent also exposes triggers
    (optional) icons/assets...
```

### Required layout — Python Agent (full custom logic)

```text
ai-agents/
  <agent_code>/
    plugin.json          ← type: python_tool_agent
    entrypoint.py
    __init__.py
    <agent_code>_agent_tool.py
    (optional) icons/assets...
    (optional) more python modules...
```

### Required layout for every Service Provider

```text
service-providers/
  <provider_code>/
    provider.json          (recommended)
    config.json            (recommended for OAuth URLs/credentials)
    meta.json               (optional)
    (optional) icon.svg / png...
```

### Supported service provider file merge order

If multiple JSON files exist for the same provider, they combine like this:

1. `meta.json` (optional base object)
2. `provider.json` (merged on top)
3. `config.json` (merged into the top-level `config` object)

**Practical split (recommended):**

- Put **labels + form schema** in `provider.json`
- Put **technical OAuth settings + client credentials** in `config.json`

---

## 1.1) AI Agent folder layout (supported)

This project uses a **flat** agent plugin layout. Do not create nested provider folders under `ai-agents/`.

### Flat layout (supported)

```text
ai-agents/
  <agent_code>/
    plugin.json
    ...
```

### Provider relationship (how the agent references auth)

Service providers stay in their own catalog:

- `service-providers/<provider_code>/...`

AI agents reference a provider **only** via `plugin.json`:

- `ai-agents/<agent_code>/plugin.json` → `"service_provider_code": "<provider_code>"`

**Rule:** the `service_provider_code` in `plugin.json` should match the provider folder name under `service-providers/` whenever the provider is locally managed. Some agents reference a `service_provider_code` that is wired up outside this folder tree (platform-side connection handling) — if you don't find a matching folder, don't assume it's a bug; confirm with whoever owns that integration before adding one.

---

## 2) Create a new MCP Agent

Use this when the vendor runs a hosted MCP server over HTTP or SSE.

### Generic path (no Python required)

#### Step A: create the agent folder

```text
ai-agents/
  <agent_code>/
    plugin.json
    icon.svg      ← optional
```

#### Step B: write `plugin.json`

```json
{
  "plugin_id": "<agent_code>",
  "app_codes": ["<agent_code>"],
  "type": "mcp_wrapper",
  "display_name": "Human Friendly Name",
  "description": "One sentence: what this agent does.",
  "service_provider_code": "<provider_code>",
  "icon": "icon.svg",
  "mcp_url": "https://mcp.example.com",
  "server_type": "http"
}
```

**Field reference:**

| Field | Required | Notes |
|---|---|---|
| `plugin_id` | yes | Unique across all agents. Match the folder name. |
| `app_codes` | yes | Array — in production keep exactly one entry (same as `plugin_id`). |
| `type` | yes | One of `"python_tool_agent"`, `"functions_wrapper"`, `"mcp_wrapper"`. Any other value fails validation. |
| `display_name` | yes | Shown in the UI. |
| `description` | yes | Used as the tool description the LLM sees. |
| `mcp_url` | yes (generic path, http/sse) | Base URL of the hosted MCP server. |
| `server_type` | yes (generic path) | `"http"` (streamable HTTP, recommended), `"sse"`, or `"stdio"`. |
| `service_provider_code` | if auth needed | Links to `service-providers/<code>/provider.json`. |
| `icon` | no | Relative path to an SVG/PNG in the same folder. |
| `entrypoint` | no | Omit for the generic path; set it (default filename `entrypoint.py`) to switch to the custom path. |
| `triggers` | no | See [Section 6](#6-triggers-event-driven-execution). |
| `param_options` | no | See [Section 5](#5-dependent-options-dynamic-dropdowns). |

**`server_type` guide:**
- Use `"http"` for MCP servers that support the streamable HTTP transport (most modern hosted servers).
- Use `"sse"` for older MCP servers that only support Server-Sent Events.
- Use `"stdio"` when the MCP server is a local process the platform should launch itself (e.g. a Node/Python script vendored alongside the agent) rather than a URL it connects to over the network.

**Stdio transport — manifest-level defaults:** for `"server_type": "stdio"`, put the launch command in `config` instead of `mcp_url`:

```json
{
  "...": "... plugin_id / app_codes / type / display_name / description ...",
  "config": {
    "server_type": "stdio",
    "command": "node",
    "args": ["./my-mcp-server/start-stdio.js"],
    "cwd": "./my-mcp-server",
    "env": { "SOME_FLAG": "1" }
  }
}
```

These `config.command` / `config.args` / `config.cwd` / `config.env` values act as the **out-of-the-box default** for every connection — a per-user/company connected-service config can still override any of them, and any value the connected-service config supplies always wins over the manifest default. This lets a self-hosted deployment that vendors the MCP server locally make stdio the default without needing a per-company database record, while deployments that don't have that local server can leave the manifest on `"http"`/`mcp_url` and let stdio be configured per-connection instead. Because this is a locally-launched process, only put a `command`/`args` here that you trust to be safely executable in every deployment that ships this plugin folder as-is — treat it the same as shipping any other executable.

### Custom entrypoint path (recommended when you need more control)

Add `"entrypoint": "entrypoint.py"` to `plugin.json` and create your own `entrypoint.py`:

```python
from __future__ import annotations
from typing import Any, Dict, Optional
from llm_tools.generic_mcp_agent_tool import GenericMCPAgentTool

async def create_tool_agent(
    *,
    app_code: str,
    app_config: Dict[str, Any],
    llm_provider: Any,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    # Customise mcp_url, server_type, or subclass GenericMCPAgentTool as needed.
    agent = GenericMCPAgentTool(
        mcp_url="https://mcp.example.com",
        server_type="http",
        tool_description="What this agent does.",
        llm_provider=llm_provider,
        token=token,
        user_id=user_id,
        company_id=company_id,
        agent_id=agent_id or "",
        app_config=app_config or {},
    )
    await agent.initialize()
    return agent
```

If you don't want a generic MCP wrapper at all, `entrypoint.py` can instead construct any object that satisfies the `BaseToolAgent` contract (see [Section 4.1](#41-dependencies-inside-your-agent-class-do-not-skip)) and return that.

---

## 3) Create a new Function Wrapper Agent (fastest path)

This is the type to reach for **by default** when adding a new vendor integration. You write plain Python methods; the platform supplies the agent loop, tool discovery, and orchestration.

### Step A: choose your agent code

Pick a short, stable code (lowercase + underscores) — becomes the folder name, `plugin_id`, and default `app_codes` entry.

### Step B: write `plugin.json`

```json
{
  "plugin_id": "<agent_code>",
  "app_codes": ["<agent_code>"],
  "type": "functions_wrapper",
  "display_name": "Human Friendly Name",
  "description": "One sentence: what it does.",
  "entrypoint": "entrypoint.py",
  "service_provider_code": "<provider_code>",
  "icon": "<icon_filename>"
}
```

Omit `service_provider_code` if the agent needs no external account.

### Step C: write your functions class

Create `ai-agents/<agent_code>/<agent_code>_functions.py`. Each public action is a plain `async def` method decorated with `@tool(...)`. The decorator inspects your method signature and builds the JSON-schema the LLM sees automatically — parameters without a default become required; typed hints (`int`, `float`, `bool`, `list`, `dict`, `str`) map to the matching JSON type.

```python
from __future__ import annotations

from typing import Dict, Optional

from llm_tools import tool
from backend.services.ai_agents.connected_service_tool_agent import ConnectedServiceToolAgent


class <VendorName>Functions(ConnectedServiceToolAgent):
    """One method per action this agent can perform."""

    @tool(
        description="Search or list records matching a query.",
        params={
            "search_query": "Vendor-specific search syntax.",
            "max_results": "Maximum number of results to return.",
        },
    )
    async def list_records(self, search_query: Optional[str] = None, max_results: int = 20) -> Dict:
        # Use self.access_token / self.credentials / self.connected_service_id here.
        ...
        return {"success": True, "response": "Found N records.", "result": {"items": [...]}}

    @tool(description="Create a new record.", params={"payload": "Fields for the new record."})
    async def create_record(self, payload: Dict) -> Dict:
        ...
        return {"success": True, "response": "Record created.", "result": {"id": "..."}}
```

**Rules:**

- Subclass `ConnectedServiceToolAgent` (not `BaseToolAgent` directly) whenever the agent needs auth — you get `self.access_token`, `self.credentials`, `self.connected_service_id`, and `self.refresh_access_token(...)` for free (see [Section 4.1](#41-dependencies-inside-your-agent-class-do-not-skip)). If no auth is needed, a plain class is fine.
- Every `@tool`-decorated method should return a dict shaped like `{"success": bool, "response": "<short summary>", "result": {...}}` — this becomes the tool's observation in the agent loop and, on the final call, the step's output (see [Section 4.2](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical) for the `need_discovery` / `execution_issue` conventions when something goes wrong).
- Keep each method focused on one action — the LLM picks which method to call based on `description` and `params`, so vague or overloaded methods lead to wrong tool selection.

### Step D: write `entrypoint.py`

```python
from __future__ import annotations

from typing import Any, Dict, Optional

from .<agent_code>_functions import <VendorName>Functions
from llm_tools.functions_wrapper_agent_tool import FunctionsWrapperAgentTool


async def create_tool_agent(
    *,
    app_code: str,
    app_config: Dict[str, Any],
    llm_provider: Any,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    agent = FunctionsWrapperAgentTool(
        functions_class=<VendorName>Functions,
        functions_config={
            "token": token,
            "user_id": user_id,
            "company_id": company_id,
            "agent_id": agent_id or "",
            "app_config": app_config or {},
        },
        llm_provider=llm_provider,
        token=token,
        user_id=user_id,
        company_id=company_id,
        agent_id=agent_id or "",
        app_config=app_config or {},
    )
    await agent.initialize()
    return agent
```

**What happens under the hood:** `FunctionsWrapperAgentTool` (the generic function-calling wrapper — the analogue of `GenericMCPAgentTool` but for a plain Python class instead of a remote MCP server) spawns your functions class behind a lightweight tool-serving process, auto-discovers every `@tool`-decorated method as a callable tool, and delegates the entire `run()` loop to the platform's shared reasoning brain. You never implement `run()`, event yielding, or step orchestration yourself — that's the whole point of this agent type.

If the connected service isn't set up yet, the wrapper automatically surfaces a "connect this app" prompt instead of failing — you don't need to hand-write that check inside every method, though you should still fail gracefully (return a clear `success: false` message) if a call is attempted without a token.

---

## 4) Create a new Python Agent (full custom logic)

Use this only when the agent's behavior doesn't fit "a list of callable actions" — for example it runs its own multi-step reasoning, manages long-running state across calls, or needs custom event types beyond the standard ones.

### Step A: choose your agent code

Pick a short, stable code (lowercase + underscores):

- Good: `invoice_writer`, `slack`, `jira_search`
- Avoid: spaces, uppercase, special characters

This `<agent_code>` becomes:

- Folder name: `ai-agents/<agent_code>/`
- `plugin_id` (recommended to match folder name)
- The default value in `app_codes` (recommended)

### Step B: create `plugin.json` (agent manifest)

Create `ai-agents/<agent_code>/plugin.json`.

**Manifest filename fallback (compat):**

Some deployments look for agent manifests in this order:

1. `plugin.json`
2. `app.json`
3. `manifest.json`

For best compatibility, always use **`plugin.json`**.

#### Minimal manifest (no auth)

```json
{
  "plugin_id": "<agent_code>",
  "app_codes": ["<agent_code>"],
  "type": "python_tool_agent",
  "display_name": "Human Friendly Name",
  "description": "One sentence: what it does.",
  "entrypoint": "entrypoint.py",
  "auto_attach_to_all_agents": true
}
```

`auto_attach_to_all_agents: true` means this tool is always available without the user explicitly enabling it — reserve this for small, universally-useful, no-auth utilities (calculators, date/time helpers, etc.). Leave it `false` (or omit it) for anything vendor-specific.

#### Manifest with auth (connect account in UI)

Add `service_provider_code` and (optionally) an icon.

```json
{
  "plugin_id": "<agent_code>",
  "app_codes": ["<agent_code>"],
  "type": "python_tool_agent",
  "display_name": "Human Friendly Name",
  "description": "One sentence: what it does.",
  "entrypoint": "entrypoint.py",
  "service_provider_code": "<provider_code>",
  "required_fields": [],
  "icon": "<icon_filename>",
  "auto_attach_to_all_agents": false
}
```

**Production rules for `plugin.json`:**

- **`plugin_id` must be unique** across all agent folders.
- **`app_codes` must be a non-empty array** (strings) — keep it to exactly one code per plugin.
- **`type`** must be exactly one of `"python_tool_agent"`, `"functions_wrapper"`, or `"mcp_wrapper"`.
- **`entrypoint` must point to a file that exists** — required for `python_tool_agent` and `functions_wrapper`; optional for `mcp_wrapper` (omit it to use the generic path).
- **`mcp_url` is required** when `type` is `"mcp_wrapper"` and no `entrypoint` is set.
- If you add `service_provider_code`, prefer that the provider exists under `service-providers/<provider_code>/provider.json` unless it's a known platform-managed exception.
- If you add `triggers` or `param_options`, they must validate against the shapes in [Section 5](#5-dependent-options-dynamic-dropdowns) and [Section 6](#6-triggers-event-driven-execution) — malformed entries fail manifest validation and the whole plugin is skipped.

### Step C: create `entrypoint.py` (plugin factory)

Create `ai-agents/<agent_code>/entrypoint.py`.

```python
from __future__ import annotations

from typing import Any, Dict, Optional

from .<agent_code>_agent_tool import <AgentClassName>


async def create_tool_agent(
    *,
    app_code: str,
    app_config: Dict[str, Any],
    llm_provider: Any,
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    agent = <AgentClassName>(
        llm_provider=llm_provider,
        agent_id=agent_id or "",
        token=token,
        company_id=company_id,
        user_id=user_id,
        app_config=app_config or {},
    )
    if hasattr(agent, "initialize"):
        await agent.initialize()
    return agent
```

**Notes:**

- Keep the function name **exactly** `create_tool_agent`.
- Use **relative imports** inside the agent folder (`from .x import y`).
- Whatever object you return is strictly validated: it must be an instance of the platform's `BaseToolAgent` base class and expose a callable `run()`. If it doesn't, plugin loading fails loudly rather than silently.

### Step D: create your tool class (the runtime behavior)

Create `ai-agents/<agent_code>/<agent_code>_agent_tool.py`.

Your class must:

- Extend the platform tool base class (`BaseToolAgent`)
- Implement `run(...)` as an **async generator** (not a coroutine that returns once — you `yield` events as you go)
- Always yield a **final** event
- Never leak secrets in yielded events or logs

```python
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

from backend.services.ai_agents.base_tool_agent import BaseToolAgent


def event(event_type: str, content: Any) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "content": content,
    }


class <AgentClassName>(BaseToolAgent):
    @classmethod
    def get_tool_responsibility(cls) -> str:
        return (
            "Explain what this tool does, what inputs it expects in tool_args, "
            "and what it returns."
        )

    def __init__(
        self,
        *,
        llm_provider: Any,
        agent_id: str,
        token: str,
        company_id: Optional[str],
        user_id: Optional[int],
        app_config: Dict[str, Any],
    ):
        self.llm_provider = llm_provider
        self.agent_id = agent_id
        self.token = token
        self.company_id = company_id
        self.user_id = user_id
        self.app_config = app_config

    async def run(
        self,
        user_query: str,
        provided_data: Optional[Any] = None,
        session_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict, None]:
        tool_args = tool_args or {}
        query = (tool_args.get("query") or user_query or "").strip()

        if not query:
            out = {"success": False, "error": "Missing query", "result": None}
            yield event("error", out)
            yield event("final", out)
            return

        # Replace with your logic. Keep outputs structured and predictable.
        out = {"success": True, "response": f"You said: {query}", "result": {"echo": query}}
        yield event("result", out)
        yield event("final", out)
```

**Common event `type` values** you'll see across agents in this codebase: `thought`, `plan`, `decision`, `tool_call`, `observation`, `result`, `error`, `final`, `done`. You are not restricted to these — define whatever custom event types make sense for your agent — but always end the stream with a `final` event, and make sure the `final` event's `content` carries the same `{success, response, result}` shape described in [Section 4.2](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical) so downstream steps can consume it.

---

## 4.1) Dependencies inside your agent class (do not skip)

Every agent instance is constructed by `entrypoint.py:create_tool_agent(...)`. The platform passes a consistent set of dependencies you must handle safely. This applies equally to Function Wrapper agents (your functions class constructor) and Python Agents.

### Construction-time dependencies (store on `self`)

- **`llm_provider`**: optional LLM client/adapter for intent parsing, summarization, drafting
  - **Rule**: do not crash if it is missing; make LLM use optional.
- **`app_config`**: dict of tool configuration and (for auth tools) the connected credential payload, under `app_config["service_credential"]`
  - **Rule**: always use `.get(...)` and default values; never assume keys exist.
- **`token`**: platform session token (platform-internal)
  - **Rule**: do not treat this as the vendor OAuth token.
- **`user_id`, `company_id`, `agent_id`**: context identifiers for scoping and correlation

### Auth-required dependency: connected service credential

Tools that require a connected account should subclass the platform's connected-service base class:

- `backend.services.ai_agents.connected_service_tool_agent.ConnectedServiceToolAgent`

In that model:

- The connected account payload is available on the agent as `self.credentials` (sourced from `app_config["service_credential"]["credentials"]`)
- The platform's connection identifier is exposed as `self.connected_service_id`
- The vendor access token is exposed as `self.access_token` — it resolves from whichever of `access_token` / `accessToken` / `token` / `api_key` / `api-key` is present in the credential payload, so you don't need to guess the vendor's exact key name
- `self.refresh_access_token(client=<optional httpx.AsyncClient>)` refreshes the token once and (if you pass a client) re-applies the new `Authorization` header to it for you

If the account is not connected, `self.access_token` may be missing/empty. Your agent must exit cleanly with a UI-actionable message rather than raising an unhandled exception.

### Standard "expired token" behavior (used by Gmail/Calendar/most OAuth agents)

When calling a vendor API:

- Make the request with `Authorization: Bearer <self.access_token>`
- If the vendor returns **401 Unauthorized**:
  - Refresh **once** using `await self.refresh_access_token(client=<your_httpx_client>)`
  - Retry the vendor request **once**
- If it still fails with 401:
  - Stop and return a **reconnect in UI** message (do not loop)

### Run-time inputs (provided per execution)

Function Wrapper method calls and Python Agent `run()` calls both receive, conceptually:

- **`user_query`** (Python Agent) / natural-language intent (Function Wrapper — resolved to a method call by the shared reasoning loop): the user's text for this tool step
- **`tool_args`** / method parameters: structured arguments supplied by the planner/executor (preferred)
- **`provided_data`**: optional outputs from earlier steps (context only)
- **`session_id`**: optional correlation id

**Rule of thumb:**

- Prefer deterministic values from `tool_args` / typed method parameters (IDs, filters, dates, recipients).
- Use free-text + LLM interpretation only as a fallback when structured args are missing or ambiguous.

### Production checklist for every networked agent

- **Timeouts**: always set HTTP timeouts; never hang indefinitely
- **Rate limits**: handle vendor rate-limit responses (backoff or clear "try again" output)
- **Pagination**: if listing/searching, return a `next_page_token` (or equivalent) when present
- **Safe errors**: return non-technical messages and never include tokens/credentials in outputs
- **Connection missing**: auth tools must handle missing credentials and exit cleanly

---

## 4.2) Multi-step plans, dependency placeholders, and "raise a step" behavior (critical)

Many real tools are executed as **multi-step plans** (for example: search → choose an id → take an action). The runner can pass outputs from earlier steps into later steps, but it can only do that if your tool returns **machine-readable results** with stable identifiers.

### What a "step" is

At runtime, a plan step typically contains:

- **`tool_name`**: which tool to run (matched by what the UI exposes)
- **`query`**: the instruction string for the tool
- **`tool_args`**: structured parameters (preferred)
- Optional metadata like `action`, `depends_on`, etc.

**Tool author rule:** make your tool deterministic from `tool_args` when possible; use LLM parsing of `query` only as fallback.

### Dependency resolution between steps (placeholders)

The runner may replace placeholder strings inside later-step `tool_args` with values from earlier-step results. Recognized placeholder forms:

- `{steps[N].result[M].field}`
  - \(N\) is **1-based step number**
  - \(M\) is **0-based index** within a list-like result
  - `field` is a key inside that item (example: `id`)
- `{step_N_result_message_id}` — a flattened shorthand form for a single well-known field
- `{{steps.N.result.id}}` or `{{steps.N.response.id}}`
  - Useful when a step returns an object with an `id` field

**Tool author rule:** return stable identifiers (`id`, `*_id`) so future steps have something reliable to reference.

### How to "raise" missing dependencies (trigger discovery instead of failing)

When a required parameter is missing (examples: `message_id`, `event_id`, `phone_number`), your tool should exit cleanly and tell the runner it needs discovery.

In your **final output content**, include:

- `success: false`
- `need_discovery: true`
- `missing_info: [...]`

Minimal example:

```json
{
  "success": false,
  "need_discovery": true,
  "missing_info": [
    {
      "parameter": "message_id",
      "reason": "No message id could be resolved from tool_args or previous steps.",
      "original_query": "..."
    }
  ],
  "result": { "success": false }
}
```

What happens next (runner behavior, conceptually):

- The runner attempts additional steps using other tools to resolve the missing value, checking merged context/execution history first.
- If it still cannot, it replans, and if that still can't resolve it, the UI asks the user to provide/select the missing value.

### How to signal a fixable execution issue (insert fix steps or ask the user)

Some failures are "fixable" but not purely "missing one field" (example: ambiguous selection, truncated input, missing identifier).

In those cases, return:

- `success: false`
- `execution_issue: true`
- `issue`: object with:
  - `code`: stable string (example: `ambiguous_selection`)
  - `message`: safe, user-readable explanation
  - `suggested_fix`: one of `resolve_from_step`, `discovery`, `replan`, `ask_user` (any other value is treated as `discovery`)
  - Optional: `prior_step_index`, `tool_name`, `fix_hint`, `context`

Minimal example:

```json
{
  "success": false,
  "execution_issue": true,
  "issue": {
    "code": "ambiguous_selection",
    "message": "Multiple matching records found; need a specific id to proceed.",
    "suggested_fix": "ask_user",
    "fix_hint": "Ask the user to choose one of the listed items."
  },
  "response": "Multiple matching records found; need a specific id to proceed."
}
```

The runner inserts fix steps and retries the failed step automatically — this is designed to be fully autonomous, so prefer `execution_issue` over just failing outright whenever the problem is something a follow-up step could plausibly resolve.

### Tool output shape (make steps easy)

In your **final** event (or Function Wrapper method return value), prefer:

- `success` (bool)
- `response` (string): short user-facing summary
- `result` (object): machine-readable data

Inside `result`, prefer:

- Stable identifiers (`id`, `*_id`)
- Lists under predictable keys (`items`, `messages`, `events`, `data`)
- A pagination cursor/token like `next_page_token` when available

Avoid returning only a large blob with no ids—future steps can't reference it reliably.

---

## 5) Dependent options (dynamic dropdowns)

Some fields only make sense once another field is chosen — e.g. you can't pick a "pipeline" until you've picked which "table" it lives in, and you can't pick a "status" until you've picked the pipeline. Rather than hardcoding those lists, declare the **dependency chain** in `plugin.json` and implement one function that resolves live options at request time.

### Step A: declare `param_options` in `plugin.json`

```json
{
  "...": "... rest of manifest ...",
  "param_options": {
    "<action_or_trigger_id>": {
      "<param_key>": {
        "depends_on": [],
        "description": "Human-readable hint for what this field selects."
      },
      "<child_param_key>": {
        "depends_on": ["<param_key>"],
        "description": "Depends on <param_key> being chosen first."
      },
      "<grandchild_param_key>": {
        "depends_on": ["<param_key>", "<child_param_key>"],
        "description": "Depends on both of the above."
      }
    }
  }
}
```

- `<action_or_trigger_id>` is the tool action name (a Function Wrapper method name, or a trigger's `trigger_id`) that owns this field.
- `<param_key>` can be a dotted path (e.g. `filter.conditions.field_id`) when the field is nested inside a larger argument object.
- `depends_on` lists the other `param_key`s (on the same action) whose chosen values must be supplied before this field's options can be resolved. An empty array means "resolvable with no other input."
- This block is validated at load time — a field referencing a `depends_on` key that never resolves, or a malformed shape, fails manifest validation for the whole plugin.

Real-world example (three-level chain — pick an AI Employee, then a table inside it, then a pipeline inside that table, then a status inside that pipeline):

```json
"param_options": {
  "record_list": {
    "ai_employee_id": { "depends_on": [], "description": "The user's AI Employees" },
    "table_id": { "depends_on": ["ai_employee_id"], "description": "Tables in the selected AI Employee" },
    "pipeline_id": { "depends_on": ["ai_employee_id", "table_id"], "description": "Pipelines on the selected table" },
    "status_id": { "depends_on": ["ai_employee_id", "table_id", "pipeline_id"], "description": "Statuses on the selected pipeline" }
  }
}
```

A simpler, common case is a two-level chain — e.g. a "language code" field whose valid values depend on which "template" was picked first:

```json
"param_options": {
  "send_template_message": {
    "template_name": { "depends_on": [], "description": "Available message templates" },
    "language_code": { "depends_on": ["template_name"], "description": "Languages available for the selected template" }
  }
}
```

Once a field is declared here, the UI knows to render it as a dynamic picker (calling back for live options as earlier fields are filled in) instead of a free-text input.

### Step B: implement `build_param_options` in `entrypoint.py`

Every plugin that declares `param_options` must implement this function in its `entrypoint.py`:

```python
from typing import Any, Dict, List, Optional


async def build_param_options(
    *,
    action_or_trigger_id: str,
    param_key: str,
    dependent_values: Dict[str, Any],
    app_config: Dict[str, Any],
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if action_or_trigger_id == "send_template_message" and param_key == "template_name":
        templates = await _fetch_templates(app_config)
        return [{"value": t["name"], "name": t["display_name"], "description": ""} for t in templates]

    if action_or_trigger_id == "send_template_message" and param_key == "language_code":
        template_name = dependent_values["template_name"]
        languages = await _fetch_languages_for_template(app_config, template_name)
        return [{"value": lang_code, "name": lang_label, "description": ""} for lang_code, lang_label in languages]

    return []
```

**Rules:**

- `dependent_values` is guaranteed to already contain every key listed in that field's `depends_on` — the platform checks this before calling you, so you don't need to defensively re-validate it's present (do still validate the *value* is usable).
- Each returned option is `{"value": ..., "name": ..., "description": ""}` — keep `value` in its native type (e.g. an integer id stays an int, don't stringify it).
- Prefer calling your own vendor client / functions class directly here rather than routing back through the LLM reasoning loop — this is a plain lookup, not a natural-language task, so keep it fast and deterministic. A common pattern is to dispatch on `param_key` via a small lookup table of resolver functions, e.g.:

```python
_RESOLVERS = {
    "template_name": _options_from_templates,
    "language_code": _options_from_languages,
}

async def build_param_options(*, param_key: str, dependent_values, app_config, **kwargs):
    resolver = _RESOLVERS.get(param_key)
    if not resolver:
        return []
    return await resolver(dependent_values=dependent_values, app_config=app_config)
```

- This same function is used for dependent options on both regular tool actions and trigger `input_schema` fields — `action_or_trigger_id` may be either a method name or a `trigger_id`.

---

## 6) Triggers (event-driven execution)

A **trigger** is a different kind of entry point than a normal tool call: instead of the agent being invoked because the user (or the planner) asked it to do something, a trigger fires because **something happened on the vendor's side** — an email arrived, a booking was created, a webhook was posted. A workflow can wire a trigger node in as its starting point, and the platform handles subscribing to the event source and calling your code back when it fires.

Every trigger in this codebase today is **event/webhook-driven** (the vendor pushes a notification to the platform), not a generic cron/schedule mechanism — for time-based ("every day at 9am") execution, see your platform's separate scheduling/workflow-scheduler tooling, which is a distinct system from agent triggers.

### Step A: declare `triggers` in `plugin.json`

```json
{
  "...": "... rest of manifest ...",
  "triggers": [
    {
      "trigger_id": "<vendor>_<event_name>",
      "display_name": "On <Event> Happening",
      "description": "What this trigger fires on and what it returns.",
      "entrypoint": "trigger_entrypoint.py",
      "input_schema": {
        "type": "object",
        "properties": {
          "some_required_setting": { "type": "string", "description": "Something the user must configure when wiring this trigger into a workflow." }
        },
        "required": ["some_required_setting"]
      },
      "config": {
        "some_static_setting": "baked-in, non-secret configuration this trigger needs at runtime"
      }
    }
  ]
}
```

**Field reference:**

| Field | Required | Notes |
|---|---|---|
| `trigger_id` | yes | Unique within this plugin. Used to identify the trigger everywhere (subscribe, execute, callback matching). |
| `display_name` / `description` | yes | Shown in the UI when picking a trigger for a workflow. |
| `entrypoint` | no | Defaults to `trigger_entrypoint.py`. Separate file from the tool's own `entrypoint.py`. |
| `input_schema` | no | JSON Schema describing what the *user* must configure when adding this trigger to a workflow (e.g. "which event type to watch"). Fields here can also be declared in `param_options` if their choices are dynamic. |
| `config` | no | Static, manifest-baked configuration (non-secret) the trigger implementation needs — e.g. a Pub/Sub topic name or a webhook verification token. Distinct from the per-instance `trigger_config` a caller supplies when subscribing. |

A plugin can declare more than one trigger (e.g. "on booking created", "on booking cancelled", "on booking rescheduled" as three separate `trigger_id`s in the same `triggers` array).

### Step B: implement `trigger_entrypoint.py`

Your trigger class must extend the platform's trigger base class and implement two async methods:

```python
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.services.ai_agents.connected_service_trigger_agent import ConnectedServiceTriggerAgent


class <VendorName><EventName>Trigger(ConnectedServiceTriggerAgent):
    @classmethod
    def get_trigger_responsibility(cls) -> str:
        return "Fires when <event> happens on the connected <vendor> account."

    async def subscribe(self, trigger_config: Dict[str, Any]) -> Dict[str, Any]:
        # 1. Call the vendor's API to start watching for this event
        #    (e.g. register a webhook, or start a push-notification watch).
        # 2. Persist a way to match an inbound webhook back to this exact
        #    workflow/trigger-node registration:
        callback_unique_identifier = "<something the vendor's callback payload will also contain, e.g. account id>"
        await self.register_workflow_trigger_callback(
            callback_unique_identifier=callback_unique_identifier,
            metadata={"anything_execute_trigger_will_need_later": "..."},
        )
        return {"success": True, "callback_unique_identifier": callback_unique_identifier}

    async def execute_trigger(
        self,
        trigger_body: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        # Called once the platform has matched an inbound webhook to this
        # workflow/trigger-node. `metadata` is whatever you saved in subscribe().
        # Do the real work (fetch full event details from the vendor if the
        # webhook payload was thin), and return a LIST of result objects —
        # this becomes the trigger node's output for the rest of the workflow.
        result_item = {"id": "...", "...": "..."}
        return [result_item]


async def create_trigger_agent(
    *,
    app_code: str,
    app_config: Dict[str, Any],
    token: str,
    user_id: int,
    company_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    trigger = <VendorName><EventName>Trigger(
        token=token,
        company_id=company_id,
        user_id=user_id,
        app_config=app_config or {},
    )
    if hasattr(trigger, "initialize"):
        await trigger.initialize()
    return trigger


async def handle_trigger_callback(
    *,
    app_code: str,
    trigger_defs: List[Dict[str, Any]],
    method: str,
    headers: Dict[str, str],
    query_params: Dict[str, str],
    raw_body: bytes,
) -> Dict[str, Any]:
    # This is the raw inbound-webhook handler for ALL triggers in this plugin.
    # You are fully responsible here for:
    #  - Verifying the request really came from the vendor (signature check,
    #    shared secret, etc.)
    #  - Answering any vendor verification handshake (e.g. echoing back a
    #    challenge query param on first webhook setup)
    #  - Extracting the callback_unique_identifier(s) this payload matches,
    #    so the platform can route it to the right subscribed workflow(s)
    return {
        "status_code": 200,
        "response_body": "OK",
        "response_headers": {},
        "response_content_type": "text/plain",
        "callback_unique_identifiers": ["<the matching identifier(s) from the payload>"],
    }
```

**The four responsibilities, split cleanly:**

- **`subscribe(trigger_config)`** — called once, when a user wires this trigger into a workflow. Talk to the vendor to start listening, then call `self.register_workflow_trigger_callback(...)` so the platform can later match an inbound webhook back to this exact workflow/node. Must return at least `{"success": bool, "error": Optional[str], ...}`.
- **`handle_trigger_callback(...)`** — a **module-level function** (not a method) that owns the raw inbound webhook: auth/signature verification, vendor handshake responses, and identifying which `callback_unique_identifier`(s) the payload belongs to. Whatever `status_code`/`response_body`/`response_headers`/`response_content_type` you return is sent back to the vendor verbatim — get this right or the vendor may disable the webhook.
- **`execute_trigger(trigger_body, metadata)`** — called once the platform has resolved the inbound event to a specific workflow/trigger-node. `metadata` is exactly what you saved via `register_workflow_trigger_callback` in `subscribe` (or re-saved on a later firing — see below). **Must return a list** — that list becomes the trigger node's output for the rest of the workflow, so shape each item the same way you would a tool's `result` (stable ids, predictable keys — see [Section 4.2](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical)).
- **`create_trigger_agent(...)`** — the factory, same idea as a tool's `create_tool_agent`. Keep the name exact.

**Advancing state across firings:** if your event source needs a cursor (e.g. "process everything since history id X"), re-call `self.register_workflow_trigger_callback(...)` inside `execute_trigger` with the same `callback_unique_identifier` but updated `metadata` (e.g. the new cursor value) before returning — this is how a trigger avoids reprocessing the same event twice on the next firing.

**Auth:** subclass `ConnectedServiceTriggerAgent` (mirrors `ConnectedServiceToolAgent` — same `self.access_token` / `self.credentials` / `self.connected_service_id` / `self.refresh_access_token(...)` pattern) whenever the trigger needs a connected vendor account. Use the same refresh-once-then-reconnect rule as tools.

### Trigger execution flow, end to end

1. **Discovery** — the workflow builder UI lists available triggers for an app by reading each plugin's `triggers` manifest entries (no credentials needed for this step).
2. **Subscribe** — when a user adds the trigger to a workflow and saves it, the platform resolves the connected credential, instantiates your trigger class, and calls `subscribe(trigger_config)` (where `trigger_config` is whatever the user filled in for your `input_schema`, with any `param_options` fields already resolved to concrete values). Your `subscribe()` registers the callback mapping so future events can find their way back to this workflow node.
3. **Vendor event fires** — the vendor calls back into the platform's webhook receiver. This dispatches to your `handle_trigger_callback(...)` for identification/verification, then the platform matches the returned `callback_unique_identifiers` against everything subscribed for this `app_code`.
4. **Execute** — for each matched workflow/trigger-node, the platform fetches that node's saved `metadata`, instantiates your trigger class again, and calls `execute_trigger(trigger_body, metadata)`. The returned list becomes that trigger node's output and the workflow continues from there, with later steps able to reference the trigger's output the same way they'd reference any other step's `result` (see the placeholder syntax in [Section 4.2](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical)).

---

## 7) When does an agent need a Service Provider (auth)?

An agent needs a Service Provider when it must call an external service on behalf of a user/company and therefore needs a connection step from the UI.

### Add auth to an agent

1) Create (or reuse) a provider under `service-providers/<provider_code>/provider.json`
2) Set `service_provider_code` in the agent's `plugin.json`
3) Start the app, then in the UI:
   - Find the agent
   - Click **Connect**
   - Complete the connection flow and grant permissions

After the UI connection is completed, the platform will provide the agent what it needs at runtime (the agent should not implement login screens).

---

## 8) Create a new Service Provider (auth provider catalog)

Service Providers are meant to be **shared** across many agents (example: multiple agents can reuse `google`).

### Step A: choose your provider code

Pick a stable code (lowercase + underscores), for example: `google`, `microsoft`, `github`, `salesforce`.

### Step B: create `provider.json`

Create `service-providers/<provider_code>/provider.json`.

#### Provider code rule (critical)

In this project, the provider is identified by its **code**, and your folder naming should align with it.

- Use a stable, URL-safe folder name (letters, numbers, `_`, `-`)
- Avoid spaces and path characters
- Keep the folder name and JSON `"code"` the same (example: `service-providers/google/provider.json` has `"code": "google"`).

#### Provider schema (aligned with this repo)

Your `service-providers/<provider_code>/provider.json` should follow the same keys used by the providers in this repo:

- **Display name**: `name` (string)
- **Provider code**: `code` (string)
- **Auth mode**: `auth_type` (string — observed values: `oauth2`, `api_key`, `custom`)
- **Credential form schema**: `required_fields` (array of fields)
- **Technical settings**: `config` (object, or `null` when there's nothing beyond `required_fields`)
- **Optional**: `icon` (relative path like `"./google.svg"`)
- **Optional**: `is_active` (boolean)

If your UI build also supports alternate key names, you can still include them, but for consistency, prefer the schema above.

#### `auth_type` values (what the UI does)

- **`oauth2`**: UI offers a browser **Connect** flow (redirect to vendor → return to UI)
- **`custom`**: UI shows a credential form (user pastes token/fields); no vendor redirect
- **`api_key`**: similar to custom; treat as "API key / connection params" entry form

#### `required_fields` shape (recommended)

Use an **array of field descriptors** for UI-friendly forms:

```json
[
  { "key": "client_id", "type": "text", "label": "Client ID" },
  { "key": "client_secret", "type": "password", "label": "Client Secret" }
]
```

You may also encounter deployments that accept an object-shape `required_fields`, but the array form is the best default for UI rendering.

#### OAuth2: what belongs in `config`

For `auth_type: "oauth2"`, `config` should include:

- Authorize URL (`authorization_endpoint` or equivalent key your deployment supports)
- Token URL (`token_endpoint` or equivalent key)
- Requested scope(s) (`scope` string or `scopes` array/string)
- Optional flow options: `access_type` (offline), `grant_types`, `is_client_registration_required`, `registration_endpoint` (dynamic client registration URL, if the vendor supports it), `is_revoke_api_json_based` (set when the vendor's token-revoke endpoint expects a JSON body rather than form-encoded), etc.
- Only put in `required_fields` what the vendor actually needs from the user — e.g. some vendors only need `client_id` for the OAuth flow to work, so don't add a `client_secret` field just to mirror another provider's shape. Never hardcode a real `client_id`/`client_secret` value into `provider.json` itself — those belong in the UI credential flow's stored config, not in the file every deployment ships with.

**Redirect URL rule (UI-first):** when registering the app at the vendor, always copy the **Redirect/Reply URL exactly as shown in your UI** for that provider/environment. Do not guess.

#### What the Service Provider "Add New Credential" screen shows (UI)

When a user creates a **new credential** for a Service Provider in the UI:

- The form fields come from the provider's `required_fields` (for example: Client ID, Client Secret).
- If the provider's `auth_type` is **`oauth2`**, the UI shows a **Connect** button that redirects the user to the vendor login/consent screen and then returns back to the UI.
- The UI also shows a **Redirect/Reply URL** that must be registered in the vendor console (always copy it from the UI for that environment).

**Example (OAuth provider credential screen):**

![Provider credential screen (required fields + OAuth connect)](docs/assets/provider-credential-required-fields-oauth.png)

---

Your `provider.json` can follow this shape (based on providers already in this repo):

```json
{
  "name": "Provider Display Name",
  "code": "<provider_code>",
  "auth_type": "oauth2",
  "required_fields": [
    { "key": "client_id", "type": "text", "label": "Client ID" },
    { "key": "client_secret", "type": "text", "label": "Client Secret" }
  ],
  "config": {
    "scope": "<scopes>",
    "authorization_endpoint": "<authorization endpoint>",
    "token_endpoint": "<token endpoint>",
    "grant_types": ["authorization_code", "refresh_token"],
    "access_type": "offline",
    "is_client_registration_required": false
  },
  "icon": "./<icon_file>",
  "is_active": true
}
```

#### Provider types you can use

- **`auth_type: "oauth2"`**: the UI will guide the user through an OAuth connect flow.
- **`auth_type: "custom"`**: the UI will collect one or more fields (for example an access token) and store them as the connected credential.
- **`auth_type: "api_key"`**: same UX category as custom; naming indicates "API key / connection parameters" semantics — good for things like database connection details (server, user, password) as well as a literal API key.

Example template for `custom`:

```json
{
  "name": "Provider Display Name",
  "code": "<provider_code>",
  "auth_type": "custom",
  "required_fields": [
    { "key": "access_token", "type": "text", "label": "Access Token" }
  ],
  "config": {},
  "is_active": true
}
```

**Production rules for `provider.json`:**

- **`code` must be unique** across all providers.
- Keep `required_fields` minimal and UI-friendly (clear labels).
- Treat `config` as non-secret provider metadata (endpoints, scopes, flags). Secrets should be entered/stored via the UI credential flow.
- If you include `icon`, store it in the same provider folder.
- Not every agent's `service_provider_code` needs a matching folder here — some are wired up elsewhere in the platform. Don't create a placeholder folder "just in case"; only add one when you're actually building the local provider definition.

---

## 9) "Drop-in folder" enablement checklist (what a user does)

This is the process for someone who wants to add your new agent/provider by copying folders.

### A) Add the AI Agent folder

- Copy `ai-agents/<agent_code>/` into the target instance's `ai-agents/` directory.

### B) If the agent needs auth, add the Service Provider folder too

- Copy `service-providers/<provider_code>/` into the target instance's `service-providers/` directory (skip this if the provider is platform-managed rather than locally defined).

### C) Restart the app and verify in UI

- The agent should appear in the UI (name, description, icon).
- If `service_provider_code` is set, the UI should show a **Connect** action.
- If `triggers` is set, the workflow builder should offer this agent's trigger(s) as a starting node option.

**Where you should see it (example):**

![All AI Agents screen (example)](docs/assets/all-ai-agents.png)

If your agent folder is correct and the app has been restarted/refreshed, your new agent will appear in the "All AI Agents" list like the example above.

### D) Connect credentials (UI)

- Use the UI to connect the provider once.
- Then run the agent/tool from the UI chat or tool picker, or wire its trigger into a workflow.

#### What appears in the "Connect" modal (driven by `plugin.json`)

When you click **Connect** on an AI Agent card, the UI modal is built from the agent's `plugin.json`:

- **Agent details**: `display_name`, `description`, and (optionally) `icon`.
- **Agent-specific config fields**: `required_fields` (if present in the agent `plugin.json`) are rendered as input fields after the description.
- **Select Credential**: if the agent has `"service_provider_code": "<provider_code>"`, the UI shows a **Select Credential** field so the user can choose an already-connected credential for that provider (or connect one first, depending on the UI flow).

**Example (Gmail Agent modal):**

![Gmail Agent modal with Connect](docs/assets/gmail-connect-modal.png)

**Example (Select Credential appears when provider auth is used):**

![Gmail Agent modal showing Select Credential](docs/assets/gmail-select-credential.png)

#### Selecting (or creating) a credential for the agent

When you open the **Select Credential** control, the UI shows:

- A list of **all connected credentials** for the agent's `service_provider_code`
- A search box to filter credentials
- A **Create** action to add a **new credential** for that same provider (then it becomes selectable)

After selecting the credential, click **Save** so the agent uses that credential going forward.

**Example (credential picker with Create button):**

![Credential picker (list + create)](docs/assets/credential-picker.png)

---

## 10) Safe update rules (production)

When updating an existing agent:

- **Do not change `plugin_id`** (it breaks existing references).
- **Do not remove `app_codes`** that users already rely on.
- **Keep output stable**: if your `result` shape changes, update consumers and UI expectations.
- **Always yield a `final` event** (Python Agent) or return the standard `{success, response, result}` shape (Function Wrapper method), even on errors.
- **Never output secrets** (tokens, credentials, authorization headers, private keys).
- **Do not remove or rename a `trigger_id`** that workflows have already subscribed to — existing subscriptions reference it by id; add a new trigger instead if the shape needs to change incompatibly.
- **Do not remove a `param_options` field's declaration** while UI forms still reference it as a dependent dropdown — coordinate the removal with the UI.

When updating a provider:

- **Do not change `code`** after it's in use.
- If you add new `required_fields`, ensure the UI can collect them and existing connections have a migration path (or stay backward compatible).

---

## 11) Common causes of "plugin not loading"

- **Invalid JSON** (trailing commas, comments).
- **Missing `entrypoint.py`** — for `python_tool_agent` and `functions_wrapper` the file must exist; for `mcp_wrapper` it is only required if you set `"entrypoint"` in `plugin.json`.
- **Missing `mcp_url`** — `mcp_wrapper` plugins without an `entrypoint` must declare `mcp_url`; without it the plugin is skipped.
- **Wrong `server_type`** — use `"http"` for streamable HTTP servers, `"sse"` for SSE-only servers, and `"stdio"` only when you actually vendor a local launchable server with the plugin; a mismatch will cause a connection failure.
- **Wrong `type` value** — must be exactly `"python_tool_agent"`, `"functions_wrapper"`, or `"mcp_wrapper"`; anything else fails manifest validation.
- **`create_tool_agent` doesn't return a `BaseToolAgent`** — the loader strictly validates the returned object; a plain dict or the wrong class fails loudly.
- **Duplicate `plugin_id`** across agent folders.
- **`app_codes` has more than one entry** — supported historically, but treat as a single-code array going forward; extra entries can cause ambiguous routing.
- **Import errors** in Python files (prefer relative imports in the agent folder).
- **Auth mismatch**: `service_provider_code` points to a provider that does not exist locally and isn't a known platform-managed exception.
- **Malformed `triggers` or `param_options`** — a `depends_on` referencing a key that's never declared, or a trigger missing `trigger_id`, fails validation for the whole plugin, not just that entry.
- **Missing `handle_trigger_callback`** — if a plugin declares `triggers` but its `trigger_entrypoint.py` doesn't define this module-level function, inbound webhooks for that app can't be routed anywhere.

---

## 12) Copy-from-real-examples (already in this repo)

If you want working reference folders to copy (with "how it works" notes), start with these:

### MCP Agent examples (type: `mcp_wrapper`)

- **Generic hosted MCP server (no Python files)**
  - Best for: your first MCP agent when the vendor already runs a hosted MCP server — shows the minimal 2-file layout (`plugin.json` + `icon.svg`)

- **Custom-entrypoint MCP wrapper (first-party MCP-style server, dependent options, triggers)**
  - Best for: seeing `mcp_wrapper` combined with a custom `entrypoint.py`, a rich `param_options` block, and multiple triggers all in one plugin

### Function Wrapper Agent examples (type: `functions_wrapper`)

- **Small, single-provider, API-key auth, no trigger**
  - Best for: your first Function Wrapper agent — the minimal shape of `plugin.json` + `entrypoint.py` + a functions class with a handful of `@tool` methods

- **Full-featured OAuth agent with a trigger**
  - Best for: seeing `service_provider_code` (OAuth), a large `@tool` surface, and one webhook trigger together in a mature agent

- **Multiple webhook triggers + dependent `param_options`**
  - Best for: an agent with several distinct `trigger_id`s (created/cancelled/rescheduled-style events) plus a two-level dependent-options chain

- **`custom` auth_type + webhook trigger with verification handshake**
  - Best for: seeing a `handle_trigger_callback` that must answer a vendor's webhook verification challenge, and a provider using `auth_type: "custom"` with several `required_fields`

### Python Agent examples (type: `python_tool_agent`)

- **Minimal, no auth, no `auto_attach_to_all_agents`**
  - Best for: your first simple tool (`plugin.json` + `entrypoint.py` + one tool file), when the task doesn't fit the "list of actions" shape a Function Wrapper wants

- **Minimal, no auth, always-attached utility**
  - Best for: seeing `auto_attach_to_all_agents: true` used correctly, for a small universally-useful no-auth tool

- **Config-heavy, permissions-aware, own agentic loop**
  - Best for: tools that rely on `app_config` for connection/settings, enforce safe modes (e.g. read-only vs write), and run their own internal reasoning loop rather than delegating to the shared one

- **Uses `need_discovery` directly for a missing required parameter**
  - Best for: seeing the discovery-raise pattern from [Section 4.2](#42-multi-step-plans-dependency-placeholders-and-raise-a-step-behavior-critical) implemented end to end

### Provider definitions (shared auth catalog)

- OAuth2 examples (Google-style and Microsoft-style: scopes, authorization/token endpoints, `access_type: offline`, refresh grant)
- `custom` auth_type example (single `access_token` field, e.g. a GitHub-style personal access token)
- `api_key`-style example with multiple connection fields and no `config` object (e.g. a database-style server/user/password provider)

**Worked example (recommended):**

- A full end-to-end worked example (with screenshots) walking through building one OAuth-backed agent and its matching provider from scratch is the fastest way to internalize this whole guide — build one small agent yourself following [Section 3](#3-create-a-new-function-wrapper-agent-fastest-path) or [Section 4](#4-create-a-new-python-agent-full-custom-logic) before attempting anything with triggers or dependent options.
