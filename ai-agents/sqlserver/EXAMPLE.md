## SQL Server AI Agent (example)

This document explains the existing **SQL Server** AI Agent tool and how it matches the creation guide.

For the full “how to build your own”, read:
- [AI Agent Tool Creation Guide](../../AI_AGENT_PLUGIN_CREATION_GUIDE.md)

---

### What this agent does

- **Purpose**: connect to Microsoft SQL Server and help with:
  - schema discovery (tables/columns/keys)
  - safe query generation
  - query execution (based on permission mode)

This tool uses `pyodbc` and expects SQL Server connectivity details to be provided in configuration.

---

### Where the code lives

- `ai-agents/sqlserver/plugin.json` (manifest)
- `ai-agents/sqlserver/entrypoint.py` (factory function)
- `ai-agents/sqlserver/sqlserver_agent_tool.py` (tool implementation)

---

### `plugin.json` in plain English

Key fields:
- **`plugin_id`**: `sqlserver`
- **`app_codes`**: `["sqlserver"]`
- **`type`**: `python_tool_agent`
- **`permissions`**: `["network", "database"]`

---

### How `entrypoint.py` works (important config keys)

Unlike simpler tools, this agent reads configuration from `app_config`:

- **Connection string**:
  - `app_config.connection_string`, or
  - `app_config.sqlserver_connection_string`

- **Permissions mode**:
  - `app_config.permissions` (default: `read-only`)

- **Optional extra instructions**:
  - `app_config.agent_instructions`

Then it creates `SQLServerAgentTool(connection_string=..., permissions=..., ...)`.

---

### Production checklist (common failure points)

- **ODBC drivers** must exist inside the runtime environment (container image).
- Connection string must be correct (server reachable, credentials valid).
- If you expose this tool to many users, keep **read-only** as the default and validate any write operations carefully.

