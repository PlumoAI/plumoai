import { FastMCP } from "fastmcp";
import { z } from "zod"; // Or any validation library that supports Standard Schema
import axios from "axios";
import dotenv from "dotenv";
dotenv.config();
function plumoApiV1Base() {
    const raw = process.env.PLUMO_API_BASE_URL?.trim() ?? "https://api.plumoai.com/v1";
    return raw.replace(/\/+$/, "");
}
function buildFetchRecordsGuide(opts) {
    const w = opts?.workspace_name?.trim();
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const s = opts?.status_name?.trim();
    const hasNames = w || p || t || s;
    const projectStep = p
        ? `1. Call **ProjectList** (no parameters, or with workspace_id to filter). From the response, identify **project_id** of the project named **${p}**. Pass that \`project_id\` as-is into RecordList.`
        : `1. Call the **ProjectList** tool (no parameters, or with workspace_id to filter).\n2. From the response, read \`data\` (array of projects).\n3. Pick the project you need and take its **\`project_id\`** value.\n4. Pass that string **as-is** into RecordList's \`project_id\`.`;
    const tableStep = t
        ? `1. Call **ProjectTablesList**(projectId) with the encrypted project_id from ProjectList. From the response, identify **table_id** of the table named **${t}**. Pass that \`table_id\` as-is into RecordList. Omit to get records from all tables.`
        : `1. Call **ProjectTablesList**(projectId) with the encrypted project_id from ProjectList.\n2. From the response, each table has **\`table_id\`** and **\`table_name\`**. Pass **\`table_id\`** as-is into RecordList's \`table_id\`.\n3. **Omit** \`table_id\` to get records from all tables.`;
    const statusStep = s
        ? `1. Call **ProjectTableStatusList**(projectId, tableId) or **RecordList** once to get records with status info. From the response, identify **status_id** of the status named **${s}**. Use that \`status_id\` in RecordList. **Omit** for all statuses.`
        : `1. Call **RecordList** once with \`project_id\` (and optional \`table_id\`); each record has **\`status_id\`** and **\`status_name\`**.\n2. Or use **ProjectTableStatusList**(projectId, tableId) if it returns status IDs.\n3. Use one of those **\`status_id\`** values in a later RecordList call. **Omit** for all statuses.`;
    const flowProject = p
        ? `**ProjectList()** → Identify project_id of project **${p}** (encrypted).`
        : `**ProjectList()** → Choose project, note \`project_id\` (encrypted).`;
    const flowTable = t
        ? `**(Optional) ProjectTablesList(projectId)** → Identify table_id of table **${t}** (encrypted).`
        : `**(Optional) ProjectTablesList(projectId)** → Choose table, note \`table_id\` (encrypted).`;
    const flowStatus = s
        ? `**RecordList(...)** → If filtering by status, use status_id of status **${s}** from ProjectTableStatusList or a record.`
        : `**RecordList(project_id [, table_id] [, status_id] [, isIncludeEmptyFields])** → Get records. Use \`status_id\` from a record for a later filtered call. For narrowest result: \`project_id\` + \`table_id\` + \`status_id\`.`;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${w ? `- **Workspace:** ${w}\n` : ""}${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}${s ? `- **Status:** ${s}\n` : ""}
When you call ProjectList, ProjectTablesList, or ProjectTableStatusList, match by the name above to get the correct encrypted ID for use in RecordList.

---
`
        : "";
    return `# Action Guide: Fetch Records

**Use the Guide-RecordList tool** to get this guide whenever you need to use the RecordList tool. This guide explains how to fetch records using the **RecordList** tool: which parameters to use, where each value comes from, and the recommended flow.

---
${contextSection}## 1. Tool and API

| Item | Value |
|------|--------|
| **Tool name** | \`RecordList\` |
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
| Records in one project | \`project_id\` (required) | **ProjectList** → \`data[].project_id\` |
| Records in one workspace | Filter projects first | **ProjectList**(workspace_id) → then use \`project_id\` |
| Records in one table | \`table_id\` (optional) | **ProjectTablesList**(projectId) → \`data[].table_id\` |
| Records in one status | \`status_id\` (optional) | **RecordList** → any \`data[].status_id\`, or **ProjectTableStatusList** |
| Same fields on every record | \`isIncludeEmptyFields: true\` | N/A |
| Paginate results | \`page\`, \`limit\` | N/A (you decide page/size) |

**Good practice:** Narrow in order — project → table → status. For the narrowest result use \`project_id\` + \`table_id\` + \`status_id\` together.

---

## 5. Combining Filters (Examples)

- **All records in a project:** \`RecordList(project_id: "<from ProjectList>")\`
- **All records in one table:** \`RecordList(project_id: "...", table_id: "<from ProjectTablesList>")\`
- **All records in one status:** \`RecordList(project_id: "...", status_id: "<from a record or ProjectTableStatusList>")\`
- **One table and one status:** \`RecordList(project_id: "...", table_id: "...", status_id: "...")\`
- **Full list with all field keys:** \`RecordList(project_id: "...", isIncludeEmptyFields: true)\` (optionally add \`table_id\` / \`status_id\`).
- **Second page of 10 records:** \`RecordList(project_id: "...", page: 2, limit: 10)\`.

---

## 6. Recommended Flow

1. ${flowProject}
2. ${flowTable}
3. ${flowStatus}

---

## 7. Response Shape

Tool returns \`{ success, message, data: records }\`. Each record has keys mapped to field names where possible. \`record_id\`, \`status_id\`, \`table_id\` in the response are encrypted and can be reused. For **DetailedRecord** use the **numeric** record ID, not the encrypted \`record_id\`.

---

## 8. Quick Reference

| Parameter | Source | Action |
|-----------|--------|--------|
| \`project_id\` | **ProjectList** → \`data[].project_id\` | Copy encrypted string as-is. |
| \`table_id\` | **ProjectTablesList**(projectId) → \`data[].table_id\` | Copy as-is; omit for all tables. |
| \`status_id\` | **RecordList** or **ProjectTableStatusList** → \`status_id\` | Copy as-is; omit for all statuses. |
| \`isIncludeEmptyFields\` | N/A | Set \`true\` or \`false\` (or omit). |
| \`page\` | N/A | 1, 2, 3, ... (page number). |
| \`limit\` | N/A | Page size (e.g. 10, 20, 50). |

---

## 9. Common Mistakes to Avoid

- Do **not** use numeric project/table IDs; use **encrypted** \`project_id\` and \`table_id\`.
- Do **not** guess \`status_id\`; get it from a record or ProjectTableStatusList.
- Use the **same** \`project_id\` for ProjectTablesList and RecordList when filtering by table.
- **DetailedRecord** expects the **numeric** record ID, not the encrypted \`record_id\` from the list.
- Prefer server-side filtering (\`table_id\`, \`status_id\`) over fetching all and filtering client-side.
`;
}
function buildCreateRecordGuide(opts) {
    const p = opts?.project_name?.trim();
    const t = opts?.table_name?.trim();
    const hasNames = p || t;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}

