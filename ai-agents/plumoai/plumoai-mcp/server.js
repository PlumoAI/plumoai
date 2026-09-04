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
const RECORD_LIST_FIELD_OPERATOR_LABELS = [
    "Equal To",
    "Not Equal To",
    "Contains",
    "Has any of",
    "Has none of",
    "Greater Than",
    "Less Than",
    "Greater Than Equal To",
    "Less Than Equal To",
];
const RECORD_LIST_FIELD_OPERATOR_TO_API = {
    "Equal To": "=",
    "Not Equal To": "<>",
    Contains: "like",
    "Has any of": "in",
    "Has none of": "not in",
    "Greater Than": ">",
    "Less Than": "<",
    "Greater Than Equal To": ">=",
    "Less Than Equal To": "<=",
};
const recordListFilterConditionSchema = z.object({
    field_id: z.string().min(1).describe("Required encrypted field ID for addon filter conditions."),
    field_datatype: z
        .enum(["text", "team", "record", "user", "date", "datetime", "number"])
        .describe("Filter datatype — must match the field type from ai_employee_table_fields. " +
        "Per-type allowed operators (use these labels in field_operator): " +
        "text → Equal To, Not Equal To, Contains; " +
        "team → Has any of, Has none of; " +
        "record → Equal To, Not Equal To; " +
        "user → Equal To, Not Equal To; " +
        "date → Equal To, Not Equal To, Greater Than, Less Than, Greater Than Equal To, Less Than Equal To; " +
        "datetime → Equal To, Not Equal To, Greater Than, Less Than, Greater Than Equal To, Less Than Equal To; " +
        "number → Equal To, Not Equal To, Greater Than, Less Than, Greater Than Equal To, Less Than Equal To."),
    field_operator: z
        .enum(RECORD_LIST_FIELD_OPERATOR_LABELS)
        .describe("Filter operator label. Use the human-readable text (not symbols). " +
        "**Contains / substring on text:** always use `Contains` (do not use `Equal To` for contains). " +
        "For `Contains`, pass the plain substring in `search_value1` — do **not** wrap with `%` wildcards (the server strips `%` if present). " +
        "Allowed labels: Equal To, Not Equal To, Contains, Has any of, Has none of, " +
        "Greater Than, Less Than, Greater Than Equal To, Less Than Equal To. " +
        "These are mapped to API operators (=, <>, like, in, not in, >, <, >=, <=) before the query is sent."),
    search_value1: z
        .string()
        .describe("Primary filter value. For **contains** on a text field: set `field_operator` to `Contains` and put the plain substring here (no `%` wildcards)."),
    search_value2: z
        .string()
        .optional()
        .describe("Optional secondary filter value when the API expects an extra operand."),
});
const recordListFilterGroupSchema = z.lazy(() => z.object({
    logical_operator: z
        .enum(["AND", "OR", "and", "or", "And", "Or"])
        .describe("How to combine the nested conditions."),
    conditions: z
        .array(z.union([recordListFilterConditionSchema, recordListFilterGroupSchema]))
        .min(1)
        .describe("List of filter conditions or nested groups."),
}));
function withBearerPrefix(value) {
    const t = value.trim();
    if (!t)
        return t;
    return /^bearer\s+/i.test(t) ? t : `Bearer ${t}`;
}
function normalizeOutputKey(value) {
    return value.trim().toLowerCase().replace(/\s+/g, "_");
}
/** Normalize fields metadata from records/query and related APIs (property names vary). */
function normalizeRecordFields(fields) {
    if (!Array.isArray(fields))
        return [];
    return fields.map((f) => ({
        field_id: f?.field_id ?? f?.proj_field_id,
        field_key: f?.field_key ?? f?.record_actual_fieldname ?? null,
        field_name: f?.field_name,
        type: f?.type ?? f?.field_type ?? f?.datatype,
    }));
}
function buildKeyToFieldNameMap(fields) {
    const keyToFieldName = {};
    for (const f of fields) {
        const name = f.field_name ?? "";
        if (f.field_id)
            keyToFieldName[f.field_id] = name;
        if (f.field_key)
            keyToFieldName[f.field_key] = name;
    }
    return keyToFieldName;
}
/** Keys that identify phone fields (raw API key, display name, or normalized output key). */
function buildPhoneFieldKeySet(fields) {
    const phoneKeys = new Set();
    for (const f of fields) {
        if ((f.type ?? "").toString().trim().toLowerCase() !== "phone")
            continue;
        if (f.field_id)
            phoneKeys.add(f.field_id);
        if (f.field_key)
            phoneKeys.add(f.field_key);
        if (f.field_name) {
            phoneKeys.add(f.field_name);
            phoneKeys.add(normalizeOutputKey(f.field_name));
        }
    }
    return phoneKeys;
}
function isPhoneJsonObject(parsed) {
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        return false;
    const keys = Object.keys(parsed).map((k) => k.toLowerCase());
    return keys.includes("phonenumber") || keys.includes("countrycode");
}
/** Unwrap JSON-encoded strings (handles double-encoded phone values from the API). */
function tryUnwrapJson(value) {
    let current = value;
    for (let i = 0; i < 4; i++) {
        if (typeof current !== "string")
            return current;
        const t = current.trim();
        if (!t)
            return current;
        try {
            current = JSON.parse(t);
            continue;
        }
        catch {
            const start = t.indexOf("{");
            const end = t.lastIndexOf("}");
            if (start >= 0 && end > start) {
                try {
                    current = JSON.parse(t.slice(start, end + 1));
                    continue;
                }
                catch {
                    return value;
                }
            }
            return value;
        }
    }
    return current;
}
/**
 * Parse phone JSON strings into objects.
 * - Known phone fields (type === "phone"), or
 * - Values that unwrap to a phone-shaped object ({ countrycode, phonenumber })
 * Handles normal and double-encoded JSON strings from the records API.
 */
