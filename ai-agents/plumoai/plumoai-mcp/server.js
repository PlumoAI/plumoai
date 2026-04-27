import { FastMCP } from "fastmcp";
import { z } from "zod"; // Or any validation library that supports Standard Schema
import axios from "axios";
import dotenv from "dotenv";
dotenv.config();
function plumoApiV1Base() {
    const raw = process.env.PLUMO_API_BASE_URL?.trim() ?? "https://api.plumoai.com/v1";
    return raw.replace(/\/+$/, "");
}
const OAUTH_ME_TTL_MS = 60000;
const oauthMeCache = new Map();
function buildFetchRecordsGuide(opts) {
    const w = opts?.workspace_name?.trim();
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const s = opts?.status_name?.trim();
    const hasNames = w || p || t || s;
    const projectStep = p
        ? `1. Call **project_list** (no parameters, or with workspace_id to filter). From the response, identify **project_id** of the project named **${p}**. Pass that \`project_id\` as-is into record_list.`
        : `1. Call the **project_list** tool (no parameters, or with workspace_id to filter).\n2. From the response, read \`data\` (array of projects).\n3. Pick the project you need and take its **\`project_id\`** value.\n4. Pass that string **as-is** into record_list's \`project_id\`.`;
    const tableStep = t
        ? `1. Call **project_tables_list**(projectId) with the encrypted project_id from project_list. From the response, identify **table_id** of the table named **${t}**. Pass that \`table_id\` as-is into record_list. Omit to get records from all tables.`
        : `1. Call **project_tables_list**(projectId) with the encrypted project_id from project_list.\n2. From the response, each table has **\`table_id\`** and **\`table_name\`**. Pass **\`table_id\`** as-is into record_list's \`table_id\`.\n3. **Omit** \`table_id\` to get records from all tables.`;
    const statusStep = s
        ? `1. Call **project_table_status_list**(projectId, tableId) or **record_list** once to get records with status info. From the response, identify **status_id** of the status named **${s}**. Use that \`status_id\` in record_list. **Omit** for all statuses.`
        : `1. Call **record_list** once with \`project_id\` (and optional \`table_id\`); each record has **\`status_id\`** and **\`status_name\`**.\n2. Or use **project_table_status_list**(projectId, tableId) if it returns status IDs.\n3. Use one of those **\`status_id\`** values in a later record_list call. **Omit** for all statuses.`;
    const flowProject = p
        ? `**project_list()** → Identify project_id of project **${p}** (encrypted).`
        : `**project_list()** → Choose project, note \`project_id\` (encrypted).`;
    const flowTable = t
        ? `**(Optional) project_tables_list(projectId)** → Identify table_id of table **${t}** (encrypted).`
        : `**(Optional) project_tables_list(projectId)** → Choose table, note \`table_id\` (encrypted).`;
    const flowStatus = s
        ? `**record_list(...)** → If filtering by status, use status_id of status **${s}** from project_table_status_list or a record.`
        : `**record_list(project_id [, table_id] [, status_id] [, isIncludeEmptyFields])** → Get records. Use \`status_id\` from a record for a later filtered call. For narrowest result: \`project_id\` + \`table_id\` + \`status_id\`.`;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${w ? `- **Workspace:** ${w}\n` : ""}${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}${s ? `- **Status:** ${s}\n` : ""}
When you call project_list, project_tables_list, or project_table_status_list, match by the name above to get the correct encrypted ID for use in record_list.

---
`
        : "";
    return `# Action Guide: Fetch Records

**Use the guide_record_list tool** to get this guide whenever you need to use the record_list tool. This guide explains how to fetch records using the **record_list** tool: which parameters to use, where each value comes from, and the recommended flow.

---
${contextSection}## 1. Tool and API

| Item | Value |
|------|--------|
| **Tool name** | \`record_list\` |
| **API** | \`GET /api/v1/records\` |
| **Base URL** | \`${plumoApiV1Base()}/api\` |

---

## 2. Parameters Overview

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`project_id\` | **Yes** | string (encrypted) | Which project's records to fetch. |
| \`table_id\` | No | string (encrypted) | Filter by table/work item type. Omit for all tables. |
| \`status_id\` | No | string (encrypted) | Filter records by workflow status. Omit for all statuses. |
| \`isIncludeEmptyFields\` | No | boolean | If \`true\`, each record includes all field keys (with \`null\` where empty). |
| \`page\` | No | number | Page number for pagination (1-based). |
| \`limit\` | No | number | Page size (how many records per page). |

All IDs are **encrypted strings**. Use them exactly as returned by the APIs; do not use numeric IDs for this endpoint.

---

## 3. How to Resolve Each Parameter

### 3.1 \`project_id\` (required)

**Purpose:** Identifies the project whose records you want.

**Where to get it:**

${projectStep}

---

### 3.2 \`table_id\` (optional)

**Purpose:** Restricts records to one table (work item type), e.g. "Outreach", "Tasks", "Leads".

**Where to get it:**

${tableStep}

---

### 3.3 \`status_id\` (optional)

**Purpose:** Restricts records to a single workflow status (e.g. "New", "In Progress", "Done").

**Where to get it:**

${statusStep}

---

### 3.4 \`isIncludeEmptyFields\` (optional)

**Purpose:** When \`true\`, every record includes all field keys with \`null\` where empty. Set \`true\` or \`false\` (or omit) as needed.

---

### 3.5 \`page\` and \`limit\` (optional)

**Purpose:** Control pagination when listing records.

- \`page\` — which page of results to fetch (1, 2, 3, ...).
- \`limit\` — how many records per page.

Example HTTP call equivalent:

\`GET /v1/records?project_id=<project_id>&page=2&limit=10\`

If you omit these, the API uses its default pagination.

## 4. Filtering: When to Use What

| Goal | Use this filter | Get value from |
|------|-----------------|----------------|
| Records in one project | \`project_id\` (required) | **project_list** → \`data[].project_id\` |
| Records in one workspace | Filter projects first | **project_list**(workspace_id) → then use \`project_id\` |
| Records in one table | \`table_id\` (optional) | **project_tables_list**(projectId) → \`data[].table_id\` |
| Records in one status | \`status_id\` (optional) | **record_list** → any \`data[].status_id\`, or **project_table_status_list** |
| Same fields on every record | \`isIncludeEmptyFields: true\` | N/A |
| Paginate results | \`page\`, \`limit\` | N/A (you decide page/size) |

**Good practice:** Narrow in order — project → table → status. For the narrowest result use \`project_id\` + \`table_id\` + \`status_id\` together.

---

## 5. Combining Filters (Examples)

- **All records in a project:** \`record_list(project_id: "<from project_list>")\`
- **All records in one table:** \`record_list(project_id: "...", table_id: "<from project_tables_list>")\`
- **All records in one status:** \`record_list(project_id: "...", status_id: "<from a record or project_table_status_list>")\`
- **One table and one status:** \`record_list(project_id: "...", table_id: "...", status_id: "...")\`
- **Full list with all field keys:** \`record_list(project_id: "...", isIncludeEmptyFields: true)\` (optionally add \`table_id\` / \`status_id\`).
- **Second page of 10 records:** \`record_list(project_id: "...", page: 2, limit: 10)\`.

---

## 6. Recommended Flow

1. ${flowProject}
2. ${flowTable}
3. ${flowStatus}

---

## 7. Response Shape

Tool returns \`{ success, message, data: records }\`. Each record has keys mapped to field names where possible. \`record_id\`, \`status_id\`, \`table_id\` in the response are encrypted and can be reused. For **detailed_record** use the encrypted \`record_id\`.

---

## 8. Quick Reference

| Parameter | Source | Action |
|-----------|--------|--------|
| \`project_id\` | **project_list** → \`data[].project_id\` | Copy encrypted string as-is. |
| \`table_id\` | **project_tables_list**(projectId) → \`data[].table_id\` | Copy as-is; omit for all tables. |
| \`status_id\` | **record_list** or **project_table_status_list** → \`status_id\` | Copy as-is; omit for all statuses. |
| \`isIncludeEmptyFields\` | N/A | Set \`true\` or \`false\` (or omit). |
| \`page\` | N/A | 1, 2, 3, ... (page number). |
| \`limit\` | N/A | Page size (e.g. 10, 20, 50). |

---

## 9. Common Mistakes to Avoid

- Do **not** use numeric project/table IDs; use **encrypted** \`project_id\` and \`table_id\`.
- Do **not** guess \`status_id\`; get it from a record or project_table_status_list.
- Use the **same** \`project_id\` for project_tables_list and record_list when filtering by table.
- **detailed_record** expects the encrypted \`record_id\` from the list.
- Prefer server-side filtering (\`table_id\`, \`status_id\`) over fetching all and filtering client-side.
`;
}
function buildGuideForNewRecord(opts) {
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const hasNames = p || t;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}

