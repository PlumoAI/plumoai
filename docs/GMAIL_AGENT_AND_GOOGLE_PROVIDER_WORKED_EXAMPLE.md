## Worked Example: Gmail AI Agent + Google Service Provider (Production)

This is a **worked example** that follows the main guide:

- [`AI_AGENT_PLUGIN_CREATION_GUIDE.md`](AI_AGENT_PLUGIN_CREATION_GUIDE.md)

Use this document when you want to build a **provider-authenticated Function Wrapper agent** like Gmail — including one with a **trigger** — and then copy the same pattern for Microsoft, GitHub, or any other vendor.

This document is **UI-first**:

- No backend/DB details
- No environment variables
- No external paths
- No direct API endpoint references (use the UI for connect/redirect)

---

### Screenshots used in this worked example

- “All AI Agents” list: `assets/all-ai-agents.png`
- Agent Connect modal: `assets/gmail-connect-modal.png`
- Agent Select Credential modal: `assets/gmail-select-credential.png`
- Credential picker (list + create): `assets/credential-picker.png`
- Provider “Add New Credential” (required fields + OAuth connect): `assets/provider-credential-required-fields-oauth.png`

### 1) Relationship: Service Provider vs AI Agent (what connects to what)

- A **Service Provider** defines **how authentication/connection happens** (OAuth2 or custom token fields). It appears in the UI under integrations/credentials/services.
- An **AI Agent** defines **what the tool can do** (read/send emails, list labels, etc.). It appears in the UI under agent tools/apps.
- The link is one string:
  - Agent `plugin.json` → `service_provider_code`
  - Provider folder name under `service-providers/` must match that code.

For Gmail:

- Provider code: `google`
- Agent code: `gmail`
- Agent type: `functions_wrapper` (a plain Python class of `@tool`-decorated actions — see [Section 4](#4-how-gmail-agent-code-uses-dependencies-what-to-copy-for-your-own-agent))
- Gmail agent `plugin.json` includes `"service_provider_code": "google"`
- Gmail also declares one **trigger** (`gmail_email_received`) — see [Section 5](#5-the-gmail-trigger-on-email-received)

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

Its real shape today:

```json
{
  "name": "Google",
  "code": "google",
  "auth_type": "oauth2",
  "required_fields": [
    { "key": "client_id", "type": "text", "label": "Client ID" },
    { "key": "client_secret", "type": "text", "label": "Client Secret" }
  ],
  "config": {
    "scope": "https://www.googleapis.com/auth/calendar https://mail.google.com/ https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/chat.spaces https://www.googleapis.com/auth/chat.messages https://www.googleapis.com/auth/chat.messages.reactions https://www.googleapis.com/auth/chat.memberships https://www.googleapis.com/auth/chat.customemojis",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "access_type": "offline",
    "grant_types": ["authorization_code", "refresh_token"],
    "is_client_registration_required": false
  },
  "is_active": true
}
```

**Notes worth calling out:**

- This is **one shared Google provider**, not a Gmail-specific one — its `scope` string already includes Calendar, Gmail, YouTube, and Google Chat scopes, because several Google-family agents (Gmail, Google Calendar, Google Chat, Google Meet, YouTube, Google Drive/Sheets) all reuse this same `google` connection. If you add a new Google-family agent, extend this shared `scope` string rather than creating a second Google provider.
- `required_fields` only asks for `client_id`/`client_secret` — no other secrets are stored in this file; they're entered once via the UI's credential form.
- `access_type: "offline"` + the `refresh_token` grant type are what make the standard 401-refresh-retry pattern (Section 4.3) work.

Minimum rules to follow when adapting this for a new provider:

- Folder name (`google`) is the provider code
- Set a clear display `name`
- Set `auth_type: "oauth2"` for a Connect flow
- Add `required_fields` so the UI can show a credential form (Client ID/Secret)
- Add an `icon` pointing at a file inside the same folder (optional)

### 2.2.1 Create a credential in the UI (OAuth provider)

When the Google provider is `auth_type: "oauth2"`, the UI shows the provider’s **required fields** and an OAuth **Connect** button.

![Provider credential screen (required fields + OAuth connect)](assets/provider-credential-required-fields-oauth.png)

### 2.3 `config.json` (recommended split for OAuth settings)

If your deployment supports `config.json` merging (see the main guide), put OAuth technical settings and client credentials there so operators know where secrets live.

**UI rule:** when registering the OAuth app at the vendor, always copy the Redirect/Reply URL **exactly** as shown in your UI for the `google` provider on that environment.

---

## 3) Create the Gmail AI Agent folder

### 3.1 Folder layout

Gmail is a **Function Wrapper agent with a trigger**, so its real folder layout is:

```text
ai-agents/
  gmail/
    plugin.json
    entrypoint.py             ← wires GmailFunctions into FunctionsWrapperAgentTool
    gmail_functions.py         ← the @tool-decorated action methods (the actual logic)
    trigger_entrypoint.py      ← the "On Email Received" trigger implementation
    __init__.py
    gmail.svg
```

There is **no `gmail_agent_tool.py`** and no hand-written `run()` loop — Gmail does not implement its own agent loop; it exposes plain async methods and lets the platform's shared `FunctionsWrapperAgentTool` reasoning loop pick and call the right one.

### 3.2 `plugin.json` (the critical wiring)

This repository already contains a working Gmail plugin manifest:

- `ai-agents/gmail/plugin.json`

Its real shape today:

```json
{
  "plugin_id": "gmail",
  "app_codes": ["gmail"],
  "type": "functions_wrapper",
  "display_name": "Gmail Agent",
  "description": "Gmail AI Agent allows this AI Digital Employee to securely connect with Gmail and manage email communication using natural language. The agent can read, send, reply, draft, label, search, and organize emails, manage attachments, and handle conversations strictly based on the permissions granted to the connected Gmail credentials while maintaining privacy, security, and reliable email operations.",
  "required_fields": [],
  "service_provider_code": "google",
  "icon": "gmail.svg",
  "entrypoint": "entrypoint.py",
  "triggers": [
    {
      "trigger_id": "gmail_email_received",
      "display_name": "On Email Received",
      "description": "Subscribes to Gmail push notifications for new incoming mail via the Gmail users.watch API and a Google Cloud Pub/Sub topic.",
      "entrypoint": "trigger_entrypoint.py",
      "config": {
        "topic_name": "projects/<your-gcp-project>/topics/<your-pubsub-topic>",
        "label_ids": ["INBOX"],
        "label_filter_action": "include"
      }
    }
  ]
}
```

The important parts (copy this pattern for your own agent):

- `type: "functions_wrapper"` — this is what tells the platform to spawn `GmailFunctions` behind the shared reasoning loop instead of expecting a hand-written `run()`.
- `plugin_id` and `app_codes` identify the tool (`gmail`)
- `entrypoint` points to `entrypoint.py`
- `service_provider_code` links the tool to the provider (`google`)
- `icon` (optional) is shown in the UI
- `triggers[0]` declares the `gmail_email_received` trigger, including its own `entrypoint` (`trigger_entrypoint.py`, a **separate file** from the tool's own `entrypoint.py`) and a `config` block: `topic_name` is your Google Cloud Pub/Sub topic that Gmail's `users.watch` API will publish new-mail notifications to — this is static, deployment-specific config, not something an end user fills in.

---

## 4) How Gmail agent code uses dependencies (what to copy for your own agent)

Gmail's logic lives in:

- `ai-agents/gmail/gmail_functions.py` → `class GmailFunctions(ConnectedServiceToolAgent)`

It is **not** a `BaseToolAgent` subclass with a `run()` loop. Instead, it's a plain class where every public, `@tool`-decorated `async def` method is one exposed Gmail action, and `entrypoint.py` wires the whole class into the platform's generic `FunctionsWrapperAgentTool`:

```python
# ai-agents/gmail/entrypoint.py
from .gmail_functions import GmailFunctions
from llm_tools.functions_wrapper_agent_tool import FunctionsWrapperAgentTool

async def create_tool_agent(*, app_code, app_config, llm_provider, token, user_id, company_id=None, agent_id=None):
    agent = FunctionsWrapperAgentTool(
        functions_class=GmailFunctions,
        functions_config={
            "token": token, "user_id": user_id, "company_id": company_id,
            "agent_id": agent_id or "", "app_config": app_config or {},
        },
        llm_provider=llm_provider, token=token, user_id=user_id,
        company_id=company_id, agent_id=agent_id or "", app_config=app_config or {},
    )
    await agent.initialize()
    return agent
```

`FunctionsWrapperAgentTool` spawns `GmailFunctions` behind a lightweight tool-serving subprocess, auto-discovers all 39 of its `@tool`-decorated methods (list/search/read/summarize emails and threads; send/reply/draft/schedule email; manage labels, filters, and attachments; batch-modify/delete; a free-text `action_by_query` catch-all), and runs the shared ReAct reasoning loop over them. **Gmail itself never decides which action to call or how to fill its parameters** — that natural-language-to-tool-call reasoning happens in the outer loop; `GmailFunctions`' job is only to expose well-described, well-typed actions and execute exactly the one it's told to.

A representative action, showing the `@tool` decorator pattern:

```python
@tool(
    description="Search or list email MESSAGES in the mailbox using Gmail search syntax. ...",
    params={
        "gmail_search_query": GMAIL_SEARCH_SYNTAX_REFERENCE,  # the full Gmail search-operator reference, inlined
        "max_results": "Number of messages to return (default 20, max 100).",
    },
)
async def list_emails(self, gmail_search_query: Optional[str] = None, max_results: int = DEFAULT_MAX_RESULTS) -> Dict:
    ...
```

Note how `params["gmail_search_query"]` inlines the *entire* Gmail search-operator syntax reference (`in:`, `from:`, `label:`, `newer_than:`, quoted phrases, boolean grouping, etc.) directly into the tool description. This is deliberate: `GmailFunctions` runs in a stdio subprocess with **no in-process LLM access of its own**, so anything the reasoning loop needs to know to translate a user's request ("emails from my boss last week") into a valid parameter value ("`from:boss newer_than:7d`") has to be spelled out in the tool's own `description`/`params` — you can't rely on a helper LLM call inside the method itself. Write your own `@tool` descriptions with this same rigor for any parameter with non-obvious syntax.

### 4.1 The base class for auth-required agents

`GmailFunctions` extends the connected-service base class used across every provider-authenticated Function Wrapper tool:

- `ConnectedServiceToolAgent`

This gives it a consistent credential surface, resolved automatically from whatever's in `app_config["service_credential"]`:

- `self.access_token` (vendor access token, if connected — resolved regardless of whether the credential payload calls it `access_token`, `accessToken`, or `token`)
- `self.credentials` (the raw connected-account credential payload)
- `self.connected_service_id` (platform connection id)
- `self.refresh_access_token(client=...)` (standard refresh path)

### 4.2 “Not connected” behavior

`GmailFunctions.__init__` logs a warning if `self.access_token` is missing, and every action that hits the network builds its request headers from `self.access_token` regardless — if the token is empty, the vendor call itself fails cleanly (non-200), and the tool returns a normal `{"success": False, ...}` result rather than crashing. The `FunctionsWrapperAgentTool` wrapper around it also has its own guard: if a `connected_service_id` is present but there's genuinely no usable token, it surfaces a "connect this app" prompt to the UI instead of attempting the call at all.

This is the most important production behavior for any auth-required agent: **never let a missing connection produce a stack trace** — always resolve to a clear, user-actionable message.

### 4.3 HTTP client setup and safe retry

Every networked `@tool` method in `GmailFunctions` follows the same shape:

- Builds the request with `Authorization: Bearer <self.access_token>` (via the shared `self._headers()` helper)
- Uses a `httpx.AsyncClient` with an explicit timeout (the instance-level client set up in `initialize()` uses a 30s timeout; the trigger's own short-lived calls use 15s)
- On **401**, refreshes once and retries once:
  - Refresh: `await self.refresh_access_token(client=self._httpx_client)` (tool side) / `await self.refresh_access_token()` (trigger side)
  - Retry the exact same request once after a successful refresh
  - If it's still 401 after that, stop and surface a clean "reconnect" message — never loop

Copy this exact strategy for Microsoft/Google-like OAuth agents.

### 4.4 Tool configuration from `app_config`

`GmailFunctions.__init__` reads a `permissions` setting out of `app_config` (checking `app_config`, `shared_config`, then `personal_config`, and tolerating a JSON-encoded string in any of them), normalizes it to exactly `"full"` or `"read-only"`, and defaults to `"full"` if the value is missing or unrecognized.

Production rule, illustrated exactly by this code: **treat `app_config` as untrusted input** — always `.get(...)`, validate against an allowed set of values, and default safely rather than trusting whatever's stored.

---

## 5) The Gmail trigger ("On Email Received")

Gmail is also a worked example of an AI Agent **trigger** — see `ai-agents/gmail/trigger_entrypoint.py` → `class GmailEmailReceivedTrigger(ConnectedServiceTriggerAgent)`. This is the best real template in the repo for building your own event-driven trigger; walk through it once end-to-end before writing a new one.

### 5.1 `subscribe()` — start watching, and register the callback mapping

When a user wires "On Email Received" into a workflow, `subscribe(trigger_config)`:

1. Reads `topic_name` / `label_ids` / `label_filter_action` from `self.static_config` — this is the **manifest-level** `triggers[0].config` block from `plugin.json` (Section 3.2), not something the caller supplies per-call.
2. Calls Gmail's `users.watch` API with that topic, using the standard 401-refresh-retry pattern.
3. Fetches the connected mailbox's own `emailAddress` (via `users.getProfile`) — this becomes the **`callback_unique_identifier`**, because it's exactly the value Gmail's Pub/Sub push notification will carry back later, letting the platform match an inbound notification to *this* workflow/trigger-node.
4. Calls `self.register_workflow_trigger_callback(callback_unique_identifier=email_address, metadata={"topic_name", "history_id", "expiration"}, expiry_datetime=...)` to persist that mapping.

### 5.2 `execute_trigger()` — resolve the event and advance the cursor

Once the platform has matched an inbound Pub/Sub push to this workflow/node, it calls `execute_trigger(trigger_body, metadata)` with exactly the `metadata` dict `subscribe()` (or a prior firing) last saved. Gmail:

1. Calls `history.list?startHistoryId=<metadata.history_id>` to find what changed since the last known point.
2. Takes the most recent history entry's most recent message.
3. **Re-registers the callback** with `history_id` advanced to the new value (same `callback_unique_identifier`, same `topic_name`/`expiration`) — this is the cursor-advance pattern that stops the next firing from reprocessing the same message.
4. Fetches that message's full detail (`messages.get?format=full`) and returns a **list with one item**, shaped identically to `GmailFunctions.read_email()`'s own output (`message_id`, `thread_id`, `from`, `to`, `subject`, `body`, `attachments`, plus `label_ids` so downstream workflow steps can branch on which label — e.g. `INBOX` — the message has).
5. If there's no new history since the last cursor, it returns an **empty list** rather than an error — "nothing happened yet" is a normal, successful outcome for a trigger.

### 5.3 `handle_trigger_callback()` — the raw webhook receiver

This module-level function owns the actual inbound Pub/Sub push request: it parses the JSON body, pulls out `emailAddress` (the same value `subscribe()` registered as the callback identifier), and always acknowledges with `{"status_code": 200, "response_body": {}, ...}` — Gmail/Pub/Sub only needs any 2xx response to consider the notification delivered; a non-2xx would cause Pub/Sub to redeliver the same notification repeatedly.

**Takeaway for your own trigger:** `subscribe()` starts listening and remembers *how to recognize this mailbox later*; `handle_trigger_callback()` recognizes the inbound event and hands off an identifier; `execute_trigger()` does the actual work and advances its own cursor so it never double-processes an event.

---

## 6) UI workflow (what an operator/user actually does)

### 6.1 Load folders and verify

- Add the `service-providers/google/` folder
- Add the `ai-agents/gmail/` folder
- Restart the running processes (provider catalog loader + agent plugin loader)
- Refresh the UI and verify:
  - Google provider appears in integrations/credentials/services
  - Gmail agent appears in agent tools/apps
  - The "On Email Received" trigger appears as an option when wiring a workflow's starting node

**Where Gmail should appear (example):**

![All AI Agents screen (example)](assets/all-ai-agents.png)

### 6.2 Connect Google once, reuse for Gmail (and other Google tools)

- In the UI, connect the `google` provider once (OAuth Connect)
- Then use/attach the Gmail tool in the UI — and any other Google-family agent (Calendar, Chat, Drive, etc.) reuses this same connection

**Gmail agent Connect entry point (example):**

![Gmail Agent modal with Connect](assets/gmail-connect-modal.png)

**Select Credential appears because Gmail uses a provider (`service_provider_code: "google"`):**

![Gmail Agent modal showing Select Credential](assets/gmail-select-credential.png)

**Credential picker (list existing credentials, or Create a new one):**

![Credential picker (list + create)](assets/credential-picker.png)

**Key idea:** users connect **providers**, not “folders”. Multiple tools can reuse the same provider connection.

### 6.3 Setting up the trigger

Adding "On Email Received" to a workflow calls `subscribe()` under the hood — the user doesn't fill in `topic_name`/`label_ids` themselves (those come from the manifest's static `config`), they just pick the trigger and save. From then on, new mail flowing into the watched labels resumes the workflow automatically each time Gmail's Pub/Sub topic fires.

---

## 7) How to copy this example for Microsoft or GitHub

### Microsoft (OAuth2)

- Create `service-providers/microsoft/` (oauth2 provider)
- Create `ai-agents/<your_microsoft_tool>/` as a `functions_wrapper` (or `mcp_wrapper`/`python_tool_agent` if that fits better) and set `"service_provider_code": "microsoft"`
- In code, extend `ConnectedServiceToolAgent` and follow the same 401→refresh→retry pattern
- If the tool should also react to events (e.g. "On Meeting Created"), add a `triggers` entry plus a `trigger_entrypoint.py` following Gmail's `subscribe()`/`execute_trigger()`/`handle_trigger_callback()` shape

### GitHub (custom token)

- Create `service-providers/github/` with `auth_type: "custom"` and a required field like `access_token`
- Create `ai-agents/<github_tool>/` and set `"service_provider_code": "github"`
- In code, treat `self.credentials` as the source of the token field(s) and fail cleanly if missing

---

## 8) Quick validation checklist (Gmail-style agent)

- [ ] Provider folder exists and appears in the UI
- [ ] Agent folder exists and appears in the UI (`plugin.json.type` matches what the code actually is — `functions_wrapper` for a plain `@tool`-method class like this one)
- [ ] Agent `plugin.json.service_provider_code` matches provider folder name exactly
- [ ] Every `@tool` method's `description`/`params` fully explains any non-obvious syntax (e.g. search query operators) since there's no in-process LLM to fall back on inside the tool subprocess
- [ ] Agent exits cleanly when not connected (no stack traces; a normal `{"success": False, ...}` result)
- [ ] Agent uses timeouts and avoids infinite retries (refresh once, retry once, then stop)
- [ ] Agent never outputs secrets in streamed events or logs
- [ ] If the agent declares a trigger: `subscribe()` registers a `callback_unique_identifier` the vendor's own callback payload will actually contain; `execute_trigger()` advances its cursor via `register_workflow_trigger_callback()` so the same event is never reprocessed; `handle_trigger_callback()` always answers with whatever status the vendor's webhook contract expects
