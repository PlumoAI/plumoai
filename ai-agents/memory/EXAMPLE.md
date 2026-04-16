## Memory AI Agent (example)

This document explains the existing **Memory** AI Agent tool and how it matches the creation guide.

For the full “how to build your own”, read:
- [AI Agent Tool Creation Guide](../../docs/AI_AGENT_PLUGIN_CREATION_GUIDE.md)

---

### What this agent does

- **Purpose**: long-term memory for a user across sessions (facts, preferences, goals, corrections).
- **How**: it talks to the PlumoAI backend “Memory API” through the platform’s internal services.

This tool is designed to be used in an **autonomous** way: it can decide whether to store, recall, update, forget, or do nothing based on the conversation.

---

### Where the code lives

- `ai-agents/memory/plugin.json` (manifest)
- `ai-agents/memory/entrypoint.py` (factory function)
- `ai-agents/memory/memory_agent_tool.py` (main tool surface)
- Supporting modules:
  - `api_client.py`, `llm_client.py`, `events.py`, `json_utils.py`, `scoring.py`, `tagging.py`, etc.

This is a good example of the **recommended production layout** (split code into smaller files).

---

### `plugin.json` in plain English

Key fields:
- **`plugin_id`**: `memory`
- **`app_codes`**: `["memory"]`
- **`type`**: `python_tool_agent`
- **`auto_attach_to_all_agents`**: `true`
- **`permissions`**: `["network"]` (it calls internal APIs)

---

### How `entrypoint.py` works

The platform calls `create_tool_agent(...)`. This file:
- imports `MemoryAgentTool`
- creates it with `llm_provider`, `token`, `company_id`, `user_id`, `agent_id`
- calls `initialize()` if present

---

### Inputs (tool_args)

The Memory tool supports multiple operations. The most common is:

- **`operation="auto"`** (default): pass the raw user message + optional recent conversation, and the tool decides what to do.

It also supports explicit operations like:
- `store`, `recall`, `update`, `forget`, `reflect`, `list`

See `MemoryAgentTool.TOOL_DESCRIPTION` in `memory_agent_tool.py` for the full list of supported keys.

---

### Output (events)

This tool uses a shared event helper (see `events.py`) and consistently yields:
- progress/thought events
- result events
- a final event envelope

---

### Production notes

- **Do not leak tokens**: this tool uses authenticated internal calls.
- **Be careful when changing scoring/tagging thresholds**: small changes can cause noisy memory writes or missed recalls.