When you call ProjectList, ProjectTablesList, ProjectTableFields, or ProjectTableStatusList, match by these names to get the correct encrypted IDs and required fields for CreateRecord.

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
## 2.2 Explicit mapping table (from ProjectTableFields)

Map user inputs using ProjectTableFields:

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

Below is the field list for the selected table (from ProjectTableFields):

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

1. ProjectList → pick target project by name → \`project_id\`
2. ProjectTablesList(projectId) → pick target table by name → \`table_id\`
3. ProjectTableFields(projectId, tableId) → field ids + picklist options
4. ProjectTableStatusList(projectId, tableId) → \`status_id\` (first status if user says "new"/not specified)
5. Build \`recordFieldValues\` from **user-provided non-empty values only**
6. Call CreateRecord(\`project_id\`, \`table_id\`, \`status_id\`, \`recordFieldValues\`)

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
- ProjectTableFields
- the user request values

Hard failure: if any item has \`{ field_key: null AND field_id: null }\` → discard and rebuild the entire \`recordFieldValues\` array.

---
${mappingSection}
${fieldsSection}
`;
}
function buildUpdateRecordGuide(opts) {
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
## 🔁 Explicit mapping table (from ProjectTableFields)
Map user inputs using ProjectTableFields:

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

Never put status inside \`recordFieldValues\`. If you want a status change, set \`status_id\` (tool will call ChangeRecordStatus API internally).

---
## Minimal flow

1. Get \`record_id\` (encrypted) from RecordList / your planning flow.
2. Use \`project_id\` and \`table_id\` for the target record/table.
3. Call ProjectTableFields(project_id, table_id) to get valid \`field_key\` and \`field_id\`.
4. (Conditional) If you want to update fields, build \`recordFieldValues\` from the user request (skip null/empty values).
5. (Conditional, status change only) If you want to change workflow status, get \`status_id\` from ProjectTableStatusList and pass it as \`status_id\` to UpdateRecord.
6. Call UpdateRecord(\`record_id\`, \`project_id\`, \`table_id\`, \`recordFieldValues\`?, \`status_id\`?).

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
- ProjectTableFields
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
        ? `Call **ProjectList**, then from the response identify **project_id** of the project named **${p}**. Use that encrypted \`project_id\` in ProjectTableStatusList.`
        : `Call **ProjectList**, then take \`project_id\` (encrypted) from the project that contains the record.`;
    const tableStep = t
        ? `Call **ProjectTablesList**(projectId), then from the response identify **table_id** of the table named **${t}**. Use that encrypted \`table_id\` in ProjectTableStatusList if you want statuses for that table. Omit to get statuses for all tables.`
        : `Call **ProjectTablesList**(projectId), then take \`table_id\` for the table the record belongs to. Omit to get statuses for all tables.`;
    const statusStep = s
        ? `Call **ProjectTableStatusList**(projectId [, tableId]). From the response, identify **status_id** of the status named **${s}**. Use that encrypted \`status_id\` in ChangeRecordStatus.`
        : `Call **ProjectTableStatusList**(projectId [, tableId]). From the response, identify the target status and copy its **\`status_id\`** (encrypted string). Use that in ChangeRecordStatus.`;
    const flow1 = p
        ? `**ProjectList()** → Identify project_id of project **${p}** (encrypted).`
        : `**ProjectList()** → Get \`project_id\` (encrypted) for the project that contains the record.`;
    const flow2 = t
        ? `**(Optional) ProjectTablesList(projectId)** → Identify table_id of table **${t}** (encrypted).`
        : `**(Optional) ProjectTablesList(projectId)** → Get \`table_id\` if you want statuses for a specific table.`;
    const flow3 = s
        ? `**ProjectTableStatusList(projectId [, tableId])** → Fetch status list; identify status_id of status **${s}** (encrypted).`
        : `**ProjectTableStatusList(projectId [, tableId])** → Get the list of statuses; identify the target status and copy its **\`status_id\`** (encrypted string).`;
    const flow4 = `**ChangeRecordStatus(record_id, status_id)** → Pass the **encrypted** \`record_id\` from RecordList and the **encrypted** \`status_id\` from step 3.`;
    const contextSection = hasNames
        ? `## Target entities (user-specified)

Use these names when fetching lists and resolving IDs:

${p ? `- **Project:** ${p}\n` : ""}${t ? `- **Table:** ${t}\n` : ""}${s ? `- **Status:** ${s}\n` : ""}
Fetch the relevant list (ProjectList, ProjectTablesList, ProjectTableStatusList), then identify the ID for the entity named above to use in the next step.

---
`
        : "";
    return `# Action Guide: Change Record Status

**Use the Guide-ChangeRecordStatus tool** to get this guide whenever you need to change a record's workflow status. This guide explains: first fetch the project table status list to identify the correct \`status_id\`, then call **ChangeRecordStatus** with the record ID and that \`status_id\`.

---
${contextSection}## 1. Overview

| Item | Value |
|------|--------|
| **Tool name** | \`ChangeRecordStatus\` |
| **API** | \`PATCH /v1/records/{record_id}/status\` |
| **Base URL** | \`${plumoApiV1Base()}\` |

To change a record's status you need:
- **record_id** — encrypted string ID of the record (from RecordList).
- **status_id** — encrypted string for the target status (from **ProjectTableStatusList**).

---

## 2. Step 1: Fetch Project Table Status List

**Before** calling ChangeRecordStatus, you must get the list of valid statuses and their encrypted \`status_id\` values.

### Tool: **ProjectTableStatusList**

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`projectId\` | **Yes** | string (encrypted) | Project ID from **ProjectList**. |
| \`tableId\` | No | string (encrypted) | Table ID from **ProjectTablesList**. Omit or \`-1\` for all tables. |

**Where to get projectId:** ${projectStep}

**Where to get tableId (optional):** ${tableStep}

**Response:** List of statuses. Each status has an encrypted \`status_id\` (and usually \`status_name\` or similar). ${s ? `Identify the status named **${s}** and note its **\`status_id\`**` : "Identify the status you want and note its **\`status_id\`**"} — you will pass this exact string to ChangeRecordStatus.

---

## 3. Step 2: Call ChangeRecordStatus