When you call project_list, project_tables_list, project_table_fields, or project_table_status_list, match by these names to get the correct encrypted IDs and required fields for record_create.

---
`
        : "";
    const fields = opts?.fields;
    let fieldsSection = "";
    let mappingSection = "";
    if (fields && Array.isArray(fields) && fields.length > 0) {
        const pickField = (fieldName) => fields.find((f) => (f?.field_name ?? "").toString().trim().toLowerCase() === fieldName.toLowerCase());
        const nameField = pickField("Name");
        const primaryEmailField = pickField("Primary Email");
        const mobileNumberField = pickField("Mobile Number");
        const mappingRows = [];
        // Force the explicit mapping table when we have live fields.
        if (nameField?.field_key)
            mappingRows.push(`"Name" → field_key: "${nameField.field_key}"`);
        if (primaryEmailField?.field_id)
            mappingRows.push(`"Primary Email" → field_id: ${primaryEmailField.field_id}`);
        if (mobileNumberField?.field_id)
            mappingRows.push(`"Mobile Number" → field_id: ${mobileNumberField.field_id}`);
        if (mappingRows.length > 0) {
            mappingSection = `
## 2.2 Explicit mapping table (from project_table_fields)

Map user inputs using project_table_fields:

${mappingRows.map((r) => `- ${r}`).join("\n")}

Use these exact mappings to build \`recordFieldValues\`.\n`;
        }
        const rows = fields
            .map((f) => {
            const name = f.field_name ?? "";
            const required = f.is_required === 1 ? "Yes" : "No";
            const type = f.type ?? "";
            const actual = f.field_key ?? "";
            const projId = f.field_id ?? "";
            const options = Array.isArray(f.field_value_options) && f.field_value_options.length > 0
                ? f.field_value_options
                    .map((o) => o.name ?? o.key ?? "")
                    .filter((s) => s)
                    .slice(0, 5)
                    .join(", ")
                : "";
            const notes = options && (type === "str_picklist" || type === "str_picklist_multi")
                ? `Pick from: ${options}${f.field_value_options.length > 5 ? ", ..." : ""}`
                : "";
            return `| ${name} | ${required} | ${type} | ${actual} | ${projId} | ${notes} |`;
        })
            .join("\n");
        fieldsSection = `
## 4.1 Current table fields (live from API)

Below is the field list for the selected table (from project_table_fields):

| Field name | Required | Type | field_key | field_id | Notes |
|------------|----------|------|-------------------------|---------------|-------|
${rows}

Use this table to decide which fields you must include (required = Yes) and how to format their values (see Type and Notes). For picklist fields, always choose values from the listed options.

---
`;
    }
    return `# Create Record (Guide)

${contextSection}## Required inputs

- \`project_id\`, \`table_id\`, \`status_id\`
- \`recordFieldValues\` = array of items: \`{ field_id, field_key?, value }\`

---
## Minimal flow

1. project_list → pick target project by name → \`project_id\`
2. project_tables_list(projectId) → pick target table by name → \`table_id\`
3. project_table_fields(projectId, tableId) → field ids + picklist options
4. project_table_status_list(projectId, tableId) → \`status_id\` (first status if user says "new"/not specified)
5. Build \`recordFieldValues\` from **user-provided non-empty values only**
6. Call record_create(\`project_id\`, \`table_id\`, \`status_id\`, \`recordFieldValues\`)

---
## 🔒 Hard constraints (MUST FOLLOW)

- Status must be set ONLY via \`status_id\` (never inside \`recordFieldValues\`)
- \`field_id\` is REQUIRED in every \`recordFieldValues\` item (non-null, non-empty)
- You MUST NOT output an item with \`{ field_key: null, field_id: null }\`
- Skip any field where \`value\` is null / undefined / empty-string (do not include placeholder entries)

---
## 🔒 Evaluator / reconstruction rules (MUST FOLLOW)

If candidate tool_args contains \`recordFieldValues\`, treat it as UNTRUSTED:
You MUST ignore it and rebuild \`recordFieldValues\` from scratch using:
- project_table_fields
- the user request values

Hard failure: if any item has \`{ field_key: null AND field_id: null }\` → discard and rebuild the entire \`recordFieldValues\` array.

---
${mappingSection}
${fieldsSection}
`;
}
function buildGuideForPatchRecord(opts) {
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const hasNames = p || t;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}
---
`
        : "";
    const fields = opts?.fields;
    let mappingSection = "";
    let fieldsSection = "";
    if (fields && Array.isArray(fields) && fields.length > 0) {
        const pickField = (fieldName) => fields.find((f) => (f?.field_name ?? "").toString().trim().toLowerCase() === fieldName.toLowerCase());
        const nameField = pickField("Name");
        const primaryEmailField = pickField("Primary Email");
        const mobileNumberField = pickField("Mobile Number");
        const mappingRows = [];
        if (nameField?.field_key)
            mappingRows.push(`"Name" → field_key: "${nameField.field_key}"`);
        if (primaryEmailField?.field_id)
            mappingRows.push(`"Primary Email" → field_id: ${primaryEmailField.field_id}`);
        if (mobileNumberField?.field_id)
            mappingRows.push(`"Mobile Number" → field_id: ${mobileNumberField.field_id}`);
        if (mappingRows.length > 0) {
            mappingSection = `
