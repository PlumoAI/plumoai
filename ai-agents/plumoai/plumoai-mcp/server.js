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
const recordListFilterConditionSchema = z.object({
    field_id: z.string().min(1).describe("Required encrypted field ID for addon filter conditions."),
    field_datatype: z
        .enum(["text", "team", "record", "user", "date", "datetime", "number"])
        .describe("Filter datatype — must match the field type from project_table_fields. " +
        "Per-type allowed operators: " +
        "text → = (Equal To), <> (Not Equal To), like (Contains); " +
        "team → in (Has any of), not in (Has none of); " +
        "record → =, <>; " +
        "user → =, <>; " +
        "date → =, <>, >, <, >=, <=; " +
        "datetime → =, <>, >, <, >=, <=; " +
        "number → =, <>, >, <, >=, <=."),
    field_operator: z
        .enum(["=", "<>", "like", "in", "not in", ">", "<", ">=", "<="])
        .describe("Filter operator sent to the query API. **Contains / substring on text:** always use `like` (do not use `=` for contains). " +
        "For `like`, pass the plain substring in `search_value1` — do **not** wrap with `%` wildcards (the server strips `%` if present). " +
        "UI label → value: Equal To → `=`, Not Equal To → `<>`, Contains → `like`, " +
        "Has any of → `in`, Has none of → `not in`, " +
        "Greater Than → `>`, Less Than → `<`, Greater Than Equal To → `>=`, Less Than Equal To → `<=`."),
    search_value1: z
        .string()
        .describe("Primary filter value. For **contains** on a text field: set `field_operator` to `like` and put the plain substring here (no `%` wildcards)."),
    search_value2: z
        .string()
        .optional()
        .describe("Optional secondary filter value when the API expects an extra operand."),
});
const recordListFilterGroupSchema = z.lazy(() => z.object({
    logical_operator: z.enum(["AND", "OR"]).describe("How to combine the nested conditions."),
    conditions: z
        .array(z.union([recordListFilterConditionSchema, recordListFilterGroupSchema]))
        .min(1)
        .describe("List of filter conditions or nested groups."),
}));
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
        ? `1. Call **project_tables_list**(projectId) with the encrypted project_id from project_list. From the response, identify **table_id** of the table named **${t}**. Pass that \`table_id\` as-is into record_list (required).`
        : `1. Call **project_tables_list**(projectId) with the encrypted project_id from project_list.\n2. From the response, each table has **\`table_id\`** and **\`table_name\`**. Pick the target table and pass its **\`table_id\`** as-is into record_list's \`table_id\` (required).`;
    const statusStep = s
        ? `1. Call **project_table_status_list**(projectId, tableId) or **record_list** once to get records with status info. From the response, identify **status_id** of the status named **${s}**. Use that \`status_id\` in record_list. **Omit** for all statuses.`
        : `1. Call **record_list** once with \`project_id\` and \`table_id\`; each record has **\`status_id\`** and **\`status_name\`**.\n2. Or use **project_table_status_list**(projectId, tableId) if it returns status IDs.\n3. Use one of those **\`status_id\`** values in a later record_list call. **Omit** for all statuses.`;
    const flowProject = p
        ? `**project_list()** → Identify project_id of project **${p}** (encrypted).`
        : `**project_list()** → Choose project, note \`project_id\` (encrypted).`;
    const flowTable = t
        ? `**project_tables_list(projectId)** → Identify table_id of table **${t}** (encrypted).`
        : `**project_tables_list(projectId)** → Choose table, note \`table_id\` (encrypted).`;
    const flowStatus = s
        ? `**record_list(...)** → If filtering by status, use status_id of status **${s}** from project_table_status_list or a record.`
        : `**record_list(project_id, table_id [, status_id] [, isIncludeEmptyFields])** → Get records. Use \`status_id\` from a record for a later filtered call. For narrowest result: \`project_id\` + \`table_id\` + \`status_id\`.`;
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
| **API** | \`POST /api/v1/records/query\` |
| **Base URL** | \`${plumoApiV1Base()}/api\` |

---

## 2. Parameters Overview

| Parameter | Required | Type | Purpose |
|-----------|----------|------|---------|
| \`project_id\` | **Yes** | string (encrypted) | Which project's records to fetch. |
| \`table_id\` | **Yes** | string (encrypted) | Which table/work item type to fetch records from. |
| \`status_id\` | No | string (encrypted) | Filter by **workflow status** only. Pass as this parameter — **not** inside \`filter\`. |
| \`filter\` | No | object | Addon filter tree for custom fields (AND/OR). **Not** for workflow status (use \`status_id\`). |
| \`isIncludeEmptyFields\` | No | boolean | If \`true\`, each record includes all field keys (with \`null\` where empty). |
| \`page\` | No | number | Page number for pagination (1-based). |
| \`limit\` | No | number | Page size (how many records per page). |
| \`order\` | No | string | Sort order, for example \`modified_desc\`. |
| \`loadPartialData\` | No | boolean | Whether to request partial record data from the query API. |
| \`fieldsToDisplay\` | No | string[] | Encrypted field IDs or system field keys to include in the query response. |
| \`title\` | No | string | Keyword search on record title; sent as \`"title": "<keyword>"\` in the query body. |

All IDs are **encrypted strings**. Use them exactly as returned by the APIs; do not use numeric IDs for this endpoint.

---

## 3. How to Resolve Each Parameter

### 3.1 \`project_id\` (required)

**Purpose:** Identifies the project whose records you want.

**Where to get it:**

${projectStep}

---

### 3.2 \`table_id\` (required)

**Purpose:** Identifies the table (work item type) whose records to fetch, e.g. "Outreach", "Tasks", "Leads".

**Where to get it:**

${tableStep}

---

### 3.3 \`status_id\` (optional)

**Purpose:** Restricts records to one **workflow status** (e.g. "New", "In Progress", "Done"). Pass the encrypted id as the top-level \`status_id\` argument to **record_list** (it is included in the query body). **Do not** put workflow status conditions inside the addon \`filter\` object.

**Where to get it:**

${statusStep}

---

### 3.4 \`filter\` (optional)

**Purpose:** Applies addon filters such as text search, numeric comparisons, date ranges, user filters, and nested AND/OR groups on **custom fields** (by \`field_id\`). **Not** for workflow status — use \`status_id\` (§3.3).

**How to build it:**

- **Do not** use \`filter\` for workflow status; always use top-level \`status_id\` for that.
- Use \`field_id\` (encrypted) for **all** addon filter conditions.
- To get the correct encrypted \`field_id\` values, call **project_table_fields(projectId, tableId)** and use \`data[].field_id\` from the response.
- Allowed \`field_datatype\`: \`text\`, \`team\`, \`record\`, \`user\`, \`date\`, \`datetime\`, \`number\` (match the field type from **project_table_fields**).
- Allowed \`field_operator\` (values): \`=\`, \`<>\`, \`like\`, \`in\`, \`not in\`, \`>\`, \`<\`, \`>=\`, \`<=\` — use only operators valid for that datatype (see tool schema descriptions).
- **Contains / substring (text):** use \`field_datatype: "text"\` + \`field_operator: "like"\` + \`search_value1: "CEO"\` — \`like\` is how the API expresses “contains”; do **not** use \`=\` for substring matching.
- For \`field_operator: "like"\`, **do not** pass special characters like \`%\` — just pass the substring in \`search_value1\`.
- Combine conditions with \`logical_operator: "AND"\` or \`"OR"\`, and nest groups as needed.

---

### 3.5 \`title\` (optional)

**Purpose:** Simple keyword search on the record title without building a \`filter\` object. The query API receives \`"title": "<your keyword>"\` (for example \`"title": "alpha"\`).

**When to use:** Prefer \`title\` for a quick title search. Use \`filter\` when you need operators/AND/OR nesting across custom fields (by \`field_id\`).

---

### 3.6 \`isIncludeEmptyFields\` (optional)

**Purpose:** When \`true\`, every record includes all field keys with \`null\` where empty. Set \`true\` or \`false\` (or omit) as needed.

---

### 3.7 \`page\` and \`limit\` (optional)

**Purpose:** Control pagination when listing records.

- \`page\` — which page of results to fetch (1, 2, 3, ...).
- \`limit\` — how many records per page.

Example HTTP call equivalent:

\`POST /v1/records/query\` with body \`{ "project_id": "<project_id>", "page": 2, "limit": 10 }\`

If you omit these, the API uses its default pagination.

## 4. Filtering: When to Use What

| Goal | Use this filter | Get value from |
|------|-----------------|----------------|
| Records in one project | \`project_id\` (required) | **project_list** → \`data[].project_id\` |
| Records in one workspace | Filter projects first | **project_list**(workspace_id) → then use \`project_id\` |
| Records in one table | \`table_id\` (required) | **project_tables_list**(projectId) → \`data[].table_id\` |
| Records in one workflow status | \`status_id\` (optional) | **project_table_status_list** or prior **record_list** → encrypted \`status_id\` (**not** inside \`filter\`) |
| Records matching advanced conditions | \`filter\` (optional) | Build nested conditions with \`field_id\` |
| Keyword search on title | \`title\` (optional) | Pass the search string, e.g. \`title: "alpha"\` |
| Same fields on every record | \`isIncludeEmptyFields: true\` | N/A |
| Return only specific fields | \`fieldsToDisplay\` | Use encrypted field IDs or system field keys |
| Paginate results | \`page\`, \`limit\` | N/A (you decide page/size) |

**Good practice:** Narrow in order — project → table → \`status_id\` (if filtering by workflow status) → \`filter\` for custom-field conditions. Do **not** mix workflow status into \`filter\`.

---

## 5. Combining Filters (Examples)

- **Records in one table:** \`record_list(project_id: "<from project_list>", table_id: "<from project_tables_list>")\`
- **All records in one workflow status:** \`record_list(project_id: "...", table_id: "...", status_id: "<from project_table_status_list or record_list>")\` — use top-level \`status_id\`, not \`filter\`.
- **Title keyword "alpha" (shortcut):** \`record_list(project_id: "...", table_id: "...", title: "alpha")\`
- **One table with advanced filters:** \`record_list(project_id: "...", table_id: "...", filter: { logical_operator: "AND", conditions: [...] })\`
- **Full list with all field keys:** \`record_list(project_id: "...", table_id: "...", isIncludeEmptyFields: true)\` (optionally add \`status_id\`).
- **Second page of 10 records:** \`record_list(project_id: "...", table_id: "...", page: 2, limit: 10)\`.

---

## 6. Recommended Flow

1. ${flowProject}
2. ${flowTable}
3. If filtering by workflow status, resolve \`status_id\` and pass it as **top-level** \`status_id\` (never inside \`filter\`). Then build addon \`filter\` only if needed (or pass \`title\` for a simple title keyword search). Call \`record_list\` with \`project_id\`, \`table_id\`, optional \`status_id\`, and optional \`title\`, \`filter\`, \`fieldsToDisplay\`, \`order\`, \`page\`, \`limit\`.

---

## 7. Response Shape

Tool returns \`{ success, message, data: records }\`. Each record has keys mapped to field names where possible. \`record_id\`, \`status_id\`, \`table_id\` in the response are encrypted and can be reused. For **detailed_record** use the encrypted \`record_id\`.

---

## 8. Quick Reference

| Parameter | Source | Action |
|-----------|--------|--------|
| \`project_id\` | **project_list** → \`data[].project_id\` | Copy encrypted string as-is. |
| \`table_id\` | **project_tables_list**(projectId) → \`data[].table_id\` | Copy encrypted string as-is (required). |
| \`status_id\` | **project_table_status_list** or **record_list** → \`status_id\` | Copy encrypted string as-is for workflow status; **do not** put status inside \`filter\`. |
| \`filter\` | You build it | Use \`field_id\` with nested AND/OR conditions (custom fields only). |
| \`title\` | User keyword | Pass plain text for title keyword search. |
| \`isIncludeEmptyFields\` | N/A | Set \`true\` or \`false\` (or omit). |
| \`page\` | N/A | 1, 2, 3, ... (page number). |
| \`limit\` | N/A | Page size (e.g. 10, 20, 50). |

---

## 9. Common Mistakes to Avoid

- Do **not** use numeric project/table IDs; use **encrypted** \`project_id\` and \`table_id\`.
- Do **not** guess \`status_id\`; get it from a record or project_table_status_list.
- Do **not** filter workflow status via the addon \`filter\` object — use top-level \`status_id\` only.
- Use the **same** \`project_id\` for project_tables_list and record_list when filtering by table.
- **detailed_record** expects the encrypted \`record_id\` from the list.
- Prefer server-side filtering (\`table_id\`, \`status_id\`) over fetching all and filtering client-side.
- For \`filter\` **contains** on text fields: use \`field_operator: "like"\` with a plain substring in \`search_value1\` — do **not** pass \`%\` (e.g. \`"CEO"\`, not \`"%CEO%"\`).
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
- \`recordFieldValues\` = array of items: \`{ field_id, value }\` (the server sends \`field_key: null\` on each item to the API)

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
- You MUST NOT output an item with a null/empty \`field_id\`
- Skip any field where \`value\` is null / undefined / empty-string (do not include placeholder entries)

---
## 🔒 Evaluator / reconstruction rules (MUST FOLLOW)

If candidate tool_args contains \`recordFieldValues\`, treat it as UNTRUSTED:
You MUST ignore it and rebuild \`recordFieldValues\` from scratch using:
- project_table_fields
- the user request values

Hard failure: if any item has a null/empty \`field_id\` → discard and rebuild the entire \`recordFieldValues\` array.

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
- Optional: \`recordFieldValues\` = array of items: \`{ field_id, value }\` (the server sends \`field_key: null\` on each item to the API)

Never put status inside \`recordFieldValues\`. If you want a status change, pass \`status_id\` to **record_update** (it applies the workflow status via the HTTP status endpoint).

---
## Minimal flow

1. Get \`record_id\` (encrypted) from record_list / your planning flow.
2. Use \`project_id\` and \`table_id\` for the target record/table.
3. Call project_table_fields(project_id, table_id) to get valid \`field_id\` values (and metadata such as \`field_key\` for your own mapping only).
4. (Conditional) If you want to update fields, build \`recordFieldValues\` from the user request (skip null/empty values).
5. (Conditional, status change only) If you want to change workflow status, get \`status_id\` from project_table_status_list and pass it as \`status_id\` to record_update.
6. Call record_update(\`record_id\`, \`project_id\`, \`table_id\`, \`recordFieldValues\`?, \`status_id\`?).

---
## 🔒 Hard constraints (MUST FOLLOW)

- \`field_id\` is REQUIRED in every \`recordFieldValues\` item (non-null, non-empty).
- DO NOT include any field where \`value\` is null/undefined/empty-string.
- Only include fields that have actual user-provided non-empty \`value\`.
- You may omit \`recordFieldValues\` entirely for status-only updates.

---
## 🔒 Evaluator / reconstruction rules (MUST FOLLOW)

If candidate tool_args contains \`recordFieldValues\`, treat it as UNTRUSTED:
You MUST ignore it and rebuild \`recordFieldValues\` from scratch using:
- project_table_fields
- User request values

Hard failure: if any item has a null/empty \`field_id\` → discard and rebuild the entire array.

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
    description: "STEP 2 OF 2 — Fetches records for a project table using the query API, with pagination, projected fields, and addon filters.\n" +
        "REQUIRED CALL ORDER: (1) project_list → project_id (ENCRYPTED string), (2) project_tables_list(projectId) → table_id (ENCRYPTED string), (3) record_list(project_id, table_id [, status_id] [, title] [, filter]).\n\n" +
        "Workflow **status** filtering: pass encrypted `status_id` as the **top-level** argument `status_id`. Do **not** encode workflow status inside the addon `filter` object.\n\n" +
        "Optional `title` passes a keyword for record title search in the query body (`\"title\": \"...\"`).\n" +
        "For **contains** on a text custom field in `filter`, use `field_operator: \"like\"` with the plain substring in `search_value1` (not `=`). Do **not** add `%` wildcards — e.g. `\"CEO\"`, not `\"%CEO%\"` (any `%` in the value is stripped before the API call).\n\n" +
        "RETURN VALUE NOTE: Each record contains TWO id fields:\n" +
        "- record_id (ENCRYPTED string) → pass to record_update or change_record_status\n" +
        "- id (integer / numeric) → present, but **not used by detailed_record** in this server.\n\n" +
        "Use `record_id` (encrypted) with detailed_record.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
        status_id: z
            .string()
            .optional()
            .describe("Optional. ENCRYPTED workflow status id from project_table_status_list or a prior record_list. Pass as this top-level parameter only — do **not** put status filtering inside `filter`."),
        recordId: z.string().optional().describe("Legacy optional record filter. Passed through when provided."),
        isIncludeEmptyFields: z.boolean().optional().describe("Optional. When true, every record includes all field keys (with null where there is no value)."),
        page: z.number().int().positive().optional().describe("Optional. Page number for pagination (1-based). If omitted, server default is used."),
        limit: z.number().int().positive().optional().describe("Optional. Page size for pagination. If omitted, server default is used."),
        order: z
            .string()
            .optional()
            .describe("Optional sort order for the query API, for example `modified_desc`."),
        loadPartialData: z
            .boolean()
            .optional()
            .describe("Optional. Query API flag to load partial record data."),
        fieldsToDisplay: z
            .array(z.string().min(1))
            .optional()
            .describe("Optional list of encrypted field IDs or system field keys to request from the query API."),
        filter: recordListFilterGroupSchema
            .optional()
            .describe("Optional addon filter tree for **custom fields** (by field_id). Supports nested AND/OR groups. " +
            "Operators: =, <>, like, in, not in, >, <, >=, <= (must pair with a valid field_datatype per tool schema). " +
            "For text **contains**, use `field_operator: \"like\"` and a plain `search_value1` substring — no `%` wildcards. " +
            "Do **not** use `filter` for workflow status — use top-level `status_id` instead."),
        title: z
            .string()
            .optional()
            .describe("Optional. Keyword search on record title; sent in the query body as `\"title\": \"<keyword>\"` (e.g. `\"alpha\"`). Omit for no title filter."),
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
            const payload = {
                project_id: args.project_id,
                table_id: args.table_id,
            };
            if (args.status_id != null)
                payload.status_id = args.status_id;
            if (args.recordId != null)
                payload.recordId = args.recordId;
            if (args.page != null)
                payload.page = args.page;
            if (args.limit != null)
                payload.limit = args.limit;
            if (args.order != null)
                payload.order = args.order;
            if (args.loadPartialData != null)
                payload.loadPartialData = args.loadPartialData;
            if (args.fieldsToDisplay != null)
                payload.fieldsToDisplay = args.fieldsToDisplay;
            const normalizeLikeValue = (s) => s.replace(/%/g, "").trim();
            const normalizeRecordListFilterGroup = (g) => ({
                logical_operator: g.logical_operator,
                conditions: g.conditions.map((c) => {
                    if ("logical_operator" in c)
                        return normalizeRecordListFilterGroup(c);
                    if (c.field_operator === "like") {
                        return { ...c, search_value1: normalizeLikeValue(c.search_value1) };
                    }
                    return c;
                }),
            });
            if (args.filter != null)
                payload.filter = normalizeRecordListFilterGroup(args.filter);
            if (args.title != null && args.title.trim() !== "")
                payload.title = args.title.trim();
            if (args.isIncludeEmptyFields != null)
                payload.isIncludeEmptyFields = args.isIncludeEmptyFields;
            const response = await axios.post(`${plumoApiV1Base()}/records/query`, payload, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}`,
                    companyId: `[${session?.companyId}]`,
                    "Content-Type": "application/json",
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
                    data: { records_count: finalRecords?.length, records: finalRecords },
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
    description: "UTILITY — Lists fields for a project table. REQUIRED BEFORE record_create / record_update when you need field IDs. ID TYPES: projectId ENCRYPTED string, tableId ENCRYPTED string. Returns ENCRYPTED field_id (proj_field_id) plus field_key (physical/system key when present), type, is_required, and value options for picklists. Use these results to build recordFieldValues.\n\n" +
        "Optional pagination: pass `page` and/or `limit` (1-based page, page size) — sent as query params on `GET .../fields?page=&limit=`.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is."),
        tableId: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
        page: z
            .number()
            .int()
            .positive()
            .optional()
            .describe("Optional. Page number for field list pagination (1-based). Omitted = API default."),
        limit: z
            .number()
            .int()
            .positive()
            .optional()
            .describe("Optional. Page size for field list pagination. Omitted = API default."),
    }),
    execute: async (args, { session }) => {
        try {
            const fieldsPath = `${plumoApiV1Base()}/projects/${encodeURIComponent(args.projectId)}/tables/${encodeURIComponent(args.tableId)}/fields`;
            const query = new URLSearchParams();
            if (args.page != null) {
                query.set("page", String(args.page));
            }
            else {
                query.set("page", "1");
            }
            if (args.limit != null) {
                query.set("limit", String(args.limit));
            }
            else {
                query.set("limit", "1000");
            }
            const url = query.toString() ? `${fieldsPath}?${query.toString()}` : fieldsPath;
            const response = await axios.get(url, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    Accept: "application/json",
                },
            });
            const rawData = response.data?.data ?? response.data;
            const items = Array.isArray(rawData.fields) ? rawData.fields : [];
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
        "Array of objects. Each object sets one field: \`{ field_id, value }\` only. The server sends \`field_key: null\` on each item to the API. Only include fields you want to set.\n" +
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
        "  { \"field_id\": \"ZmllbGQx\", \"value\": \"Fix login bug\" },\n" +
        "  { \"field_id\": \"ZmllbGQy\", \"value\": \"high\" },\n" +
        "  { \"field_id\": \"ZmllbGQz\", \"value\": \"2026-04-20\" }\n" +
        "]\n\n" +
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by project_table_fields. Do not invent values.",
    parameters: z.object({
        project_id: z.string().describe("Required. ENCRYPTED project_id string from project_list. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from project_tables_list(projectId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from project_table_status_list(projectId [, tableId]). Pass as-is."),
        recordFieldValues: z
            .array(z.object({
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from project_table_fields. MUST NOT be null/empty."),
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
                recordFieldValues: recordFieldValues.map(({ field_id, value }) => ({
                    field_id,
                    field_key: null,
                    value,
                })),
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
        "(1) record_list(project_id, table_id) → locate the record by name/title, note its encrypted record_id string.\n" +
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
    name: "record_add_comment",
    description: "Adds a comment to an existing record.\n\n" +
        "REQUIRED: encrypted `record_id` from **record_list** (same id used with **detailed_record** / **record_update**).\n\n",
    parameters: z.object({
        record_id: z.string().min(1).describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
        comment: z.string().min(1).describe("Required. Comment text (may include HTML entities or tags as supported by the product)."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const payload = {
                comment: args.comment,
                mention_userid: "",
                mention_teamid: "",
            };
            const response = await axios.post(`${plumoApiV1Base()}/records/${encodeURIComponent(args.record_id)}/comments`, payload, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}`,
                    companyId: `[${session?.companyId}]`,
                    "Content-Type": "application/json",
                },
            });
            return {
                type: "text",
                text: JSON.stringify(response.data?.data ?? response.data ?? { success: true }),
            };
        }
        catch (error) {
            return {
                type: "text",
                text: JSON.stringify({
                    success: false,
                    error: error.response?.data ?? error.message ?? "Unknown error occurred",
                }),
            };
        }
    },
});
server.addTool({
    name: "record_update",
    description: "STEP 4 OF 4 — Updates an existing record (field values and/or workflow status). REQUIRED CALL ORDER: (1) project_list → project_id, (2) project_tables_list(projectId) → table_id, (3) record_list(project_id, table_id) → record_id, (4) project_table_fields(projectId, tableId) → field_id (when updating fields), (5) project_table_status_list(projectId [, tableId]) → status_id (when changing status), (6) record_update(record_id, project_id, table_id, recordFieldValues?, status_id?). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "Do not put workflow status inside recordFieldValues — pass status_id separately. Prefer **record_update** with status_id over **change_record_status**.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects, or null/omit/[] for a status-only update. Each object updates one field: \`{ field_id, value }\` only (\`field_id\` from project_table_fields, non-null, non-empty). The server sends \`field_key: null\` on each item to the API.\n\n" +
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
        "  { \"field_id\": \"ZmllbGQx\", \"value\": \"Fix login bug (updated)\" },\n" +
        "  { \"field_id\": \"ZmllbGQy\", \"value\": \"high\" },\n" +
        "  { \"field_id\": \"ZmllbGQz\", \"value\": \"2026-04-22\" }\n" +
        "]\n\n" +
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by project_table_fields. Do not invent values.",
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
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from project_table_fields. Pass as-is."),
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
            const invalidFieldIdItems = recordFieldValues.filter((it) => {
                const id = it?.field_id ?? null;
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
                    recordFieldValues: recordFieldValues.map(({ field_id, value }) => ({
                        field_id,
                        field_key: null,
                        value,
                    })),
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
                text: JSON.stringify({ "success": true, "message": "Project table status retrieved successfully", "data": response.data.data?.transitions }),
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