After you have the target \`status_id\` from ProjectTableStatusList (or from a record's current \`status_id\` in RecordList), call:

### Tool: **ChangeRecordStatus**

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`record_id\` | **Yes** | string (encrypted) | Encrypted record ID. Use \`record_id\` from **RecordList** as-is. |
| \`status_id\` | **Yes** | string (encrypted) | Target status. Use the **encrypted** \`status_id\` from **ProjectTableStatusList** (or from another record in the same table). |

**Example:** \`ChangeRecordStatus(record_id: "<from RecordList>", status_id: "<from ProjectTableStatusList>")\`

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
| \`record_id\` (encrypted) | **RecordList** — use \`record_id\` from the record as-is. |
| \`status_id\` (encrypted) | **ProjectTableStatusList**(projectId, tableId) — ${s ? `identify status_id of status **${s}**` : "copy \`status_id\` for the desired status"}. |

---

## 6. Common Mistakes to Avoid

- Do **not** use numeric record ID — use the **encrypted** \`record_id\` from **RecordList** as-is.
- Do **not** guess or invent \`status_id\` — always get it from **ProjectTableStatusList** (or from another record's \`status_id\` in the same table).
- **First** fetch status list, **then** call ChangeRecordStatus.
`;
}
import { StoredProcedureService } from "./utils/sp_caller_service.js";
const spService = new StoredProcedureService();
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
            var response = await axios.get(`https://api.plumoai.com/Auth/oauth/me`, {
                headers: {
                    "Authorization": authHeader
                }
            });
            if (response.data.error) {
                throw new Response(null, {
                    status: 401,
                    statusText: "Invalid OAuth token",
                });
            }
            if (companyId == null) {
                companyId = response.data.data.companyIds.find((x) => x);
            }
            if (response.data.data.companyIds.map((x) => x.toString()).indexOf(companyId.toString()) < 0) {
                throw new Response(null, {
                    status: 401,
                    statusText: "Invalid Company ID",
                });
            }
            return { user_access_token: authHeader.slice(7), expires_in: 900, companyId: companyId, userId: response.data.data.userId };
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
    name: "WorkspaceList",
    description: "Fetch all workspaces (locations) for the authenticated company and user.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({}),
    execute: async (args, { session }) => {
        let data = {
            "storeProcedureName": "GetClientAndLocation",
            "parameters": {
                "userid": session?.userId,
                "companyid": session?.companyId
            }
        };
        var responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, {
            isOpenApi: false,
        }, session?.user_access_token + "");
        return {
            type: "text",
            text: JSON.stringify(responseData.data.map((x) => {
                return {
                    Id: x.LocationID,
                    name: x.Name?.split("§§")?.pop()
                };
            }))
        };
    },
});
server.addTool({
    name: "ProjectList",
    description: "Fetch all projects for the authenticated user. Returns workspace, workspace_id, project_description, project_id (encrypted), project_name, template_name. Optionally filter by workspace_id (encrypted). Use project_id (encrypted) as-is when calling ProjectTablesList or RecordList. (Note: Also fetch sprints for Scrum projects after getting a specific project)\n\n" +
        "Returns workspace, workspace_id (ENCRYPTED string), project_description, project_id (ENCRYPTED string), project_name, template_name, and a numeric project fid (integer).\n\n" +
        "ID USAGE GUIDE:\n" +
        "- project_id (ENCRYPTED string) → use with: RecordList, ProjectTablesList, ProjectTableFields, ProjectTableStatusList, CreateRecord, UpdateRecord, record_create, record_update, record_list\n" +
        "- numeric fid (integer) → use with: ProjectCategories, ProjectSprints, Document_List, Document_Detail, CustomTableFields",
    parameters: z.object({
        workspace_id: z.string().optional().describe("Optional. Filter projects by workspace. Use workspace_id (encrypted) from a previous ProjectList response."),
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
// server.addTool({
//   name: "RecordList",
//   description: `Fetch records grouped by status with basic fields only. Supports filters (project, sprint, user, status, priority, etc.). 
// (Note: Required Sprint ID for scrum project. Pass 0 For non Scrum projects, and For scrum project must be pass sprint ID by fetching all sprints and pass current sprint id after identifying from sprint list. If sprint not defined then ask with user to select sprint).`,
//   parameters: z.object({
//     projectId: z.number().describe("Required. Project ID. Use ProjectList if unknown."),
//     sprintId: z.number().describe("Required Sprint ID for scrum project. Pass 0 For non Scrum projects, and For scrum project must be pass sprint ID by fetching all sprints and pass current sprint id after identifying from sprint list. If sprint not defined then ask with user to select sprint"),
//     parentTaskId: z.number().optional().describe("Parent record ID. Default -1 = all records."),
//     workitemTypeId: z.string().optional().describe("TableID. Default -1 = all tables. Can pass multiple numeric Table IDs comma-separated."),
//     assignedToUser: z.number().optional().describe("User assigned to record. Default -1 = all users."),
//     createdByUser: z.number().optional().describe("User who created record. Default -1 = all users."),
//     statusID: z.number().optional().describe("Filter by status ID. Default -1 = all statuses."),
//     priority: z.string().optional().describe("Filter by priority. Default -1 = all priorities."),
//     title: z.string().optional().describe("Search by record title."),
//     completed: z.number().optional().describe("Completion flag. 0=incomplete, 1=completed. Default 0."),
//     taskId: z.number().optional().describe("Specific record ID. Default -1 = all records."),
//     taskUniqueKey: z.string().optional().describe("Unique record key (e.g., SALE-737, ABC-123)."),
//     pageNo: z.number().optional().describe("Page number. Default 1."),
//     rows: z.number().optional().describe("Rows per page. Default 10."),
//   }),
//   canAccess(auth) {
//     return checkAccess(auth);
//   },
//   execute: async (args, { session }) => {
//     try {
//       const data = {
//         storeProcedureName: "usp_proj_get_tasks_with_nested_filter",
//         parameters: {
//           p_json: [
//             {
//               p_project_id: String(args.projectId),
//               p_sprint_id: args.sprintId ?? 0,
//               p_parent_task_id: String(args.parentTaskId ?? -1),
//               p_proj_workitem_type_fid: String(args.workitemTypeId ?? -1),
//               p_assigned_to_user: String(args.assignedToUser ?? -1),
//               p_created_by_user: String(args.createdByUser ?? -1),
//               p_status: String(args.statusID ?? -1),
//               p_priority: String(args.priority ?? -1),
//               p_title: args.title ?? "",
//               p_completed: args.completed ?? 0,
//               p_task_id: String(args.taskId ?? -1),
//               p_task_unique_key: args.taskUniqueKey ?? "",
//               p_calledfrom: "S",
//               p_groupby: "S",
//               p_page_no: args.pageNo ?? 1,
//               p_rows: args.rows ?? 10,
//               p_returncount: 1,
//               proj_fields: {},
//             }
//           ]
//         },
//         version: 4,
//       };
//       const responseData = await spService.storedProcedureComplianceDb(
//         data,
//         session?.companyId,
//         { isOpenApi: false },
//         String(session?.user_access_token ?? "")
//       );
//       const tasks = (responseData?.data ?? []).length>1?(responseData?.data ?? [])[1]:(responseData?.data ?? []);
//       return {
//         type: "text",
//         text: JSON.stringify(
//           {
//             "status_records_count":(responseData?.data ?? [[]])[0].map((m:any)=>{
//               return {
//                 "statusID":m.rec_type,
//                 "records_count": m.rec_count
//               }
//             }),
//             "records":tasks
//           }
//         ),
//       };
//     } catch (error: any) {
//       return {
//         type: "text",
//         text: JSON.stringify({ error: error.message ?? "Unknown error occurred" }),
//       };
//     }
//   },
// });
server.addTool({
    name: "RecordList",
    description: "Preferred: use `record_list`. `RecordList` is the legacy name — identical behaviour.\n\n" +
        "STEP 2 OF 2 — Fetches records for a project (optionally filtered by table/status/record) with pagination.\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → get project_id (ENCRYPTED string)\n" +
        "(2) RecordList(project_id [, table_id] [, status_id] [, recordId])\n\n" +
        "OPTIONAL PREREQS (filters):\n" +
        "- table_id: call ProjectTablesList(projectId) → get table_id (ENCRYPTED string)\n" +
        "- status_id: call ProjectTableStatusList(projectId [, tableId]) → get status_id (ENCRYPTED string)\n\n" +
        "ID TYPES: project_id/table_id/status_id/recordId are ENCRYPTED STRINGS. Pass them as-is (NOT numeric).\n\n" +
        "RETURN VALUE NOTE: Each record contains TWO id fields:\n" +
        "- record_id (ENCRYPTED string) → pass to UpdateRecord, record_update, ChangeRecordStatus\n" +
        "- id (integer / numeric) → pass to DetailedRecord, AddRecordComment\n\n" +
        "Do NOT mix these up. Passing an encrypted string where a numeric id is required will cause a silent failure or API error.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        table_id: z.string().optional().describe("Optional. ENCRYPTED table_id string from ProjectTablesList(projectId). Omit for all tables."),
        status_id: z.string().optional().describe("Optional. ENCRYPTED status_id string from ProjectTableStatusList or prior RecordList. Omit for all statuses."),
        recordId: z.string().optional().describe("Optional. ENCRYPTED record_id string (from RecordList) to fetch/filter a specific record."),
        isIncludeEmptyFields: z.boolean().optional().describe("Optional. When true, every record includes all field keys (with null where there is no value)."),
        page: z.number().int().positive().optional().describe("Optional. Page number for pagination (1-based). If omitted, server default is used."),
        limit: z.number().int().positive().optional().describe("Optional. Page size for pagination. If omitted, server default is used."),
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
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "Records retrieved successfully",
                    data: transformedRecords,
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
// Preferred snake_case alias (migration path). Keep legacy tool name for compatibility.
server.addTool({
    name: "record_list",
    description: "Preferred: use `record_list`. `RecordList` is the legacy name — identical behaviour.\n" +
        "STEP 2 OF 2 — Fetches records for a project (optionally filtered by table/status/record) with pagination.\n" +
        "REQUIRED CALL ORDER: (1) ProjectList → project_id (ENCRYPTED string), (2) record_list(project_id [, table_id] [, status_id] [, recordId]).\n\n" +
        "RETURN VALUE NOTE: Each record contains TWO id fields:\n" +
        "- record_id (ENCRYPTED string) → pass to UpdateRecord, record_update, ChangeRecordStatus\n" +
        "- id (integer / numeric) → pass to DetailedRecord, AddRecordComment\n\n" +
        "Do NOT mix these up. Passing an encrypted string where a numeric id is required will cause a silent failure or API error.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        table_id: z.string().optional().describe("Optional. ENCRYPTED table_id string from ProjectTablesList(projectId). Omit for all tables."),
        status_id: z.string().optional().describe("Optional. ENCRYPTED status_id string from ProjectTableStatusList or prior RecordList. Omit for all statuses."),
        recordId: z.string().optional().describe("Optional. ENCRYPTED record_id string (from RecordList) to fetch/filter a specific record."),
        isIncludeEmptyFields: z.boolean().optional().describe("Optional. When true, every record includes all field keys (with null where there is no value)."),
        page: z.number().int().positive().optional().describe("Optional. Page number for pagination (1-based). If omitted, server default is used."),
        limit: z.number().int().positive().optional().describe("Optional. Page size for pagination. If omitted, server default is used."),
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
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "Records retrieved successfully",
                    data: transformedRecords,
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
    name: "ProjectTablesList",
    description: "UTILITY — Lists project tables (work item types). ID TYPES: projectId is ENCRYPTED string. Returns ENCRYPTED table_id values. Use: (1) ProjectList → get ENCRYPTED project_id, (2) ProjectTablesList(projectId) → get ENCRYPTED table_id. Pass these encrypted strings as-is into downstream tools (RecordList/CreateRecord/UpdateRecord).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z
            .string()
            .describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is (NOT numeric)."),
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
    name: "ProjectTableFields",
    description: "UTILITY — Lists fields for a project table. REQUIRED BEFORE CreateRecord/UpdateRecord when you need field IDs. ID TYPES: projectId ENCRYPTED string, tableId ENCRYPTED string. Returns ENCRYPTED field_id (proj_field_id) plus field_key (physical/system key when present), type, is_required, and value options for picklists. Use these results to build recordFieldValues.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        tableId: z.string().describe("Required. ENCRYPTED table_id string from ProjectTablesList(projectId). Pass as-is."),
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
    name: "ProjectCategories",
    description: "UTILITY — Fetches state categories for a project.\n" +
        "ID TYPE: projectId is a NUMERIC integer (the fid field from ProjectList response). NOT the encrypted project_id string.\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → find project by name, note its numeric fid integer field.\n" +
        "(2) ProjectCategories(projectId=that integer).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Required. NUMERIC project fid (integer) from ProjectList response. Use the integer fid field, NOT the encrypted project_id string. Example: 4821 (not \"aGVsbG8gd29ybGQ=\")",
        }),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_state_category",
                parameters: {
                    p_project_fid: String(args.projectId),
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const workflowStatus = responseData?.data ?? [];
            return {
                type: "text",
                text: JSON.stringify(workflowStatus),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
// Preferred snake_case alias (migration path). Keep legacy tool name for compatibility.
server.addTool({
    name: "record_create",
    description: "Preferred: use `record_create`. `CreateRecord` is the legacy name — identical behaviour.\n" +
        "STEP 5 OF 5 — Creates a new record. REQUIRED CALL ORDER: (1) ProjectList → project_id, (2) ProjectTablesList(projectId) → table_id, (3) ProjectTableStatusList(projectId [, tableId]) → status_id, (4) ProjectTableFields(projectId, tableId) → field_id, (5) record_create(project_id, table_id, status_id, recordFieldValues). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects. Each object sets one field. Only include fields you want to set.\n" +
        "Fields where is_required=true (from ProjectTableFields) MUST be included.\n\n" +
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
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by ProjectTableFields. Do not invent values.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ProjectTablesList(projectId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ProjectTableStatusList(projectId [, tableId]). Pass as-is."),
        recordFieldValues: z
            .array(z.object({
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from ProjectTableFields. MUST NOT be null/empty."),
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
                        message: "Invalid recordFieldValues: field_id is required for every item and must not be null/empty. Rebuild recordFieldValues using ProjectTableFields.",
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
    name: "DetailedRecord",
    description: "STEP 2 OF 2 — Fetches full details of a single record: all fields, custom fields, comments, attachments, checklist items, linked records, and user info.\n" +
        "ID TYPE: recordId is NUMERIC (integer), NOT the encrypted record_id string.\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) RecordList(project_id) → locate the record by name/title, note its numeric id integer field (NOT the record_id encrypted string).\n" +
        "(2) DetailedRecord(recordId=that integer).",
    parameters: z.object({
        recordId: z.number().describe("Required. Numeric record ID. Use the numeric ID directly (not the encrypted record_id string from RecordList)."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/records/${args.recordId}`, {
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
    name: "ProjectSprints",
    description: "UTILITY — Fetches all sprints for a project. Returns empty array if project is not a Scrum template.\n" +
        "ID TYPE: projectId is a NUMERIC integer (the fid field from ProjectList response).\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → find project by name, note its numeric fid integer field.\n" +
        "(2) ProjectSprints(projectId=that integer).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Required. NUMERIC project fid (integer) from ProjectList response. Use the integer fid field, NOT the encrypted project_id string.",
        }),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_sprint",
                parameters: {
                    p_project_id: String(args.projectId),
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const sprints = responseData?.data ?? [];
            return {
                type: "text",
                text: JSON.stringify(sprints),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
function normalizeTaskRecord(data) {
    const task = data[0][0]; // Main task info
    const fields = data[1] || []; // Field metadata
    const comments = data[3] || [];
    const attachments = data[4] || [];
    const checklist = data[8] || [];
    const linkedTasks = data[5] || [];
    const users = data[2] || [];
    // Step 1 — Resolve dynamic fields
    const resolvedFields = {};
    for (const f of fields) {
        if (f.is_in_task_table === 1) {
            resolvedFields[f.field_name] = task[f.task_actual_fieldname];
        }
        else {
            resolvedFields[f.field_name] = f.field_value;
        }
    }
    // Step 2 — Build normalized record
    return {
        ...task,
        ...resolvedFields,
        users: {
            createdBy: task.created_by,
            modifiedBy: task.modified_by,
            assignee: {
                name: task.assignee_name,
                email: task.assignee_email,
                mobile: task.assignee_mobile,
                profilePicture: task.assignee_profilepicture
            }
        },
        company: {
            name: task.company_name,
            project: task.project_name,
            workspace: task.project_location
        },
        comments: comments,
        attachments: attachments,
        checklist: checklist,
        linkedTasks: linkedTasks,
        recordUsers: users
    };
}
server.addTool({
    name: "TableFieldsTab",
    description: "UTILITY — Fetches field tab groupings (UI sections) for a table. Returns tab names and the field_ids within each tab.\n" +
        "Use when you need to know how fields are visually grouped in the UI.\n" +
        "ID TYPE: tableId is NUMERIC integer (table fid from ProjectTablesList response).\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectTablesList(projectId) → find table, note its numeric fid integer.\n" +
        "(2) TableFieldsTab(tableId=that integer).\n\n" +
        "NOTE: For the field list itself, use ProjectTableFields (ENCRYPTED IDs). TableFieldsTab is only needed when you care about tab/section organisation.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        tableId: z.number({
            description: "Required. NUMERIC table fid (integer) from ProjectTablesList response. NOT the encrypted table_id string.",
        }),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_field_tabs",
                parameters: {
                    p_workitemTypeId: String(args.tableId),
                    p_loggedin_user: String(session?.userId),
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const tabs = responseData?.data ?? [];
            return {
                type: "text",
                text: JSON.stringify(tabs)
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "CustomTableFields",
    description: "UTILITY — Fetches user-defined (custom) fields only for a table.\n" +
        "Difference from ProjectTableFields: ProjectTableFields returns ALL fields (system + custom). CustomTableFields returns custom fields only.\n" +
        "ID TYPES: both projectId and tableId are NUMERIC integers (fid values).\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → note numeric project fid integer.\n" +
        "(2) ProjectTablesList(projectId) → note numeric table fid integer.\n" +
        "(3) CustomTableFields(projectId=project fid, tableId=table fid).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "projectId: Required. NUMERIC project fid (integer) from ProjectList. NOT encrypted.",
        }),
        tableId: z.number({
            description: "tableId: Required. NUMERIC table fid (integer) from ProjectTablesList. NOT encrypted.",
        }),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_project_fields",
                version: 2,
                parameters: {
                    p_project_fid: String(args.projectId),
                    p_proj_workitem_type_fid: String(args.tableId),
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const fields = responseData?.data ?? [];
            return {
                type: "text",
                text: JSON.stringify(fields),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "CreateRecord",
    description: "Preferred: use `record_create`. `CreateRecord` is the legacy name — identical behaviour.\n\n" +
        "STEP 5 OF 5 — Creates a new record.\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → get project_id (ENCRYPTED string)\n" +
        "(2) ProjectTablesList(projectId) → get table_id (ENCRYPTED string)\n" +
        "(3) ProjectTableStatusList(projectId [, tableId]) → get status_id (ENCRYPTED string)\n" +
        "(4) ProjectTableFields(projectId, tableId) → get field_id for each field you want to set (ENCRYPTED string)\n" +
        "(5) CreateRecord(project_id, table_id, status_id, recordFieldValues)\n\n" +
        "ID TYPES:\n" +
        "- project_id/table_id/status_id/field_id/record_id in this HTTP workflow are ENCRYPTED STRINGS. Pass them as-is from prior tool responses.\n" +
        "- Numeric IDs are used by legacy stored-procedure tools (e.g., UpdateRecordField/DetailedRecord) and are NOT accepted here.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects. Each object sets one field. Only include fields you want to set.\n" +
        "Fields where is_required=true (from ProjectTableFields) MUST be included.\n\n" +
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
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by ProjectTableFields. Do not invent values.\n\n" +
        "RULES:\n" +
        "- Do NOT send items with null/undefined/empty-string values.\n" +
        "- field_id must be present for every item and must not be null/empty.\n" +
        "- Do NOT use a 'fields' parameter; use recordFieldValues.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ProjectTablesList(projectId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ProjectTableStatusList(projectId [, tableId]). Pass as-is."),
        recordFieldValues: z.array(z.object({
            field_id: z
                .string()
                .min(1)
                .describe("Required. ENCRYPTED field_id string from ProjectTableFields. MUST NOT be null/empty."),
            field_key: z
                .string()
                .nullable()
                .optional()
                .describe("Optional. Physical/system field key like 'title' (only when available)."),
            value: z.any().describe("Field value to set for this record field."),
        })).nonempty().describe("Required. Field values to set when creating the record. See tool description for worked examples."),
    }),
    execute: async (args, { session }) => {
        try {
            const rawRecordFieldValues = (args.recordFieldValues ?? []);
            // Drop any fields where value is null/undefined/empty-string
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
                        message: "Invalid recordFieldValues: field_id is required for every item and must not be null/empty. Rebuild recordFieldValues using ProjectTableFields.",
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
            // Transform created record keys to field names (same approach as RecordList)
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
    }
});
// Preferred snake_case alias (migration path). Keep legacy tool name for compatibility.
server.addTool({
    name: "record_update",
    description: "Preferred: use `record_update`. `UpdateRecord` is the legacy name — identical behaviour.\n" +
        "STEP 4 OF 4 — Updates a record (fields and/or status). ID TYPES: record_id/project_id/table_id/status_id/field_id are ENCRYPTED strings — pass as-is.\n\n" +
        "THREE USAGE MODES — choose the one that fits:\n\n" +
        "Mode 1 — Fields only (do not pass status_id):\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n" +
        "  status_id: omit or null\n\n" +
        "Mode 2 — Status only (do not pass field values):\n" +
        "  status_id: \"ENCRYPTED_status_id from ProjectTableStatusList\"\n" +
        "  recordFieldValues: null\n\n" +
        "Mode 3 — Fields + status in one call:\n" +
        "  status_id: \"ENCRYPTED_status_id\"\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n\n" +
        "PREFERRED OVER ChangeRecordStatus for all status changes.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from RecordList. Pass as-is."),
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ProjectTablesList(projectId). Pass as-is."),
        status_id: z.string().nullable().optional().describe("Optional. ENCRYPTED status_id string from ProjectTableStatusList. Pass as-is for status changes."),
        recordFieldValues: z
            .array(z.object({
            field_key: z.string().nullable().optional().describe("Optional. Physical/system field key like 'title'."),
            field_id: z.string().nullable().optional().describe("Optional. ENCRYPTED field_id string from ProjectTableFields (required when field_key is null/empty)."),
            value: z.any().describe("Field value to update."),
        }))
            .nullable()
            .optional()
            .describe("Optional. Field updates. Can be null/empty for status-only updates."),
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
            const invalidItems = recordFieldValues.filter((it) => {
                const k = it?.field_key ?? null;
                const id = it?.field_id ?? null;
                const hasKey = typeof k === "string" && k.trim().length > 0;
                const hasId = typeof id === "string" && id.trim().length > 0;
                if (!hasKey && !hasId)
                    return true;
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
                lastMessage = response.data?.message ?? lastMessage;
                lastSuccess = response.data?.success ?? lastSuccess;
            }
            if (statusId) {
                const statusResponse = await axios.patch(`${plumoApiV1Base()}/records/${encodeURIComponent(args.record_id)}/status`, { status_id: statusId }, {
                    headers: {
                        Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                        "Content-Type": "application/json",
                    },
                });
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
    name: "UpdateRecord",
    description: "Preferred: use `record_update`. `UpdateRecord` is the legacy name — identical behaviour.\n\n" +
        "STEP 4 OF 4 — Updates a record (fields and/or workflow status) using the HTTP workflow.\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → get project_id (ENCRYPTED string)\n" +
        "(2) ProjectTablesList(projectId) → get table_id (ENCRYPTED string)\n" +
        "(3) RecordList(project_id [, table_id] [, status_id]) → get record_id (ENCRYPTED string) of the record to update\n" +
        "(4) UpdateRecord(record_id, project_id, table_id, recordFieldValues?, status_id?)\n\n" +
        "OPTIONAL PREREQS:\n" +
        "- If updating fields: call ProjectTableFields(projectId, tableId) first to get ENCRYPTED field_id values.\n" +
        "- If changing status: call ProjectTableStatusList(projectId [, tableId]) first to get ENCRYPTED status_id.\n\n" +
        "ID TYPES:\n" +
        "- record_id/project_id/table_id/status_id/field_id are ENCRYPTED STRINGS for this tool.\n" +
        "- Numeric IDs are for legacy SP tools only.\n\n" +
        "THREE USAGE MODES — choose the one that fits:\n\n" +
        "Mode 1 — Fields only (do not pass status_id):\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n" +
        "  status_id: omit or null\n\n" +
        "Mode 2 — Status only (do not pass field values):\n" +
        "  status_id: \"ENCRYPTED_status_id from ProjectTableStatusList\"\n" +
        "  recordFieldValues: null\n\n" +
        "Mode 3 — Fields + status in one call:\n" +
        "  status_id: \"ENCRYPTED_status_id\"\n" +
        "  recordFieldValues: [{ field_id, field_key, value }, ...]\n\n" +
        "PREFERRED OVER ChangeRecordStatus for all status changes.\n\n" +
        "recordFieldValues SHAPE (worked examples):\n" +
        "- Update title: [{ field_key: \"title\", value: \"New title\" }] OR [{ field_id: \"<encrypted field_id>\", value: \"New title\" }]\n" +
        "- Update picklist: [{ field_id: \"<encrypted field_id>\", value: \"High\" }]\n" +
        "RULES:\n" +
        "- Each item must include either field_key or field_id. If field_key is null/empty, field_id is required.\n" +
        "- Do NOT send items with null/undefined/empty-string values.\n" +
        "- For status-only changes, omit recordFieldValues (or pass null/empty) and pass status_id.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from RecordList. Pass as-is."),
        project_id: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ProjectTablesList(projectId). Pass as-is."),
        status_id: z
            .string()
            .nullable()
            .optional()
            .describe("Optional. ENCRYPTED status_id string from ProjectTableStatusList. Pass as-is for status changes."),
        recordFieldValues: z
            .array(z.object({
            field_key: z.string().nullable().optional().describe("Optional. Physical/system field key like 'title'."),
            field_id: z
                .string()
                .nullable()
                .optional()
                .describe("Optional. ENCRYPTED field_id string from ProjectTableFields (required when field_key is null/empty)."),
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
    name: "ProjectTableStatusList",
    description: "UTILITY — Lists workflow statuses for a project (optionally a table). ID TYPES: projectId ENCRYPTED string; tableId ENCRYPTED string (or omit for all). Returns ENCRYPTED status_id values you pass as-is into CreateRecord/UpdateRecord/ChangeRecordStatus.",
    parameters: z.object({
        projectId: z.string().describe("Required. ENCRYPTED project_id string from ProjectList. Pass as-is."),
        tableId: z
            .string()
            .optional()
            .describe("Optional. ENCRYPTED table_id string from ProjectTablesList. Omit for all tables. (Do NOT pass numeric IDs.)"),
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
    name: "UpdateRecordField",
    description: "LEGACY (stored procedure) — Updates one field using NUMERIC IDs only.\n" +
        "ID TYPES: recordId NUMERIC, fieldId NUMERIC.\n" +
        "DEPRECATION: Prefer the HTTP workflow `UpdateRecord` (ENCRYPTED IDs) for nearly all agent use-cases. Use this only when you explicitly have numeric IDs and you cannot use UpdateRecord.",
    parameters: z.object({
        recordId: z.number().describe("Required. NUMERIC record ID (NOT encrypted record_id from RecordList)."),
        fieldId: z.number().describe("Required. NUMERIC field ID (SQL fid)."),
        value: z.string().optional().describe("New value for the field."),
        requiresQuotes: z.boolean().default(true).describe("Whether the value should be quoted in SQL. If field is string then true. Default true."),
        text: z.string().optional().describe("New text value for the field if field is text_multiline."),
        users: z.string().optional().describe("Optional user IDs (comma-separated) if field is user-based."),
        teams: z.string().optional().describe("Optional team IDs (comma-separated) if field is team-based."),
        userType: z.string().optional().describe("Optional user type info if field is user-based."),
        jsonValue: z.union([z.number(), z.string()]).default(0).describe("If the field is JSON-based, pass JSON or set 0."),
        uniqueField: z.string().nullable().optional().describe("If field has uniqueness constraint, pass unique value."),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_update_task_details_onebyone",
                version: 3,
                parameters: {
                    p_Json: [
                        {
                            p_task_fid: args.recordId,
                            p_proj_field_fid: args.fieldId,
                            p_field_quotes_required: args.requiresQuotes ? 1 : 0,
                            p_field_value: args.value,
                            p_field_text: args.text ?? "",
                            p_field_users: args.users ?? "",
                            p_field_teams: args.teams ?? "",
                            p_field_user_type: args.userType ?? "",
                            p_field_json_value: args.jsonValue ?? 0,
                            p_uniquefield: args.uniqueField ?? null,
                            p_loggedin_user: String(session?.userId),
                        }
                    ]
                }
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    updatedFieldId: args.fieldId,
                    newValue: args.value,
                    response: responseData?.data ?? []
                }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "ChangeRecordStatus",
    description: "DEPRECATED — Use record_update or UpdateRecord with status_id instead.\n" +
        "This tool remains functional but will be removed in a future version.\n" +
        "If you must use it: requires record_id (ENCRYPTED) and status_id (ENCRYPTED) from ProjectTableStatusList.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from RecordList. Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ProjectTableStatusList (or RecordList). Pass as-is."),
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
server.addTool({
    name: "AddRecordComment",
    description: "Adds a comment to a record.\n" +
        "ID TYPE: recordId is NUMERIC (integer), NOT the encrypted record_id string.\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) RecordList(project_id) → locate the record, note its numeric id integer field.\n" +
        "(2) AddRecordComment(recordId=that integer, comment=html string).\n\n" +
        "COMMENT FORMAT: HTML string. Plain text works too (wrap in <p> tags for best results).\n" +
        "Example: \"<p>Reviewed and approved. Proceeding to next stage.</p>\"",
    parameters: z.object({
        recordId: z.number().describe("Required. NUMERIC record id (integer) from RecordList response. Use the integer id field, NOT the encrypted record_id string. Example: 47832 (not \"aGVsbG8=\")"),
        comment: z.string().describe("Required. Comment text in HTML format. Example: \"<p>This is my comment.</p>\" Plain text is also accepted but HTML is preferred for rich formatting."),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                "storeProcedureName": "usp_proj_save_task_comments",
                "version": 3,
                "parameters": {
                    "p_Json": {
                        "task_comment_id": 0,
                        "task_fid": args.recordId,
                        "task_comment": encryptHtml(args.comment),
                        "mention_userid": "",
                        "mention_teamid": "",
                        "loggedin_user": session?.userId,
                        "action": "I"
                    }
                }
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            return {
                type: "text",
                text: JSON.stringify(responseData?.data),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    error: error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: `Document_List`,
    description: "UTILITY — Fetches documents for a project.\n" +
        "ID TYPES: projectId is NUMERIC integer (fid from ProjectList). webDocumentId is also NUMERIC (from this tool's own response).\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → find project by name, note its numeric fid integer.\n" +
        "(2) Document_List(projectId=that integer) → get list of documents with webDocumentId.\n" +
        "(3) Optionally: Document_Detail(projectId, webDocumentId) for full content.\n\n" +
        "PAGINATION NOTE: Pass webDocumentId=0 (or omit) to fetch all documents.\n" +
        "Pass a specific webDocumentId to fetch only that document's metadata.",
    parameters: z.object({
        projectId: z.number().describe("Required. NUMERIC project fid (integer) from ProjectList. NOT the encrypted project_id string."),
        webDocumentId: z.number().optional().describe("Optional. NUMERIC document ID. Omit or pass 0 to list all documents. Pass a specific integer to filter to one document. Obtain from a prior Document_List response."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            var documents = await documentsList(args.webDocumentId, args.projectId, session?.userId, session?.companyId, session?.user_access_token);
            return {
                type: "text",
                text: JSON.stringify(documents),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({ error: error.message ?? "Unknown error occurred" }),
            };
        }
    },
});
server.addTool({
    name: `Document_Detail`,
    description: "UTILITY — Fetches full content of a specific document.\n" +
        "ID TYPES: projectId is NUMERIC integer (fid). webDocumentId is NUMERIC integer.\n\n" +
        "REQUIRED CALL ORDER:\n" +
        "(1) ProjectList → note numeric project fid.\n" +
        "(2) Document_List(projectId) → get document list, note webDocumentId of the target doc.\n" +
        "(3) Document_Detail(projectId, webDocumentId).",
    parameters: z.object({
        projectId: z.number().describe("Required. NUMERIC project fid (integer) from ProjectList. NOT the encrypted project_id string."),
        webDocumentId: z.number().describe("Required. NUMERIC document ID from Document_List response."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            var documents = await documentsList(args.webDocumentId, args.projectId, session?.userId, session?.companyId, session?.user_access_token);
            return {
                type: "text",
                text: JSON.stringify(documents),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({ error: error.message ?? "Unknown error occurred" }),
            };
        }
    },
});
server.addTool({
    name: `Implement-Project`,
    description: "Create or Implement a Project",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number().describe("Project ID where the project will be implemented."),
    }),
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    execute: async (args, { session }) => {
        let projResponse = await spService.storedProcedureComplianceDb({
            storeProcedureName: "usp_proj_get_project",
            parameters: { p_project_id: args.projectId, p_LoggedInUser: session?.userId, p_CompanyID: session?.companyId, p_Location_fid: -1, p_proj_status: "P", p_PageNumber: 1, p_RowsOfPage: 1000 }
        }, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
        let softwareDevProjects = (projResponse?.data ?? []).filter((p) => p.template_proj_type_fid === 1);
        if (softwareDevProjects.length == 0) {
            return {
                type: "text",
                text: `No active software development project found with ID ${args.projectId}.`
            };
        }
        let proj = softwareDevProjects[0];
        return {
            type: "text",
            text: `AI Project Implementation Protocol

⚙️ This protocol must be strictly followed in sequential order (1 → 23).
Each step defines a mandatory rule for structured and traceable AI-driven project execution.

1. Project Initialization

Prompt project name is ${proj.project_name}.

2. Confirmation to Begin

Ask the user:

“Are you ready to proceed with project implementation for ${proj.project_name}?”
Only continue after receiving confirmation.

3. Gather Project Information

Upon confirmation, begin gathering all required information for ${proj.project_name}.

4. Fetch Web Documents List

Fetch the list of web documents (documentList) related to the project.
👉 Only fetch the list — no content yet.

5. Analyze "JSON DATA" Document

Identify the "JSON DATA" web document.
Fetch its detailed content using its page_id and analyze it carefully for planning.

6. Fetch Only Workflows and Tables

Fetch all workflows and their associated tables for the project.
🚫 Do not fetch sprints.

7. Fetch Higher-Level Records

Fetch all records (one by one) of the Epics, User Stories, and Features tables.

8. Fetch Task-Level Records

Fetch all records (one by one) of the Tasks and Subtasks tables.

9. Analyze Structure and Hierarchy

Analyze records and relationships to understand the project structure.

Tables have hierarchical levels:

Level 1 = Parent

Level 2 = Child

Level 3 = Sub-child, etc.

Each child record references its parent record ID.

Maintain and enforce this hierarchy throughout implementation.

10. Analyze JSON Document

Analyze the "JSON DATA" document content to extract key technical and structural information for planning and setup.

11. Create Project Plan

Based on analysis, create a comprehensive project plan following the same technical structure defined in the “Project Plan” document.

12. Confirm Plan

Ask the user to confirm the project plan before moving forward.

13.1 Execution Mode Selection

Before starting implementation, ask the user how they want to begin execution:

“How would you like to start implementing ${proj.project_name}?”
Choose one of the following modes:

Sequential Order — process all records strictly in hierarchical sequence.

UI Design First — implement all frontend/UI-related records first.

Backend First — implement all backend-related records first.

The chosen mode defines the initial execution order.
After selection, apply all subsequent rules (Step 15 onward) according to this mode.

13. Proceed After Confirmation

After confirmation and mode selection, start implementing the project ${proj.project_name} as per the approved plan.

14. Create To-Do List

Strictly build a To-Do list containing all Epics, Features, Tasks, and Subtasks — each To-Do corresponds to one record.

15. Sequential Record Execution

Process one record at a time — no parallelism allowed.

For each record:

a. Ensure current status is "To Do".
b. Change record status to "InProgress" (Mandatory before coding).
c. Implement only what is defined in that record.
🚫 Do not add extra logic, assumptions, or unrelated functionality.
d. After completion:

Comment in record all implementation you done.

Add a comment summarizing the work done.
e. Change record status to "Testing" (Mandatory after completion).
f. Perform unit and integration testing.
g. Change record status to "Completed" (Mandatory after testing).
h. Move to the next record sequentially only after all its dependencies are resolved.

🧩 Hierarchy Handling (Strict Enforcement)

Every record must respect its hierarchical dependencies before implementation.

1. Hierarchy Levels

Level 1 → Parent

Level 2 → Child

Level 3 → Sub-Child

and so on.

Each record contains a reference to its parent’s ID.

2. Execution Rules

Parent Before Child:
A child record can be processed only when its parent record is "InProgress" or "Completed".
🚫 If a parent is "To Do", its children must wait — no execution allowed.

Sequential Order by Level:
Process records level by level:

Start from Level 1 (parents).

Move to Level 2 (children) only after parent is started.

Proceed to Level 3 (sub-children) only after child is started, and so on.

Parent Auto-Completion Rule:
A parent record can be automatically marked "Completed" only when:

✅ All its child records are marked "Completed".

✅ The parent’s own logic or setup is verified as complete.

Parent Progress Rule:
If a parent has even one child "To Do" or "InProgress",
the parent’s status must remain "InProgress" — never "Completed".

Child-First Restriction:
A child record cannot start before its parent is "InProgress".
Attempting to start a child while the parent is "To Do" or "Testing" is invalid.

Bottom-Up Finalization:
Always complete the deepest hierarchy level first, then move upward.

Record Skip Restriction:
No record at any level can be skipped or executed before its dependent parent is ready.

✅ Hierarchy Example

Level	Record	Allowed Start Condition
1	Epic	Always starts first
2	Feature	Parent Epic must be "InProgress"
3	Task	Parent Feature must be "InProgress"
4	Subtask	Parent Task must be "InProgress"

🚫 Subtask cannot begin if Task is "To Do".
🚫 Task cannot begin if Feature is "To Do".
✅ Parent auto-completes only when all children = "Completed".

16. Environment Setup (.env)

During implementation, populate the .env file with required environment variables.
Prompt the user whenever input is needed.

17. Database Implementation

Under DB-related records:

Create tables, relationships, and constraints as defined.

Generate a single executable DB file that can apply all changes at once
using the DB connection provided in .env.

18. Post-Implementation Update

After each record implementation:

Comment in record all implementation you done.

Add a work summary in comments.

Ensure record status is "Completed".

19. Final Review

After all records are completed:

Review the entire project implementation.

Confirm all records, hierarchy, and dependencies are properly addressed.

20. Execute DB Changes

At completion, execute all database changes in one click using the generated file and connection string from .env.

21. Testing

Strictly run all available test cases and ensure everything works as expected.

22. Bug Handling

If issues appear during or after testing:

Create a Bug Record (status "To Do").

Process bug records using the same lifecycle:
"To Do" → "InProgress" → "Testing" → "Completed".

Each fix must follow the same process as Step 15.

23. Final Launch

Once all records (including bugs) are completed:

Ensure the project is fully functional and running in the development environment.

⚙️ Enforced Rules Summary

✅ Status lifecycle:
"To Do" → "InProgress" → "Testing" → "Completed"

✅ Create project folder before starting any implementation.
✅ Implement only code mentioned in each record — nothing more.
✅ Always comment in record all implementation you done.
✅ Status changes before and after coding are mandatory.
✅ Parent auto-completion requires:
✅ No need to fetch record in detail for each record.Just fetch list of records.

All children "Completed", and

Parent verified as compatible.
✅ Bug records follow the same lifecycle.
✅ Final project must be running and functional.
✅ Always ask execution mode before starting implementation.
✅ Should follow record execution process that is mentioned in Step 15.

🚫 No assumptions, shortcuts, or parallel execution.
⏸ Pause and ask for clarification if any ambiguity arises.`
        };
    }
});
export default server;
function encryptHtml(htmlStr) {
    if (htmlStr != null) {
        htmlStr = htmlStr
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/\\&quot;/g, "slash&quot;")
            .replace(/'/g, "&#39;");
        return encodeURIComponent(htmlStr);
    }
    return null;
}
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
async function documentsList(documentId, projectId, userId, companyId, user_access_token) {
    if (documentId == null || documentId <= 0) {
        documentId = -1;
    }
    const data = {
        "storeProcedureName": "usp_notebook_listing",
        "parameters": {
            "p_ClientId": null,
            "p_LoggedinUser": userId,
            "p_LocationId": null,
            "pProjectID": projectId,
            "p_CompanyId": companyId,
            "p_page_id": documentId,
            "p_PageNumber": 1,
            "p_RowsOfPage": 100
        }
    };
    const responseData = await spService.storedProcedureComplianceDb(data, companyId, { isOpenApi: false }, String(user_access_token ?? ""));
    const webDocuements = responseData?.data ?? [];
    return webDocuements;
}