## 🔁 Explicit mapping table (from project_table_fields)
Map user inputs using project_table_fields:

${mappingRows.map((r) => `- ${r}`).join("\n")}

Use these mappings to build \`recordFieldValues\`.
---`;
        }
        const rows = fields
            .map((f) => {
            const name = f.field_name ?? "";
            const required = f.is_required === 1 ? "Yes" : "No";
            const type = f.type ?? "";
            const fieldKey = f.field_key ?? "";
            const fieldId = f.field_id ?? "";
            const options = Array.isArray(f.field_value_options) && f.field_value_options.length > 0
                ? f.field_value_options
                    .map((o) => o.name ?? o.key ?? "")
                    .filter((s) => s)
                    .slice(0, 5)
                    .join(", ")
                : "";
            const notes = options && (type === "str_picklist" || type === "str_picklist_multi")
                ? `Pick from: ${options}${f.field_value_options.length > 5 ? ", ..." : ""}`
                : "";
            return `| ${name} | ${required} | ${type} | ${fieldKey} | ${fieldId} | ${notes} |`;
        })
            .join("\n");
        fieldsSection = `
## 4.1 Current table fields (live from API)
| Field name | Required | Type | field_key | field_id | Notes |
|------------|----------|------|-----------|----------|-------|
${rows}
---`;
    }
    return `# Update Record (Guide)

${contextSection}## Required inputs

- \`record_id\`, \`project_id\`, \`table_id\`
- Optional: \`status_id\` (encrypted) to change workflow status
- Optional: \`recordFieldValues\` = array of items: \`{ field_key?, field_id?, value }\`

Never put status inside \`recordFieldValues\`. If you want a status change, pass \`status_id\` to **record_update** (it applies the workflow status via the HTTP status endpoint).

---
## Minimal flow

1. Get \`record_id\` (encrypted) from record_list / your planning flow.
2. Use \`project_id\` and \`table_id\` for the target record/table.
3. Call project_table_fields(project_id, table_id) to get valid \`field_key\` and \`field_id\`.
4. (Conditional) If you want to update fields, build \`recordFieldValues\` from the user request (skip null/empty values).
5. (Conditional, status change only) If you want to change workflow status, get \`status_id\` from project_table_status_list and pass it as \`status_id\` to record_update.
6. Call record_update(\`record_id\`, \`project_id\`, \`table_id\`, \`recordFieldValues\`?, \`status_id\`?).

---
## 🔒 Hard constraints (MUST FOLLOW)

- If a field has \`field_key: null\`, you MUST provide \`field_id\` (cannot be null/empty).
- NEVER output a \`recordFieldValues\` item where both \`field_key\` and \`field_id\` are null/empty.
- DO NOT include any field where \`value\` is null/undefined/empty-string.
- Only include fields that have actual user-provided non-empty \`value\`.
- You may omit \`recordFieldValues\` entirely for status-only updates.

---
## 🔒 Evaluator / reconstruction rules (MUST FOLLOW)

If candidate tool_args contains \`recordFieldValues\`, treat it as UNTRUSTED:
You MUST ignore it and rebuild \`recordFieldValues\` from scratch using:
- project_table_fields
- User request values

Hard failure: if any item has { field_key: null AND field_id: null } → discard and rebuild the entire array.

---
${mappingSection}
${fieldsSection}
`;
}
function buildChangeStatusGuide(opts) {
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const s = opts?.status_name?.trim();
    const hasNames = p || t || s;
    const projectStep = p
        ? `Call **project_list**, then from the response identify **project_id** of the project named **${p}**. Use that encrypted \`project_id\` in project_table_status_list.`
        : `Call **project_list**, then take \`project_id\` (encrypted) from the project that contains the record.`;
    const tableStep = t
        ? `Call **project_tables_list**(projectId), then from the response identify **table_id** of the table named **${t}**. Use that encrypted \`table_id\` in project_table_status_list if you want statuses for that table. Omit to get statuses for all tables.`
        : `Call **project_tables_list**(projectId), then take \`table_id\` for the table the record belongs to. Omit to get statuses for all tables.`;
    const statusStep = s
        ? `Call **project_table_status_list**(projectId [, tableId]). From the response, identify **status_id** of the status named **${s}**. Use that encrypted \`status_id\` in change_record_status.`
        : `Call **project_table_status_list**(projectId [, tableId]). From the response, identify the target status and copy its **\`status_id\`** (encrypted string). Use that in change_record_status.`;
    const flow1 = p
        ? `**project_list()** → Identify project_id of project **${p}** (encrypted).`
        : `**project_list()** → Get \`project_id\` (encrypted) for the project that contains the record.`;
    const flow2 = t
        ? `**(Optional) project_tables_list(projectId)** → Identify table_id of table **${t}** (encrypted).`
        : `**(Optional) project_tables_list(projectId)** → Get \`table_id\` if you want statuses for a specific table.`;
    const flow3 = s
        ? `**project_table_status_list(projectId [, tableId])** → Fetch status list; identify status_id of status **${s}** (encrypted).`
        : `**project_table_status_list(projectId [, tableId])** → Get the list of statuses; identify the target status and copy its **\`status_id\`** (encrypted string).`;
    const flow4 = `**change_record_status(record_id, status_id)** → Pass the **encrypted** \`record_id\` from record_list and the **encrypted** \`status_id\` from step 3.`;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}${s ? `- **Status:** ${s}\n` : ""}
