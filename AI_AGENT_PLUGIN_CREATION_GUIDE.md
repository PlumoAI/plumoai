## AI Agent Tool **Creation** Guide (Production)

This guide teaches you how to **create** your own AI Agent tool or update an existing one.

This is written to be:
  - **Production-level** (covers the small details that usually break tools)
- **Step-by-step** (copy/paste templates)
- **Easy to understand** (with a simple glossary)

**Scope restriction:** tool-only. Everything you change lives under `ai-agents/`.

---

### Quick glossary (plain English)

- **AI Agent tool**: a “capability” your AI Employee can use (web search, memory, database query, etc.).
- **Folder**: a directory on disk (example: `ai-agents/websearch/`).
- **`plugin.json`**: the tool manifest file (name, id, how to load it). The filename is `plugin.json`.
- **`entrypoint.py`**: the “start file” that creates your tool class.
- **`BaseToolAgent`**: a base class your tool must extend so the platform knows how to run it.
- **`tool_args`**: structured inputs passed to your tool (like `query="..."`).
- **Docker Compose**: the thing that runs PlumoAI as multiple containers.

---

## 1) Where tools live (and why the name looks confusing)

In this repository, tool folders live here:

- **Host path**: `ai-agents/`

But at runtime, `docker-compose.yml` mounts them into the `ai-service` container here:

- **Container path**: `/opt/plumoai/app_agents`

So inside the running container, the import path stays:

- `app_agents.*`

This is intentional because the `ai-service` Docker image expects the loader path to be `/opt/plumoai/app_agents`.

---

## 2) The simplest way to create a new AI Agent tool (recommended)

If you are new, **copy an existing agent** and edit it.

### Step A: Pick a new name (important)

Choose a short, lowercase name with underscores:

- good: `hello_tool`, `weather_lookup`, `invoice_writer`
- avoid: spaces, uppercase, special characters

This name becomes:
- the folder name: `ai-agents/<name>/`
- the `plugin_id`
- the `app_code` users/tools will reference

### Step B: Create your tool folder

Create:

```text
ai-agents/<your_tool_name>/
  plugin.json
  entrypoint.py
  __init__.py
  <your_tool_name>_agent_tool.py
```

### Step C: Write `plugin.json` (tool manifest)

Create `ai-agents/<your_tool_name>/plugin.json`:

```json
{
  "plugin_id": "your_tool_name",
  "app_codes": ["your_tool_name"],
  "type": "python_tool_agent",
  "display_name": "Your Tool Name",
  "description": "One sentence: what it does.",
  "entrypoint": "entrypoint.py",
  "version": "0.1.0",
  "core_min_version": "0.1.0",
  "auto_attach_to_all_agents": true,
  "permissions": []
}
```

**Small details that matter:**
- `plugin_id` must be unique (if duplicated, only the first one loads).
- `app_codes` must be a non-empty list of strings.
- `type` must be exactly `python_tool_agent` (for Python tools).
- `entrypoint` must be a file inside your tool folder.

### Step D: Write `entrypoint.py` (tool factory)

Create `ai-agents/<your_tool_name>/entrypoint.py`:

```python
from __future__ import annotations

from typing import Any, Dict, Optional

from .your_tool_name_agent_tool import YourToolNameAgentTool


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
    agent = YourToolNameAgentTool(
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

### Step E: Write your tool class (the thing that runs)

Create `ai-agents/<your_tool_name>/<your_tool_name>_agent_tool.py`.

**Production rule:** your tool must yield event dictionaries and finish with a `final` event.

```python
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

from backend.services.app_agents.base_tool_agent import BaseToolAgent


def event(event_type: str, content: Any) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "content": content,
    }


class YourPluginNameAgentTool(BaseToolAgent):
    @classmethod
    def get_tool_responsibility(cls) -> str:
        return "Describe exactly what this tool does and what tool_args it expects."

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
    ) -> AsyncGenerator[Dict, None]:
        # 1) Validate inputs
        query = (tool_args or {}).get("query") or user_query
        query = (query or "").strip()
        if not query:
            out = {"success": False, "error": "Missing query", "result": None}
            yield event("error", out)
            yield event("final", out)
            return

        # 2) Do work (replace this with your logic)
        out = {"success": True, "response": f"You said: {query}", "result": {"echo": query}}

        # 3) Stream results
        yield event("result", out)
        yield event("final", out)
```

---

## 3) Updating an existing AI Agent tool (safe production checklist)

When you change an existing tool under `ai-agents/`:

- **Keep `plugin_id` the same** (changing it can break selection).
- **Keep `app_codes` the same** unless you intentionally change how it’s called.
- **Do not remove the `final` event.**
- **Do not crash on missing inputs**: validate and return a clean `error` + `final`.
- **Never log or yield secrets** (tokens, passwords, raw Authorization headers).

---

## 4) The 5 most common “minor things” that break tools

- **Wrong file names**: `plugin.json` must exist and `entrypoint.py` must exist.
- **Wrong JSON**: a trailing comma makes `plugin.json` invalid JSON.
- **Duplicate IDs**: duplicate `plugin_id` or duplicate `app_codes` mapping (first one wins).
- **Import mistakes**: avoid `from app_agents...` imports inside your tool folder; use `from .x import y` for tool-local code.
- **No final event**: always yield a `final` event.

---

## 5) Examples you can copy (real agents in this repo)

Read these first if you learn better from examples:

- **Web Search** (simple, minimal config): `ai-agents/websearch/EXAMPLE.md`
- **Memory** (larger, production-grade module layout): `ai-agents/memory/EXAMPLE.md`
- **SQL Server** (uses `app_config` for connection + permissions): `ai-agents/sqlserver/EXAMPLE.md`

Direct links:
- [Web Search example](ai-agents/websearch/EXAMPLE.md)
- [Memory example](ai-agents/memory/EXAMPLE.md)
- [SQL Server example](ai-agents/sqlserver/EXAMPLE.md)