function coercePhoneFieldValue(value, isPhoneField) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
        if (isPhoneField || isPhoneJsonObject(value))
            return value;
        return value;
    }
    if (typeof value !== "string")
        return value;
    const unwrapped = tryUnwrapJson(value);
    if (unwrapped && typeof unwrapped === "object" && !Array.isArray(unwrapped)) {
        if (isPhoneField || isPhoneJsonObject(unwrapped))
            return unwrapped;
    }
    return value;
}
function transformRecordKeys(record, keyToFieldName, phoneKeys = new Set()) {
    const transformedRecord = {};
    for (const key of Object.keys(record)) {
        const mappedKey = keyToFieldName[key] ?? key;
        const outKey = normalizeOutputKey(mappedKey);
        const isPhoneField = phoneKeys.has(key) || phoneKeys.has(mappedKey) || phoneKeys.has(outKey);
        transformedRecord[outKey] = coercePhoneFieldValue(record[key], isPhoneField);
    }
    return transformedRecord;
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
    name: "PlumoAI AI Employee MCP Server",
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
        console.log("auth: ", authHeader);
        try {
            const cacheKey = `${authHeader}`;
            const now = Date.now();
            const cached = oauthMeCache.get(cacheKey);
            let me = null;
            if (cached && cached.expiresAt > now) {
                me = cached.value;
            }
            else {
                var response = await axios.get(`https://api.plumoai.com/auth/oauth/me`, {
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
            return { user_access_token: authHeader.slice(7), expires_in: 900, companyId: String(companyId), userId: me.userId };
        }
        catch (error) {
            console.log("error: ", error);
            throw new Response(null, {
                status: 401,
                statusText: error.message ?? "Invalid OAuth token",
            });
        }
    },
});
server.addTool({
    name: "ai_employee_list",
    description: "Fetch all AI Employees for the authenticated user. Returns workspace, workspace_id, project_description, project_id (encrypted), project_name, template_name — these are the raw backend field names (unchanged). Optionally filter by workspace_id (encrypted). Use the returned project_id (encrypted) value as-is as the ai_employee_id argument when calling ai_employee_tables_list or record_list. (Note: Also fetch sprints for Scrum AI Employees after getting a specific AI Employee)\n\n" +
        "Returns workspace, workspace_id (ENCRYPTED string), project_description, project_id (ENCRYPTED string), project_name, template_name, and a numeric fid (integer) — backend field names, unchanged.\n\n" +
        "ID USAGE GUIDE:\n" +
        "- project_id (ENCRYPTED string) → pass as ai_employee_id / aiEmployeeId to: record_list, ai_employee_tables_list, ai_employee_table_fields, ai_employee_table_pipelines, ai_employee_table_pipeline_status_list, record_create, record_update, change_record_status\n" +
        "- numeric fid (integer) → Scrum/sprint utilities only",
    parameters: z.object({
        workspace_id: z.string().optional().describe("Optional. Filter AI Employees by workspace. Use workspace_id (encrypted) from a previous ai_employee_list response."),
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
            let aiEmployees = Array.isArray(response.data?.data) ? response.data.data : (Array.isArray(response.data) ? response.data : []);
            if (args.workspace_id?.trim()) {
                const wid = args.workspace_id.trim();
                aiEmployees = aiEmployees.filter((p) => p.workspace_id === wid);
            }
            const payload = response.data?.success !== undefined
                ? { ...response.data, data: aiEmployees }
                : aiEmployees;
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
    description: "STEP 3 OF 3 — Fetches records for an AI Employee table using the query API, with pagination, projected fields, and addon filters.\n" +
        "REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id (ENCRYPTED string), (2) ai_employee_tables_list(aiEmployeeId) → table_id (ENCRYPTED string), (3) ai_employee_table_pipelines(aiEmployeeId, tableId) → pipeline_id (when filtering by pipeline or workflow status), (4) record_list(ai_employee_id, table_id [, pipeline_id] [, status_id] [, title] [, filter]).\n\n" +
        "Workflow **pipeline** scoping: pass encrypted `pipeline_id` as the **top-level** argument `pipeline_id` (from ai_employee_table_pipelines). Do **not** encode pipeline scoping inside the addon `filter` object.\n" +
        "Workflow **status** filtering: pass encrypted `status_id` as the **top-level** argument `status_id`. When filtering by status on a pipelined table, also pass the matching `pipeline_id`. Do **not** encode workflow status inside the addon `filter` object.\n\n" +
        "Optional `title` passes a keyword for record title search in the query body (`\"title\": \"...\"`).\n" +
        "For **contains** on a text custom field in `filter`, use `field_operator: \"Contains\"` with the plain substring in `search_value1` (not `Equal To`). Do **not** add `%` wildcards — e.g. `\"CEO\"`, not `\"%CEO%\"` (any `%` in the value is stripped before the API call).\n\n" +
        "RETURN VALUE NOTE: Each record contains TWO id fields:\n" +
        "- record_id (ENCRYPTED string) → pass to record_update or change_record_status\n" +
        "- id (integer / numeric) → present, but **not used by detailed_record** in this server.\n\n" +
        "Use `record_id` (encrypted) with detailed_record.",
    parameters: z.object({
        ai_employee_id: z.string().describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is."),
        pipeline_id: z
            .string()
            .optional()
            .describe("Optional. ENCRYPTED pipeline id from ai_employee_table_pipelines(aiEmployeeId, tableId). Pass as this top-level parameter only — sent in the query body as `\"pipeline_id\": \"...\"`. Do **not** put pipeline scoping inside `filter`. Use when scoping to one pipeline or when filtering by workflow status on a pipelined table."),
        status_id: z
            .string()
            .optional()
            .describe("Optional. ENCRYPTED workflow status id from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId) or a prior record_list. Pass as this top-level parameter only — do **not** put status filtering inside `filter`. When filtering by status on a pipelined table, also pass the matching `pipeline_id`."),
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
            "Operators (use labels, not symbols): Equal To, Not Equal To, Contains, Has any of, Has none of, Greater Than, Less Than, Greater Than Equal To, Less Than Equal To (must pair with a valid field_datatype per tool schema). " +
            "For text **contains**, use `field_operator: \"Contains\"` and a plain `search_value1` substring — no `%` wildcards. " +
            "Do **not** use `filter` for pipeline or workflow status — use top-level `pipeline_id` and `status_id` instead."),
        title: z
            .string()
            .optional()
            .describe("Optional. Keyword search on record title; sent in the query body as `\"title\": \"<keyword>\"` (e.g. `\"alpha\"`). Omit for no title filter."),
        // --- Payload shaping (client-side only; not sent to POST /records/query) ---
        //
        // Applied AFTER the API returns and each record's keys are renamed from encrypted
        // field_id / field_key to human-readable field_name (e.g. "Name", "Primary Email").
        // Use the same names you see on a transformed record_list row, not raw field_id values.
        //
        // select_fields — whitelist mode: keep only listed keys (+ record_id always).
        //   Example: select_fields: ["record_id", "Name", "Primary Email"]
        //   → each record is { record_id, Name, "Primary Email" } (other columns dropped).
        //
        // omit_fields — blacklist mode: drop listed keys; keep everything else (+ record_id).
        //   Example: omit_fields: ["Description", "Modified By"]
        //   → those keys removed from every record; record_id still present.
        //
        // If both are set, select_fields wins; omit_fields is ignored.
        // Omit both to return the full transformed record (default).
        select_fields: z
            .array(z.string().min(1))
            .optional()
            .describe("Optional. Whitelist output keys after field-name transformation. Only these fields are returned per record (plus record_id, which is always kept). Names must match post-transform keys, e.g. 'Name', 'Primary Email'. Not sent to the query API."),
        omit_fields: z
            .array(z.string().min(1))
            .optional()
            .describe("Optional. Blacklist output keys after field-name transformation. Listed fields are removed from each record; record_id is never removed. Ignored when select_fields is also set. Not sent to the query API."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const payload = {
                project_id: args.ai_employee_id,
                table_id: args.table_id,
            };
            if (args.pipeline_id != null)
                payload.pipeline_id = args.pipeline_id;
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
                logical_operator: String(g.logical_operator).toUpperCase() === "OR" ? "OR" : "AND",
                conditions: g.conditions.map((c) => {
                    if ("logical_operator" in c)
                        return normalizeRecordListFilterGroup(c);
                    const field_operator = RECORD_LIST_FIELD_OPERATOR_TO_API[c.field_operator];
                    if (field_operator === "like") {
                        return { ...c, field_operator, search_value1: normalizeLikeValue(c.search_value1) };
                    }
                    return { ...c, field_operator };
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
            const fields = normalizeRecordFields(raw?.fields);
            const records = raw?.records ?? [];
            const keyToFieldName = buildKeyToFieldNameMap(fields);
            const phoneKeys = buildPhoneFieldKeySet(fields);
            const transformedRecords = records.map((rec) => transformRecordKeys(rec, keyToFieldName, phoneKeys));
            // Trim select_fields / omit_fields names; build Sets for O(1) lookup per record.
            const normalizeFieldKey = (s) => normalizeOutputKey(s);
            const selectSet = Array.isArray(args.select_fields) && args.select_fields.length > 0
                ? new Set(args.select_fields.map(normalizeFieldKey).filter((k) => k.length > 0))
                : null;
            const omitSet = Array.isArray(args.omit_fields) && args.omit_fields.length > 0
                ? new Set(args.omit_fields.map(normalizeFieldKey).filter((k) => k.length > 0))
                : null;
            // Shrink each row for the MCP response only (query API always returns full records).
            const finalRecords = selectSet || omitSet
                ? transformedRecords.map((rec) => {
                    const alwaysKeep = new Set(["record_id"]);
                    const out = {};
                    if (selectSet) {
                        // Whitelist: record_id + keys that exist on this record and were requested.
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
                    // Blacklist: copy all keys except those in omitSet; record_id cannot be omitted.
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
    name: "ai_employee_tables_list",
    description: "UTILITY — Lists AI Employee tables (work item types). ID TYPES: aiEmployeeId is ENCRYPTED string. Returns ENCRYPTED table_id values. Use: (1) ai_employee_list → get ENCRYPTED ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → get ENCRYPTED table_id. Pass these encrypted strings as-is into downstream tools (e.g. record_list, ai_employee_table_fields, ai_employee_table_pipelines, ai_employee_table_pipeline_status_list, record_create, record_update).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        aiEmployeeId: z
            .string()
            .describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is (NOT numeric)."),
    }),
    execute: async (args, { session }) => {
        try {
            const response = await axios.get(`${plumoApiV1Base()}/projects/${encodeURIComponent(args.aiEmployeeId)}/tables`, {
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
    name: "ai_employee_table_fields",
    description: "UTILITY — Lists fields for an AI Employee table. REQUIRED BEFORE record_create / record_update when you need field IDs. ID TYPES: ai_employee_id ENCRYPTED string, table_id ENCRYPTED string. Returns ENCRYPTED field_id (proj_field_id) plus field_key (physical/system key when present), type, is_required, and value options for picklists. Use these results to build recordFieldValues.\n\n" +
        "Related table-scoped utilities (same ai_employee_id + table_id): **ai_employee_table_pipelines** (automation pipelines — resolve pipeline_id first), **ai_employee_table_pipeline_status_list** (workflow statuses for a pipeline).\n\n" +
        "Optional pagination: pass `page` and/or `limit` (1-based page, page size) — sent as query params on `GET .../fields?page=&limit=`.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        ai_employee_id: z.string().describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(ai_employee_id). Pass as-is."),
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
            const fieldsPath = `${plumoApiV1Base()}/projects/${encodeURIComponent(args.ai_employee_id)}/tables/${encodeURIComponent(args.table_id)}/fields`;
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
                    message: "AI Employee table fields retrieved successfully",
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
    name: "ai_employee_table_pipelines",
    description: "UTILITY — Lists pipelines of an AI Employee table. ID TYPES: aiEmployeeId ENCRYPTED string, tableId ENCRYPTED string. REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → table_id, (3) ai_employee_table_pipelines(aiEmployeeId, tableId). Returns pipeline definitions for the table (including encrypted pipeline_id values). Pass pipeline_id as-is into **record_list** (top-level pipeline_id), **ai_employee_table_pipeline_status_list**, record_create, or record_update. Use when the user asks about automations, workflows, or pipelines tied to a specific work item type.\n\n" +
        "Related table-scoped utilities (same aiEmployeeId + tableId): **ai_employee_table_fields** (field metadata), **ai_employee_table_pipeline_status_list** (statuses for a pipeline), **record_list** (scope records by pipeline_id).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        aiEmployeeId: z
            .string()
            .describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is (NOT numeric)."),
        tableId: z
            .string()
            .describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is (NOT numeric)."),
    }),
    execute: async (args, { session }) => {
        try {
            const url = `${plumoApiV1Base()}/projects/${encodeURIComponent(args.aiEmployeeId)}/tables/${encodeURIComponent(args.tableId)}/pipelines`;
            const response = await axios.get(url, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    Accept: "application/json",
                },
            });
            console.log(response.data);
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "AI Employee table pipelines retrieved successfully",
                    data: response.data?.data ?? response.data,
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
    description: "STEP 6 OF 6 — Creates a new record. REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → table_id, (3) ai_employee_table_pipelines(aiEmployeeId, tableId) → pipeline_id, (4) ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId) → status_id, (5) ai_employee_table_fields(ai_employee_id, table_id) → field_id, (6) record_create(ai_employee_id, table_id, status_id, recordFieldValues). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects. Each object sets one field: \`{ field_id, value }\` only. The server sends \`field_key: null\` on each item to the API. Only include fields you want to set.\n" +
        "Fields where is_required=true (from ai_employee_table_fields) MUST be included.\n\n" +
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
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by ai_employee_table_fields. Do not invent values.",
    parameters: z.object({
        ai_employee_id: z.string().describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId). Pass as-is."),
        recordFieldValues: z
            .array(z.object({
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from ai_employee_table_fields. MUST NOT be null/empty."),
            value: z.any().describe("Field value to set for this record field."),
        }))
            .nonempty(),
        run_automation: z
            .boolean()
            .optional()
            .default(true)
            .describe("Optional. Defaults to true. When true, the API runs any automations configured for record creation. Pass false to skip automations."),
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
                        message: "Invalid recordFieldValues: field_id is required for every item and must not be null/empty. Rebuild recordFieldValues using ai_employee_table_fields.",
                        invalid_count: invalidFieldIdItems.length,
                    }),
                };
            }
            const payload = {
                project_id: args.ai_employee_id,
                table_id: args.table_id,
                status_id: args.status_id,
                recordFieldValues: recordFieldValues.map(({ field_id, value }) => ({
                    field_id,
                    field_key: null,
                    value,
                })),
                run_automation: args.run_automation,
            };
            const response = await axios.post(`${plumoApiV1Base()}/records`, payload, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    "Content-Type": "application/json",
                },
            });
            const raw = response.data?.data ?? {};
            const fields = normalizeRecordFields(raw?.fields);
            const record = raw?.record ?? {};
            const keyToFieldName = buildKeyToFieldNameMap(fields);
            const phoneKeys = buildPhoneFieldKeySet(fields);
            const transformedRecord = transformRecordKeys(record, keyToFieldName, phoneKeys);
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
    name: "record_bulk_create",
    description: "Creates multiple records in one call. REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → table_id, (3) ai_employee_table_pipelines(aiEmployeeId, tableId) → pipeline_id, (4) ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId) → status_id, (5) ai_employee_table_fields(ai_employee_id, table_id) → field_id, (6) record_bulk_create(ai_employee_id, table_id, status_id, records). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "records FORMAT:\n" +
        "Array of objects, one per record to create. Each object has a \`recordFieldValues\` array of \`{ field_id, value }\` items. The server sends \`field_key: null\` on each item to the API. Only include fields you want to set.\n" +
        "Fields where is_required=true (from ai_employee_table_fields) MUST be included for every record.\n\n" +
        "FIELD TYPE → VALUE FORMAT:\n" +
        "  text / string     → \"My task title\"\n" +
        "  text_multiline    → \"Multi-line\\ndescription\"\n" +
        "  number            → 42\n" +
        "  date              → \"2026-04-20\"  (ISO 8601)\n" +
        "  dropdown/select   → \"high\"  (must match a field_value_options value exactly)\n" +
        "  boolean           → true\n" +
        "  user              → \"usr_abc123\"\n\n" +
        "WORKED EXAMPLE:\n" +
        "{\n" +
        "  \"ai_employee_id\": \"...\", \"table_id\": \"...\", \"status_id\": \"...\",\n" +
        "  \"records\": [\n" +
        "    { \"recordFieldValues\": [ { \"field_id\": \"ZmllbGQx\", \"value\": \"Bulk task 1\" } ] },\n" +
        "    { \"recordFieldValues\": [ { \"field_id\": \"ZmllbGQx\", \"value\": \"Bulk task 2\" } ] }\n" +
        "  ]\n" +
        "}\n\n" +
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by ai_employee_table_fields. Do not invent values.",
    parameters: z.object({
        ai_employee_id: z.string().describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is (NOT numeric)."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId). Pass as-is."),
        records: z
            .array(z.object({
            recordFieldValues: z
                .array(z.object({
                field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from ai_employee_table_fields. MUST NOT be null/empty."),
                value: z.any().describe("Field value to set for this record field."),
            }))
                .nonempty(),
        }))
            .nonempty()
            .describe("Required. One entry per record to create."),
        run_automation: z
            .boolean()
            .optional()
            .default(false)
            .describe("Optional. Defaults to false. When true, the API runs any automations configured for record creation. Pass false to skip automations."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const invalidRecords = [];
            const records = args.records.map((rec, idx) => {
                const raw = (rec.recordFieldValues ?? []);
                const recordFieldValues = raw.filter((it) => {
                    const v = it?.value;
                    if (v === null || v === undefined)
                        return false;
                    if (typeof v === "string" && v.trim().length === 0)
                        return false;
                    return true;
                });
                const hasInvalidId = recordFieldValues.some((it) => {
                    const id = it?.field_id ?? null;
                    const hasId = typeof id === "string" ? id.trim().length > 0 : false;
                    return !hasId;
                });
                if (hasInvalidId || recordFieldValues.length === 0) {
                    invalidRecords.push(idx);
                }
                return {
                    recordFieldValues: recordFieldValues.map(({ field_id, value }) => ({
                        field_id,
                        field_key: null,
                        value,
                    })),
                };
            });
            if (invalidRecords.length > 0) {
                return {
                    type: "text",
                    text: JSON.stringify({
                        success: false,
                        message: "Invalid records: field_id is required and must not be null/empty for every recordFieldValues item, and each record must have at least one valid field value. Rebuild records using ai_employee_table_fields.",
                        invalid_record_indexes: invalidRecords,
                    }),
                };
            }
            const payload = {
                project_id: args.ai_employee_id,
                table_id: args.table_id,
                status_id: args.status_id,
                run_automation: args.run_automation,
                records,
            };
            const response = await axios.post(`${plumoApiV1Base()}/records/bulk`, payload, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    "Content-Type": "application/json",
                },
            });
            const raw = response.data?.data ?? {};
            const fields = normalizeRecordFields(raw?.fields);
            const keyToFieldName = buildKeyToFieldNameMap(fields);
            const phoneKeys = buildPhoneFieldKeySet(fields);
            const rawRecords = Array.isArray(raw?.records)
                ? raw.records
                : Array.isArray(raw)
                    ? raw
                    : [];
            const transformedRecords = rawRecords.map((rec) => transformRecordKeys(rec, keyToFieldName, phoneKeys));
            return {
                type: "text",
                text: JSON.stringify({
                    success: response.data?.success ?? true,
                    message: response.data?.message ?? "Records created successfully",
                    data: transformedRecords.length > 0 ? transformedRecords : raw,
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
        "(1) record_list(ai_employee_id, table_id [, pipeline_id]) → locate the record by name/title, note its encrypted record_id string. Pass pipeline_id when the table has multiple pipelines.\n" +
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
            const fields = normalizeRecordFields(raw?.fields);
            const records = [raw?.record].filter((it) => it !== null && it !== undefined);
            const keyToFieldName = buildKeyToFieldNameMap(fields);
            const phoneKeys = buildPhoneFieldKeySet(fields);
            const transformedRecords = records.map((rec) => transformRecordKeys(rec, keyToFieldName, phoneKeys));
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
    description: "STEP 6 OF 6 — Updates an existing record (field values and/or workflow status). REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → table_id, (3) ai_employee_table_pipelines(aiEmployeeId, tableId) → pipeline_id (when table has pipelines), (4) record_list(ai_employee_id, table_id [, pipeline_id]) → record_id, (5) ai_employee_table_fields(ai_employee_id, table_id) → field_id (when updating fields), (6) ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId) → status_id (when changing status), (7) record_update(record_id, ai_employee_id, table_id, recordFieldValues?, status_id?). All IDs are ENCRYPTED strings — pass as-is.\n\n" +
        "Do not put workflow status inside recordFieldValues — pass status_id separately. Prefer **record_update** with status_id over **change_record_status**.\n\n" +
        "recordFieldValues FORMAT:\n" +
        "Array of objects, or null/omit/[] for a status-only update. Each object updates one field: \`{ field_id, value }\` only (\`field_id\` from ai_employee_table_fields, non-null, non-empty). The server sends \`field_key: null\` on each item to the API.\n\n" +
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
        "WARNING: For dropdown fields, value must exactly match one of the strings in field_value_options returned by ai_employee_table_fields. Do not invent values.",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
        ai_employee_id: z.string().describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is."),
        table_id: z.string().describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is."),
        status_id: z
            .string()
            .nullable()
            .optional()
            .describe("Optional. ENCRYPTED status_id string from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId). Pass as-is for status changes."),
        recordFieldValues: z
            .array(z.object({
            field_id: z.string().min(1).describe("Required. ENCRYPTED field_id string from ai_employee_table_fields. Pass as-is."),
            value: z.any().describe("Field value to update."),
        }))
            .nullable()
            .optional()
            .describe("Optional. Field updates. Can be null/empty for status-only updates. See tool description for examples."),
        run_automation: z
            .boolean()
            .optional()
            .default(true)
            .describe("Optional. Defaults to true. When true, the API runs any automations configured for record updates. Pass false to skip automations."),
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
                        message: "Invalid recordFieldValues: field_id is required for every item and must not be null/empty. Rebuild recordFieldValues using ai_employee_table_fields.",
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
                    project_id: args.ai_employee_id,
                    table_id: args.table_id,
                    recordFieldValues: recordFieldValues.map(({ field_id, value }) => ({
                        field_id,
                        field_key: null,
                        value,
                    })),
                    run_automation: args.run_automation,
                };
                const response = await axios.post(`${plumoApiV1Base()}/records/update`, payload, {
                    headers: {
                        Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                        "Content-Type": "application/json",
                    },
                });
                const raw = response.data?.data ?? response.data ?? {};
                const fields = normalizeRecordFields(raw?.fields);
                const record = raw?.record ?? raw?.data?.record ?? {};
                const keyToFieldName = buildKeyToFieldNameMap(fields);
                const phoneKeys = buildPhoneFieldKeySet(fields);
                const transformedRecord = transformRecordKeys(record, keyToFieldName, phoneKeys);
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
                const fields = normalizeRecordFields(raw?.fields);
                const record = raw?.record ?? raw?.data?.record ?? {};
                const keyToFieldName = buildKeyToFieldNameMap(fields);
                const phoneKeys = buildPhoneFieldKeySet(fields);
                const transformedRecord = transformRecordKeys(record, keyToFieldName, phoneKeys);
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
    name: "ai_employee_table_pipeline_status_list",
    description: "UTILITY — Lists workflow statuses for a pipeline on an AI Employee table. ID TYPES: aiEmployeeId, tableId, and pipelineId are all ENCRYPTED strings. REQUIRED CALL ORDER: (1) ai_employee_list → ai_employee_id, (2) ai_employee_tables_list(aiEmployeeId) → table_id, (3) ai_employee_table_pipelines(aiEmployeeId, tableId) → pipeline_id, (4) ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId). Returns ENCRYPTED status_id values you pass as-is into record_create, record_update, record_list (top-level status_id with matching pipeline_id), or change_record_status.\n\n" +
        "Related table-scoped utilities (same aiEmployeeId + tableId): **ai_employee_table_fields** (field metadata), **ai_employee_table_pipelines** (resolve pipeline_id — required before this tool), **record_list** (filter records by pipeline_id and status_id).",
    parameters: z.object({
        aiEmployeeId: z
            .string()
            .describe("Required. ENCRYPTED ai_employee_id string from ai_employee_list. Pass as-is (NOT numeric)."),
        tableId: z
            .string()
            .describe("Required. ENCRYPTED table_id string from ai_employee_tables_list(aiEmployeeId). Pass as-is (NOT numeric)."),
        pipelineId: z
            .string()
            .describe("Required. ENCRYPTED pipeline_id string from ai_employee_table_pipelines(aiEmployeeId, tableId). Pass as-is (NOT numeric)."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const url = `${plumoApiV1Base()}/projects/${encodeURIComponent(args.aiEmployeeId)}/tables/${encodeURIComponent(args.tableId)}/status?pipelineId=${encodeURIComponent(args.pipelineId)}`;
            const response = await axios.get(url, {
                headers: {
                    Authorization: `Bearer ${session?.user_access_token}---CompanyID---${session?.companyId}`,
                    Accept: "application/json",
                },
            });
            return {
                type: "text",
                text: JSON.stringify({
                    success: true,
                    message: "AI Employee table pipeline status list retrieved successfully",
                    data: response.data?.data?.transitions ?? response.data?.data ?? response.data,
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
    name: "change_record_status",
    description: "DEPRECATED — Prefer **record_update** with \`status_id\` for status changes.\n" +
        "This tool remains functional but will be removed in a future version.\n" +
        "If you must use it: requires record_id (ENCRYPTED) and status_id (ENCRYPTED) from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId).",
    parameters: z.object({
        record_id: z.string().describe("Required. ENCRYPTED record_id string from record_list. Pass as-is."),
        status_id: z.string().describe("Required. ENCRYPTED status_id string from ai_employee_table_pipeline_status_list(aiEmployeeId, tableId, pipelineId) or record_list. Pass as-is."),
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