Fetch the relevant list (project_list, project_tables_list, project_table_status_list), then identify the ID for the entity named above to use in the next step.

---
`
        : "";
    return `# Action Guide: Change Record Status

**Use the guide_change_record_status tool** to get this guide whenever you need to change a record's workflow status. This guide explains: first fetch the project table status list to identify the correct \`status_id\`, then call **change_record_status** with the record ID and that \`status_id\`.

---
${contextSection}## 1. Overview

| Item | Value |
|------|--------|
| **Tool name** | \`change_record_status\` |
| **API** | \`PATCH /v1/records/{record_id}/status\` |
| **Base URL** | \`${plumoApiV1Base()}\` |

To change a record's status you need:
- **record_id** — encrypted string ID of the record (from record_list).
- **status_id** — encrypted string for the target status (from **project_table_status_list**).

---

## 2. Step 1: Fetch Project Table Status List

**Before** calling change_record_status, you must get the list of valid statuses and their encrypted \`status_id\` values.

### Tool: **project_table_status_list**

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`projectId\` | **Yes** | string (encrypted) | Project ID from **project_list**. |
| \`tableId\` | No | string (encrypted) | Table ID from **project_tables_list**. Omit or \`-1\` for all tables. |

**Where to get projectId:** ${projectStep}

**Where to get tableId (optional):** ${tableStep}

**Response:** List of statuses. Each status has an encrypted \`status_id\` (and usually \`status_name\` or similar). ${s ? `Identify the status named **${s}** and note its **\`status_id\`**` : "Identify the status you want and note its **\`status_id\`**"} — you will pass this exact string to change_record_status.

---

## 3. Step 2: Call change_record_status

