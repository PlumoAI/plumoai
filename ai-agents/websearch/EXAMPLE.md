## Web Search AI Agent (example)

This document explains the existing **Web Search** AI Agent tool and how it matches the creation guide.

For the full “how to build your own”, read:
- [AI Agent Tool Creation Guide](../../AI_AGENT_PLUGIN_CREATION_GUIDE.md)

---

### What this agent does

- **Purpose**: answers questions that require **current / real-time** web information.
- **How**: it relies on the connected LLM provider having **web search enabled** (it does not use a separate third-party search API in this tool).

---

### Where the code lives

- `ai-agents/websearch/plugin.json` (manifest)
- `ai-agents/websearch/entrypoint.py` (factory function)
- `ai-agents/websearch/web_search_agent_tool.py` (tool implementation)

---

### `plugin.json` in plain English

Key fields:
- **`plugin_id`**: `websearch` (unique id)
- **`app_codes`**: `["websearch"]` (how the platform refers to this tool)
- **`type`**: `python_tool_agent` (this is a Python tool)
- **`auto_attach_to_all_agents`**: `true` (available broadly)
- **`permissions`**: `["network"]` (it may call the internet)

---

### How `entrypoint.py` works

The platform calls `create_tool_agent(...)`. This file simply:
- imports `WebSearchAgentTool`
- creates it with `llm_provider`, `token`, `user_id`, etc.
- calls `initialize()` if present
- returns the tool instance

This is the recommended pattern from the creation guide.

---

### Inputs (tool_args)

This tool accepts either:
- `tool_args={"query": "..."}` (preferred), or
- a plain `user_query` string

If both are present, it prefers `tool_args.query`.

---

### Output (events)

The tool streams event dictionaries with keys:
- `id`, `type`, `timestamp`, `content`

It ends with a `final` event containing a stable envelope like:
- `{"success": true, "response": "...", "result": {...}}`

---

### Common “why doesn’t it search the web?” causes

- Your model/provider is not configured with web search capability.
- Web-search capability is disabled in the model configuration used by the platform.

