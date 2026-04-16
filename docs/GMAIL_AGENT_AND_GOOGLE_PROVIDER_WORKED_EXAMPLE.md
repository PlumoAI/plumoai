## Worked Example: Gmail AI Agent + Google Service Provider (Production)

This is a **worked example** that follows the main guide:

- [`AI_AGENT_PLUGIN_CREATION_GUIDE.md`](AI_AGENT_PLUGIN_CREATION_GUIDE.md)

Use this document when you want to build a **provider-authenticated** agent like Gmail, and then copy the same pattern for Microsoft, GitHub, or any other vendor.

This document is **UI-first**:

- No backend/DB details
- No environment variables
- No external paths
- No direct API endpoint references (use the UI for connect/redirect)

---

### Screenshots used in this worked example

- “All AI Agents” list: `docs/assets/all-ai-agents.png`
- Agent Connect modal: `docs/assets/gmail-connect-modal.png`
- Agent Select Credential modal: `docs/assets/gmail-select-credential.png`
- Credential picker (list + create): `docs/assets/credential-picker.png`
- Provider “Add New Credential” (required fields + OAuth connect): `docs/assets/provider-credential-required-fields-oauth.png`

### 1) Relationship: Service Provider vs AI Agent (what connects to what)

- A **Service Provider** defines **how authentication/connection happens** (OAuth2 or custom token fields). It appears in the UI under integrations/credentials/services.
- An **AI Agent** defines **what the tool can do** (read/send emails, list labels, etc.). It appears in the UI under agent tools/apps.
- The link is one string:
  - Agent `plugin.json` → `service_provider_code`
  - Provider folder name under `service-providers/` must match that code.

For Gmail:

- Provider code: `google`
- Agent code: `gmail`
- Gmail agent `plugin.json` includes `"service_provider_code": "google"`

---

## 2) Create the Google Service Provider folder

### 2.1 Folder layout

Create:

```text
service-providers/
  google/
    provider.json
    (optional) config.json
    (optional) meta.json
    (optional) google.svg
```

### 2.2 `provider.json` (what the UI displays + which connect flow to use)

In this repository, there is already a working example you can mirror:

- `service-providers/google/provider.json`

Minimum rules to follow:

- Folder name `google` is the provider code
- Set a clear display name (`provider_name` recommended)
- Set `auth_type: "oauth2"` for a Connect flow
- Add `required_fields` so the UI can show a credential form (Client ID/Secret)
- Add an `icon` pointing at a file inside the same folder (optional)

### 2.2.1 Create a credential in the UI (OAuth provider)

When the Google provider is `auth_type: "oauth2"`, the UI shows the provider’s **required fields** and an OAuth **Connect** button.

![Provider credential screen (required fields + OAuth connect)](docs/assets/provider-credential-required-fields-oauth.png)

### 2.3 `config.json` (recommended split for OAuth settings)

If your deployment supports `config.json` merging (see the main guide), put OAuth technical settings and client credentials there so operators know where secrets live.

**UI rule:** when registering the OAuth app at the vendor, always copy the Redirect/Reply URL **exactly** as shown in your UI for the `google` provider on that environment.

---

## 3) Create the Gmail AI Agent folder

### 3.1 Folder layout

Create:

```text
ai-agents/
  gmail/
    plugin.json
    entrypoint.py
    __init__.py
    gmail_agent_tool.py
    (optional) gmail.svg
```

### 3.2 `plugin.json` (the critical wiring)

This repository already contains a working Gmail plugin manifest:

- `ai-agents/gmail/plugin.json`

The important parts (copy this pattern for your own agent):

- `plugin_id` and `app_codes` identify the tool (`gmail`)
- `entrypoint` points to `entrypoint.py`
- `service_provider_code` links the tool to the provider (`google`)
- `icon` (optional) is shown in the UI

---

## 4) How Gmail agent code uses dependencies (what to copy for your own agent)

This repo’s Gmail tool class is:

- `ai-agents/gmail/gmail_agent_tool.py` → `class GmailAgentTool(...)`

### 4.1 The base class for auth-required agents

Gmail extends the connected-service base class used across provider-authenticated tools:

- `ConnectedServiceToolAgent`

This gives Gmail a consistent credential surface:

- `self.access_token` (vendor access token, if connected)
- `self.connected_service_id` (platform connection id)
- `self.refresh_access_token(...)` (standard refresh path)

### 4.2 “Not connected” behavior

The Gmail agent checks for `self.access_token` at the start of `run(...)`.

If missing:

- It returns a result telling the user to **connect Gmail in the UI**
- It yields a `final` event and exits cleanly

This is the most important production behavior for any auth-required agent.

### 4.3 HTTP client setup and safe retry

Gmail:

- Creates one `httpx.AsyncClient` with a timeout
- Sends `Authorization: Bearer <self.access_token>` to the vendor
- On **401**, it refreshes once and retries once:
  - Refresh: `await self.refresh_access_token(client=self._httpx_client)`
  - Retry the request once after refresh

Copy this exact strategy for Microsoft/Google-like OAuth agents.

### 4.4 Tool configuration from `app_config`

Gmail reads tool-level configuration from `app_config` (example: permissions such as read-only vs full).

Production rule:

- Treat `app_config` as untrusted input: always `.get(...)`, validate allowed values, and default safely.

---

## 5) UI workflow (what an operator/user actually does)

### 5.1 Load folders and verify

- Add the `service-providers/google/` folder
- Add the `ai-agents/gmail/` folder
- Restart the running processes (provider catalog loader + agent plugin loader)
- Refresh the UI and verify:
  - Google provider appears in integrations/credentials/services
  - Gmail agent appears in agent tools/apps

**Where Gmail should appear (example):**

![All AI Agents screen (example)](docs/assets/all-ai-agents.png)

### 5.2 Connect Google once, reuse for Gmail (and other Google tools)

- In the UI, connect the `google` provider once (OAuth Connect)
- Then use/attach the Gmail tool in the UI

**Gmail agent Connect entry point (example):**

![Gmail Agent modal with Connect](docs/assets/gmail-connect-modal.png)

**Select Credential appears because Gmail uses a provider (`service_provider_code: "google"`):**

![Gmail Agent modal showing Select Credential](docs/assets/gmail-select-credential.png)

**Credential picker (list existing credentials, or Create a new one):**

![Credential picker (list + create)](docs/assets/credential-picker.png)

**Key idea:** users connect **providers**, not “folders”. Multiple tools can reuse the same provider connection.

---

## 6) How to copy this example for Microsoft or GitHub

### Microsoft (OAuth2)

- Create `service-providers/microsoft/` (oauth2 provider)
- Create `ai-agents/<your_microsoft_tool>/` and set `"service_provider_code": "microsoft"`
- In code, extend the same connected-service base class and follow the same 401→refresh→retry pattern

### GitHub (custom token)

- Create `service-providers/github/` with `auth_type: "custom"` and a required field like `access_token`
- Create `ai-agents/<github_tool>/` and set `"service_provider_code": "github"`
- In code, treat `self.credentials` as the source of the token field(s) and fail cleanly if missing

---

## 7) Quick validation checklist (Gmail-style agent)

- [ ] Provider folder exists and appears in the UI
- [ ] Agent folder exists and appears in the UI
- [ ] Agent `plugin.json.service_provider_code` matches provider folder name exactly
- [ ] Agent exits cleanly when not connected (no stack traces; yields `final`)
- [ ] Agent uses timeouts and avoids infinite retries
- [ ] Agent never outputs secrets in streamed events or logs