After you have the target \`status_id\` from project_table_status_list (or from a record's current \`status_id\` in record_list), call:

### Tool: **change_record_status**

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`record_id\` | **Yes** | string (encrypted) | Encrypted record ID. Use \`record_id\` from **record_list** as-is. |
| \`status_id\` | **Yes** | string (encrypted) | Target status. Use the **encrypted** \`status_id\` from **project_table_status_list** (or from another record in the same table). |

**Example:** \`change_record_status(record_id: "<from record_list>", status_id: "<from project_table_status_list>")\`

---

## 4. Recommended Flow

1. ${flow1}
2. ${flow2}
3. ${flow3}
4. ${flow4}

---

## 5. Quick Reference

| What you need | Where to get it |
|---------------|-----------------|
| \`record_id\` (encrypted) | **record_list** — use \`record_id\` from the record as-is. |
| \`status_id\` (encrypted) | **project_table_status_list**(projectId, tableId) — ${s ? `identify status_id of status **${s}**` : "copy \`status_id\` for the desired status"}. |

---

## 6. Common Mistakes to Avoid

- Do **not** use numeric record ID — use the **encrypted** \`record_id\` from **record_list** as-is.
- Do **not** guess or invent \`status_id\` — always get it from **project_table_status_list** (or from another record's \`status_id\` in the same table).
- **First** fetch status list, **then** call change_record_status.
`;
}
function withBearerPrefix(value) {
    const t = value.trim();
    if (!t)
        return t;
    return /^bearer\s+/i.test(t) ? t : `Bearer ${t}`;
}
/** HTTP: Authorization header. stdio: no request — use PLUMO_MCP_AUTHORIZATION or PLUMO_ACCESS_TOKEN (Cursor mcp.json); same shape as the header, optional Bearer prefix. */
function readPlumoAuthorization(request) {
    if (request?.headers) {
        const value = request.headers.authorization;
        if (typeof value === "string" && value.length > 0)
            return value;
        if (Array.isArray(value) && value[0])
            return value[0];
    }
    const fromMcp = process.env.PLUMO_MCP_AUTHORIZATION?.trim();
    if (fromMcp)
        return withBearerPrefix(fromMcp);
    const fromAccess = process.env.PLUMO_ACCESS_TOKEN?.trim();
    if (fromAccess)
        return withBearerPrefix(fromAccess);
    return undefined;
}
const server = new FastMCP({
    name: "PlumoAI Project MCP Server",
    version: "1.0.0",
    oauth: {
        enabled: true,
        authorizationServer: {
            issuer: "https://auth.plumoai.com",
            authorizationEndpoint: "https://api.plumoai.com/Auth/oauth/authorize",
            tokenEndpoint: "https://api.plumoai.com/Auth/oauth/token",
            registrationEndpoint: "https://api.plumoai.com/Auth/oauth/register",
            responseTypesSupported: ["code"],
            responseModesSupported: ["query"],
            grantTypesSupported: ["authorization_code", "refresh_token"],
            tokenEndpointAuthMethodsSupported: ["client_secret_basic", "client_secret_post", "none"],
            revocationEndpoint: "https://api.plumoai.com/Auth/oauth/token",
            codeChallengeMethodsSupported: ["plain", "S256"]
        },
        protectedResource: {
            resource: "https://mcp.plumoai.com",
            authorizationServers: ["https://api.plumoai.com/Auth/"],
            resource_name: "PlumoAI MCP (Beta)",
            resource_documentation: "https://developers.plumoai.com/docs/mcp",
            authorization_servers: ["https://mcp.plumoai.com"],
            bearer_methods_supported: ["header"]
        }
    },
    authenticate: async (request) => {
        var authHeader = readPlumoAuthorization(request);
        var companyId = null;
        if ((authHeader ?? "").split("---CompanyID---").length > 1) {
            companyId = (authHeader ?? "").split("---CompanyID---")[1];
            authHeader = (authHeader ?? "").split("---CompanyID---")[0];
        }
        if (!authHeader?.startsWith("Bearer ")) {
            throw new Response(null, {
                status: 401,
                statusText: JSON.stringify({ "error": "invalid_token", "error_description": "Missing or invalid access token" }),
            });
        }
        try {
            const cacheKey = `${authHeader}`;
            const now = Date.now();
            const cached = oauthMeCache.get(cacheKey);
            let me = null;
            if (cached && cached.expiresAt > now) {
                me = cached.value;
            }
            else {
                var response = await axios.get(`https://api.plumoai.com/Auth/oauth/me`, {
                    timeout: 1500,
                    headers: {
                        Authorization: authHeader,
                    },
                });
                if (response.data?.error) {
                    throw new Response(null, {
                        status: 401,
                        statusText: "Invalid OAuth token",
                    });
                }
                me = {
                    companyIds: Array.isArray(response.data?.data?.companyIds) ? response.data.data.companyIds : [],
                    userId: response.data?.data?.userId,
                };
                oauthMeCache.set(cacheKey, { expiresAt: now + OAUTH_ME_TTL_MS, value: me });
            }
            if (companyId == null) {
                companyId = me.companyIds.find((x) => x != null);
            }
            if (companyId == null) {
                throw new Response(null, {
                    status: 401,
                    statusText: "Missing Company ID",
                });
            }
            if (me.companyIds.map((x) => x.toString()).indexOf(companyId.toString()) < 0) {
                throw new Response(null, {
                    status: 401,
                    statusText: "Invalid Company ID",
                });
            }
            return { user_access_token: authHeader.slice(7), expires_in: 900, companyId: String(companyId), userId: me.userId };
        }
        catch (error) {
            throw new Response(null, {
                status: 401,
                statusText: error.message ?? "Invalid OAuth token",
            });
        }
    },
});
server.addTool({
    name: "guide_record_list",
    description: "Returns the full markdown guide for **record_list**: parameters, resolving encrypted IDs from project_list, project_tables_list, and project_table_status_list, pagination, and common mistakes.",
    parameters: z.object({
        workspace_name: z.string().optional().describe("Optional. User-mentioned workspace name to tailor the guide."),
        project_name: z.string().optional().describe("Optional. User-mentioned project name to tailor the guide."),
        table_name: z.string().optional().describe("Optional. User-mentioned table name to tailor the guide."),
        status_name: z.string().optional().describe("Optional. User-mentioned status name to tailor the guide."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args) => {
        return {
            type: "text",
            text: buildFetchRecordsGuide({
                workspace_name: args.workspace_name,
                project_name: args.project_name,
                table_name: args.table_name,
                status_name: args.status_name,
            }),
        };
    },
});
server.addTool({
    name: "guide_create_record",
    description: "Returns the full markdown guide for **record_create**: recordFieldValues shape, hard constraints, and optional live field metadata from project_table_fields.",
    parameters: z.object({
        project_name: z.string().optional(),
        table_name: z.string().optional(),
        fields: z.array(z.any()).optional().describe("Optional. Field objects from project_table_fields to inject a live field table into the guide."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args) => {
        return {
            type: "text",
            text: buildGuideForNewRecord({
                project_name: args.project_name,
                table_name: args.table_name,
                fields: args.fields,
            }),
        };
    },
});
server.addTool({
    name: "guide_update_record",
    description: "Returns the full markdown guide for **record_update**: field vs status updates, recordFieldValues rules, and optional live field metadata.",
    parameters: z.object({
        project_name: z.string().optional(),
        table_name: z.string().optional(),
        fields: z.array(z.any()).optional().describe("Optional. Field objects from project_table_fields."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args) => {
        return {
            type: "text",
            text: buildGuideForPatchRecord({
                project_name: args.project_name,
                table_name: args.table_name,
                fields: args.fields,
            }),
        };
    },
});
server.addTool({
    name: "guide_change_record_status",
    description: "Returns the full markdown guide for change_record_status: resolving encrypted status_id from project_table_status_list and using it with record_id from record_list.",
    parameters: z.object({
        project_name: z.string().optional(),
        table_name: z.string().optional(),
        status_name: z.string().optional(),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args) => {
        return {
            type: "text",
            text: buildChangeStatusGuide({
                project_name: args.project_name,
                table_name: args.table_name,
                status_name: args.status_name,
            }),
        };
    },
});
server.addTool({
    name: "project_list",
    description: "Fetch all projects for the authenticated user. Returns workspace, workspace_id, project_description, project_id (encrypted), project_name, template_name. Optionally filter by workspace_id (encrypted). Use project_id (encrypted) as-is when calling project_tables_list or record_list. (Note: Also fetch sprints for Scrum projects after getting a specific project)\n\n" +
        "Returns workspace, workspace_id (ENCRYPTED string), project_description, project_id (ENCRYPTED string), project_name, template_name, and a numeric project fid (integer).\n\n" +
        "ID USAGE GUIDE:\n" +
        "- project_id (ENCRYPTED string) → use with: record_list, project_tables_list, project_table_fields, project_table_status_list, record_create, record_update, change_record_status\n" +
        "- numeric fid (integer) → Scrum/sprint utilities only",
    parameters: z.object({
        workspace_id: z.string().optional().describe("Optional. Filter projects by workspace. Use workspace_id (encrypted) from a previous project_list response."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/projects`, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            let projects = Array.isArray(response.data?.data) ? response.data.data : (Array.isArray(response.data) ? response.data : []);
            if (args.workspace_id?.trim()) {
                const wid = args.workspace_id.trim();
                projects = projects.filter((p) => p.workspace_id === wid);
            }
            const payload = response.data?.success !== undefined
                ? { ...response.data, data: projects }
                : projects;
            return {
                type: "text",
                text: JSON.stringify(payload),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({ error: error.response?.data ?? error.message ?? "Unknown error occurred" }),
            };
        }
    },
});
server.addTool({
    name: "record_list",
    description: "STEP 2 OF 2 — Fetches records for a project (optionally filtered by table/status/record) with pagination.\n" +
        "REQUIRED CALL ORDER: (1) project_list → project_id (ENCRYPTED string), (2) record_list(project_id [, table_id] [, status_id] [, recordId]).\n\n" +
        "RETURN VALUE NOTE: Each record contains TWO id fields:\n" +
        "- record_id (ENCRYPTED string) → pass to record_update or change_record_status\n" +
        "- id (integer / numeric) → present, but **not used by detailed_record** in this server.\n\n" +
        "Use `record_id` (encrypted) with detailed_record.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        table_id: z.string().optional().describe("Optional. ENCRYPTED table_id string from project_tables_list(projectId). Omit for all tables."),
        status_id: z.string().optional().describe("Optional. ENCRYPTED status_id string from project_table_status_list or prior record_list. Omit for all statuses."),
        recordId: z.string().optional().describe("Optional. ENCRYPTED record_id string (from record_list) to fetch/filter a specific record."),
        isIncludeEmptyFields: z.boolean().optional().describe("Optional. When true, every record includes all field keys (with null where there is no value)."),
        page: z.number().int().positive().optional().describe("Optional. Page number for pagination (1-based). If omitted, server default is used."),
        limit: z.number().int().positive().optional().describe("Optional. Page size for pagination. If omitted, server default is used."),
        select_fields: z
            .array(z.string().min(1))
            .optional()
            .describe("Optional. Reduce payload by returning ONLY these output fields. Field names must match the post-transformation keys (e.g. 'record_id', 'Name', 'Primary Email'). `record_id` is always included for usability."),
        omit_fields: z
            .array(z.string().min(1))
            .optional()
            .describe("Optional. Reduce payload by removing these output fields. Field names must match the post-transformation keys. `record_id` is never omitted."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const params = {
                project_id: args.project_id,
            };
            if (args.status_id != null)
                params.status_id = args.status_id;
            if (args.table_id != null)
                params.table_id = args.table_id;
            if (args.recordId != null)
                params.recordId = args.recordId;
            if (args.isIncludeEmptyFields != null)
                params.isIncludeEmptyFields = String(args.isIncludeEmptyFields);
            if (args.page != null)
                params.page = String(args.page);
            if (args.limit != null)
                params.limit = String(args.limit);
            const queryString = new URLSearchParams(params).toString();
            const response = await axios.get(`${plumoApiV1Base()}/records?${queryString}`, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            const raw = response.data?.data ?? response.data;
            const fields = raw?.fields ?? [];
            const records = raw?.records ?? [];
            const keyToFieldName = {};
            for (const f of fields) {
                const name = f.field_name ?? "";
                if (f.field_id)
                    keyToFieldName[f.field_id] = name;
                if (f.field_key)
                    keyToFieldName[f.field_key] = name;
            }
            const transformedRecords = records.map((rec) => {
                const out = {};
                for (const key of Object.keys(rec)) {
                    const newKey = keyToFieldName[key] ?? key;
                    out[newKey] = rec[key];
                }
                return out;
            });
            const normalizeFieldKey = (s) => s.trim();
            const selectSet = Array.isArray(args.select_fields) && args.select_fields.length > 0
                ? new Set(args.select_fields.map(normalizeFieldKey).filter((k) => k.length > 0))
                : null;
            const omitSet = Array.isArray(args.omit_fields) && args.omit_fields.length > 0
                ? new Set(args.omit_fields.map(normalizeFieldKey).filter((k) => k.length > 0))
                : null;
            const finalRecords = selectSet || omitSet
                ? transformedRecords.map((rec) => {
                    const alwaysKeep = new Set(["record_id"]);
                    const out = {};
                    if (selectSet) {
                        for (const key of alwaysKeep) {
                            if (Object.prototype.hasOwnProperty.call(rec, key))
                                out[key] = rec[key];
                        }
                        for (const key of selectSet) {
                            if (alwaysKeep.has(key))
                                continue;
                            if (Object.prototype.hasOwnProperty.call(rec, key))
                                out[key] = rec[key];
                        }
                        return out;
                    }
                    // omitSet only
                    for (const key of Object.keys(rec)) {
                        if (alwaysKeep.has(key)) {
                            out[key] = rec[key];
                            continue;
                        }
                        if (omitSet?.has(key))
                            continue;
                        out[key] = rec[key];
                    }
                    return out;
                })
                : transformedRecords;
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "Records retrieved successfully",
                    data: finalRecords,
                }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({ error: error.response?.data ?? error.message ?? "Unknown error occurred" }),
            };
        }
    },
});
server.addTool({
    name: "project_tables_list",
    description: "UTILITY — Lists project tables (work item types). ID TYPES: projectId is ENCRYPTED string. Returns ENCRYPTED table_id values. Use: (1) project_list → get ENCRYPTED project_id, (2) project_tables_list(projectId) → get ENCRYPTED table_id. Pass these encrypted strings as-is into downstream tools (e.g. record_list, record_create, record_update).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z
            .string()
            .describe("Required. ENCRYPTED project_id string from project_list. Pass as-is (NOT numeric)."),
    }),
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/projects/${encodeURIComponent(args.projectId)}/tables`, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            return {
                type: "text",
                text: JSON.stringify(response.data),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "project_table_fields",
    description: "UTILITY — Lists fields for a project table. REQUIRED BEFORE record_create / record_update when you need field IDs. ID TYPES: projectId ENCRYPTED string, tableId ENCRYPTED string. Returns ENCRYPTED field_id (proj_field_id) plus field_key (physical/system key when present), type, is_required, and value options for picklists. Use these results to build recordFieldValues.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        tableId: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
    }),
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/projects/${encodeURIComponent(args.projectId)}/tables/${encodeURIComponent(args.tableId)}/fields`, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            const rawData = response.data?.data ?? response.data;
            const items = Array.isArray(rawData) ? rawData : [];
            const transformed = items.map((item) => {
                const field = {
                    field_id: item.proj_field_id,
                    field_name: item.field_name,
                    field_description: item.field_description ?? "",
                    is_required: item.is_required ?? 0,
                    type: item.type,
                    field_key: item.record_actual_fieldname ?? null,
                    field_value_options: [],
                };
                if (item.field_value_options && Array.isArray(item.field_value_options)) {
                    field.field_value_options = item.field_value_options;
                }
                else if (item.field_value_list != null) {
                    try {
                        const parsed = typeof item.field_value_list === "string"
                            ? JSON.parse(item.field_value_list)
                            : item.field_value_list;
                        field.field_value_options = Array.isArray(parsed)
                            ? parsed.map((o) => ({ key: o.key ?? o.name, name: o.name ?? o.key }))
                            : [];
                    }
                    catch {
                        field.field_value_options = [];
                    }
                }
                return field;
            });
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "Project table fields retrieved successfully",
                    data: transformed,
                }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "record_create",
    description: "STEP 5 OF 5 — Creates a new record. REQUIRED CALL ORDER: (1) project_list → project_id, (2) project_tables_list(projectId) → table_id, (3) project_table_status_list(projectId [, tableId]) → status_id, (4) project_table_fields(projectId, tableId) → field_id, (5) record_create(project_id, table_id, status_id, recordFieldValues). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects. Each object sets one field. Only include fields you want to set.\n" +
        "Fields where is_required=true (from project_table_fields) MUST be included.\n\n" +
        "FIELD TYPE → VALUE FORMAT:\n" +
        "  text / string     → \"My task title\"\n" +
        "  text_multiline    → \"Multi-line\\ndescription\"\n" +
        "  number            → 42\n" +
        "  date              → \"2026-04-20\"  (ISO 8601)\n" +
        "  dropdown/select   → \"high\"  (must match a field_value_options value exactly)\n" +
        "  boolean           → true\n" +
        "  user              → \"usr_abc123\"\n\n" +
        "WORKED EXAMPLE:\n" +
        "[\n" +
        "  { \"field_id\": \"ZmllbGQx\", \"field_key\": \"title\",     \"value\": \"Fix login bug\" },\n" +
        "  { \"field_id\": \"ZmllbGQy\", \"field_key\": \"priority\",  \"value\": \"high\" },\n" +
        "  { \"field_id\": \"ZmllbGQz\", \"field_key\": \"due_date\",  \"value\": \"2026-04-20\" }\n" +
        "]\n\n" +
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by project_table_fields. Do not invent values.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from project_table_status_list(projectId [, tableId]). Pass as-is."),
        recordFieldValues: z
            .array(z.object({
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from project_table_fields. MUST NOT be null/empty."),
            field_key: z.string().nullable().optional().describe("Optional. Physical/system field key like 'title' (only when available)."),
            value: z.any().describe("Field value to set for this record field."),
        }))
            .nonempty(),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const rawRecordFieldValues = (args.recordFieldValues ?? []);
            const recordFieldValues = rawRecordFieldValues.filter((it) => {
                const v = it?.value;
                if (v === null || v === undefined)
                    return false;
                if (typeof v === "string" && v.trim().length === 0)
                    return false;
                return true;
            });
            const invalidFieldIdItems = recordFieldValues.filter((it) => {
                const id = (it?.field_id ?? null);
                const hasId = typeof id === "string" ? id.trim().length > 0 : false;
                return !hasId;
            });
            if (invalidFieldIdItems.length > 0) {
                return {
                    type: "text",
                    text: JSON.stringify({
                        success: false,
                        message: "Invalid recordFieldValues: field_id is required for every item and must not be null/empty. Rebuild recordFieldValues using project_table_fields.",
                        invalid_count: invalidFieldIdItems.length,
                    }),
                };
            }
            const payload = {
                project_id: args.project_id,
                table_id: args.table_id,
                status_id: args.status_id,
                recordFieldValues,
            };
            const response = await axios.post(`${plumoApiV1Base()}/records`, payload, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    "Content-Type": "application/json",
                },
            });
            const raw = response.data?.data ?? {};
            const fields = raw?.fields ?? [];
            const record = raw?.record ?? {};
            const keyToFieldName = {};
            for (const f of fields) {
                const name = f.field_name ?? "";
                if (f.field_id)
                    keyToFieldName[f.field_id] = name;
                if (f.field_key)
                    keyToFieldName[f.field_key] = name;
            }
            const transformedRecord = {};
            for (const key of Object.keys(record)) {
                const newKey = keyToFieldName[key] ?? key;
                transformedRecord[newKey] = record[key];
            }
            return {
                type: "text",
                text: JSON.stringify({
                    success: response.data?.success ?? true,
                    message: response.data?.message ?? "Record created successfully",
                    data: transformedRecord,
                }),
            };
        }
        catch (error) {
            const status_code = error?.response?.status ?? null;
            const status_text = error?.response?.statusText ?? null;
            const api_error = error?.response?.data ?? null;
            return {
                type: "text",
                text: JSON.stringify({
                    success: false,
                    status_code,
                    status_text,
                    error: api_error ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "detailed_record",
    description: "STEP 2 OF 2 — Fetches full details of a single record: all fields, custom fields, comments, attachments, checklist items, linked records, and user info.\n" +
        "ID TYPE: record_id is an ENCRYPTED string (from record_list), NOT a numeric id.\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) record_list(project_id) → locate the record by name/title, note its encrypted record_id string.\n" +
        "(2) detailed_record(record_id=that encrypted string).",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/records/${encodeURIComponent(args.record_id)}`, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            const raw = response.data?.data ?? response.data ?? {};
            const fields = raw?.fields ?? [];
            const records = [raw?.record].filter((it) => it !== null && it !== undefined);
            const keyToFieldName = {};
            for (const f of fields) {
                const name = f.field_name ?? "";
                if (f.field_id)
                    keyToFieldName[f.field_id] = name;
                if (f.field_key)
                    keyToFieldName[f.field_key] = name;
            }
            const transformedRecords = records.map((rec) => {
                const out = {};
                for (const key of Object.keys(rec)) {
                    const newKey = keyToFieldName[key] ?? key;
                    out[newKey] = rec[key];
                }
                return out;
            });
            const finalRecords = transformedRecords;
            if (finalRecords.length === 0) {
                return {
                    type: "text",
                    text: JSON.stringify({
                        success: false,
                        message: "No records found",
                    }),
                };
            }
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "Records retrieved successfully",
                    data: finalRecords[0],
                }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "record_update",
    description: "STEP 4 OF 4 — Updates a record (fields and/or status). ID TYPES: record_id/project_id/table_id/status_id/field_id are ENCRYPTED strings — pass as-is.\n\n" +
        "THREE USAGE MODES — choose the one that fits:\n\n" +
        "Mode 1 — Fields only (do not pass status_id):\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n" +
        "  status_id: omit or null\n\n" +
        "Mode 2 — Status only (do not pass field values):\n" +
        "  status_id: \"ENCRYPTED_status_id from project_table_status_list\"\n" +
        "  recordFieldValues: null\n\n" +
        "Mode 3 — Fields + status in one call:\n" +
        "  status_id: \"ENCRYPTED_status_id\"\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n\n" +
        "PREFERRED OVER change_record_status for all status changes.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
        project_id: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
        status_id: z
            .string()
            .nullable()
            .optional()
            .describe("Optional. ENCRYPTED status_id string from project_table_status_list. Pass as-is for status changes."),
        recordFieldValues: z
            .array(z.object({
            field_key: z.string().nullable().optional().describe("Optional. Physical/system field key like 'title'."),
            field_id: z
                .string()
                .nullable()
                .optional()
                .describe("Optional. ENCRYPTED field_id string from project_table_fields (required when field_key is null/empty)."),
            value: z.any().describe("Field value to update."),
        }))
            .nullable()
            .optional()
            .describe("Optional. Field updates. Can be null/empty for status-only updates. See tool description for examples."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const rawRecordFieldValues = (args.recordFieldValues ?? []);
            // Skip null/undefined/empty-string values to avoid placeholder fields.
            const recordFieldValues = rawRecordFieldValues.filter((it) => {
                const v = it?.value;
                if (v === null || v === undefined)
                    return false;
                if (typeof v === "string" && v.trim().length === 0)
                    return false;
                return true;
            });
            const invalidItems = recordFieldValues.filter((it) => {
                const k = it?.field_key ?? null;
                const id = it?.field_id ?? null;
                const hasKey = typeof k === "string" && k.trim().length > 0;
                const hasId = typeof id === "string" && id.trim().length > 0;
                // Must not send an item without any identifier.
                if (!hasKey && !hasId)
                    return true;
                // If field_key is absent/null, field_id must be present.
                if (!hasKey)
                    return !hasId;
                return false;
            });
            if (invalidItems.length > 0) {
                return {
                    type: "text",
                    text: JSON.stringify({
                        success: false,
                        message: "Invalid recordFieldValues: each item must include either a valid field_key or a valid field_id; if field_key is null/empty then field_id must be provided.",
                        invalid_count: invalidItems.length,
                    }),
                };
            }
            const hasFieldUpdates = recordFieldValues.length > 0;
            const statusId = typeof args.status_id === "string" && args.status_id.trim().length > 0 ? args.status_id.trim() : null;
            // No field updates and no status update: return a safe no-op success.
            if (!hasFieldUpdates && !statusId) {
                return {
                    type: "text",
                    text: JSON.stringify({
                        success: true,
                        message: "No updates provided (recordFieldValues empty and status_id not set).",
                        data: {},
                    }),
                };
            }
            let lastTransformedRecord = {};
            let lastMessage = "Record updated successfully";
            let lastSuccess = true;
            // 1) Update fields (if provided)
            if (hasFieldUpdates) {
                const payload = {
                    record_id: args.record_id,
                    project_id: args.project_id,
                    table_id: args.table_id,
                    recordFieldValues,
                };
                const response = await axios.post(`${plumoApiV1Base()}/records/update`, payload, {
                    headers: {
                        Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                        "Content-Type": "application/json",
                    },
                });
                const raw = response.data?.data ?? response.data ?? {};
                const fields = raw?.fields ?? [];
                const record = raw?.record ?? raw?.data?.record ?? {};
                const keyToFieldName = {};
                for (const f of fields) {
                    const name = f.field_name ?? "";
                    if (f.field_id)
                        keyToFieldName[f.field_id] = name;
                    if (f.field_key)
                        keyToFieldName[f.field_key] = name;
                }
                const transformedRecord = {};
                for (const key of Object.keys(record)) {
                    const newKey = keyToFieldName[key] ?? key;
                    transformedRecord[newKey] = record[key];
                }
                lastTransformedRecord = transformedRecord;
                lastMessage = response.data?.message ?? "Record updated successfully";
                lastSuccess = response.data?.success ?? true;
            }
            // 2) Update status (if provided)
            if (statusId) {
                const statusResponse = await axios.patch(`${plumoApiV1Base()}/records/${encodeURIComponent(args.record_id)}/status`, { status_id: statusId }, {
                    headers: {
                        Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                        "Content-Type": "application/json",
                    },
                });
                const raw = statusResponse.data?.data ?? statusResponse.data ?? {};
                const fields = raw?.fields ?? [];
                const record = raw?.record ?? raw?.data?.record ?? {};
                const keyToFieldName = {};
                for (const f of fields) {
                    const name = f.field_name ?? "";
                    if (f.field_id)
                        keyToFieldName[f.field_id] = name;
                    if (f.field_key)
                        keyToFieldName[f.field_key] = name;
                }
                const transformedRecord = {};
                for (const key of Object.keys(record)) {
                    const newKey = keyToFieldName[key] ?? key;
                    transformedRecord[newKey] = record[key];
                }
                // If the status API returned a usable record, prefer that. Otherwise keep field-update output.
                if (Object.keys(transformedRecord).length > 0) {
                    lastTransformedRecord = transformedRecord;
                }
                lastMessage = statusResponse.data?.message ?? lastMessage;
                lastSuccess = statusResponse.data?.success ?? lastSuccess;
            }
            return {
                type: "text",
                text: JSON.stringify({
                    success: lastSuccess,
                    message: lastMessage,
                    data: lastTransformedRecord,
                }),
            };
        }
        catch (error) {
            const status_code = error?.response?.status ?? null;
            const status_text = error?.response?.statusText ?? null;
            const api_error = error?.response?.data ?? null;
            return {
                type: "text",
                text: JSON.stringify({
                    success: false,
                    status_code,
                    status_text,
                    error: api_error ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "project_table_status_list",
    description: "UTILITY — Lists workflow statuses for a project (optionally a table). ID TYPES: projectId ENCRYPTED string; tableId ENCRYPTED string (or omit for all). Returns ENCRYPTED status_id values you pass as-is into record_create, record_update, or change_record_status.",
    parameters: z.object({
        projectId: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        tableId: z
            .string()
            .optional()
            .describe("Optional. ENCRYPTED table_id string from project_tables_list. Omit for all tables. (Do NOT pass numeric IDs.)"),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const projectId = encodeURIComponent(args.projectId);
            const tableId = args.tableId ?? "-1";
            const url = `${plumoApiV1Base()}/projects/${projectId}/status?tableId=${encodeURIComponent(tableId)}`;
            const response = await axios.get(url, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                },
            });
            return {
                type: "text",
                text: JSON.stringify(response.data),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "change_record_status",
    description: "DEPRECATED — Prefer **record_update** with \`status_id\` for status changes.\n" +
        "This tool remains functional but will be removed in a future version.\n" +
        "If you must use it: requires record_id (ENCRYPTED) and status_id (ENCRYPTED) from project_table_status_list.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from project_table_status_list (or record_list). Pass as-is."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const response = await axios.patch(`${plumoApiV1Base()}/records/${encodeURIComponent(args.record_id)}/status`, { status_id: args.status_id }, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    "Content-Type": "application/json",
                },
            });
            return {
                type: "text",
                text: JSON.stringify(response.data ?? { success: true }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
function checkAccess(auth) {
    try {
        return true;
        return false;
    }
    catch (ex) {
        console.log(ex);
        return false;
    }
}
export default server;
