## AI Agent Authentications (Provider Catalog)

This folder is a **data catalog** of authentication providers that can be shared across multiple App Agents.

### Concept

- **App Agents** (e.g. `outlook`, `microsoft_365`) may share a single **auth provider** (e.g. `microsoft`).
- Each provider has its own folder containing a single seed file with a consistent schema.

### Layout

```
ai_agents/ai_agent_authentications/
  <provider_code>/
    provider.seed.csv
```

### Seed schema (columns)

`name, code, auth_type, required_fields, config, is_active, created_at, updated_at, service_type`

- **required_fields**: JSON-encoded array of objects (stored as a CSV field)
- **config**: JSON-encoded object (stored as a CSV field)
- **NULL-like values**: represented as empty (e.g. config is blank when unknown)

