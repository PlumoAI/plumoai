import { FastMCP } from "fastmcp";
import { z } from "zod"; // Or any validation library that supports Standard Schema
import axios from "axios";
import { verifyToken, verifyTokenWithSecret } from "./utils/jwt.js";
import { StoredProcedureService } from "./utils/sp_caller_service.js";
import { logger } from "./utils/logger.js";
const spService = new StoredProcedureService();
const server = new FastMCP({
    name: "PlumoAI MCP Server",
    version: "1.0.0",
    authenticate: async (request) => {
        try {
            const pat = request.headers["pat"];
            var userPayload = verifyTokenWithSecret(pat + "", "gfadsjhgfsdajhg4847329842kfjadshfkjad");
            if (!userPayload) {
                throw new Response(JSON.stringify({ error: "Invalid Credentials" }), {
                    status: 401, headers: { "Content-Type": "application/json" }
                });
            }
            logger.info("Authenticated request user:", userPayload.userId);
            return { user_access_token: pat, expires_in: 900, companyId: String(userPayload.companyId ?? ""), userId: userPayload.userId };
        }
        catch (err) {
            logger.error(err);
            throw err;
        }
    }
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
    description: "Fetch projects. Supports optional filtering by projectId or workspaceId. If workspaceId=-1, returns all projects (useful for searching by name).(Note: Also fetch sprints for Scrum projects after gettingn specific project)",
    parameters: z.object({
        projectId: z.number().optional().describe("Optional Project ID. Pass 0 to fetch all projects."),
        workspaceId: z.number().optional().describe("Optional Workspace ID. Pass -1 to ignore workspace and return all projects."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            if (args.projectId != null && args.projectId < 0) {
                args.projectId = 0;
            }
            const data = {
                storeProcedureName: "usp_proj_get_project",
                parameters: {
                    p_project_id: args.projectId ?? 0,
                    p_LoggedInUser: session?.userId,
                    p_CompanyID: session?.companyId,
                    p_Location_fid: args.workspaceId ?? -1,
                    p_proj_status: "P",
                    p_PageNumber: 1,
                    p_RowsOfPage: 1000,
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const projects = (responseData?.data ?? []);
            return {
                type: "text",
                text: JSON.stringify(projects),
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
    description: "LEGACY (NUMERIC-ID workflow) — Fetch list of records including custom field values. ID TYPES: projectId/sprintId/workitemTypeId are NUMERIC.\n" +
        "NOTE (Scrum projects): sprintId is required. Use 0 for non-scrum projects. For scrum projects, fetch sprints first and pass the desired numeric sprint ID.",
    parameters: z.object({
        projectId: z.number().describe("Required. Project ID. Use ProjectList if unknown."),
        sprintId: z.number().optional().describe("Required Sprint ID for scrum project. Pass 0 For non Scrum projects, and For scrum project must be pass sprint ID by fetching all sprints and pass current sprint id after identifying from sprint list. If sprint not defined then ask with user to select sprint"),
        workitemTypeId: z.number().optional().describe("Required TableID in number. Default -1 = all tables."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            let _data = JSON.stringify({});
            let config = {
                method: 'post',
                maxBodyLength: Infinity,
                url: `${process.env.COMPLIANCE_URL || ""}/grid/grid/?projectId=${args.projectId}&workitemTypeId=${args.workitemTypeId}&loadPartialData=false&sprintId=0`,
                headers: {
                    "Authorization": `Bearer ${session?.user_access_token}`,
                    'companyid': `[${session?.companyId}]`,
                    'Content-Type': 'application/json'
                },
                data: _data
            };
            var recordsRepsonse = await axios.request(config)
                .then((response) => {
                return response.data;
            })
                .catch((error) => {
                logger.warn(error);
            });
            return {
                type: "text",
                text: JSON.stringify(recordsRepsonse),
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
    name: "ProjectWorkflowAndTable",
    description: "Fetch workflows and their associated tables for a given project.",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Project ID. Required to fetch workflow and their table for the project.",
        }),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_projectworkflow",
                parameters: {
                    p_project_id: String(args.projectId),
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const workflow = responseData?.data ?? [];
            return {
                type: "text",
                text: JSON.stringify(workflow.map((x) => {
                    return {
                        "workflowID": x.proj_workflow_id,
                        "workflowName": x.workflow_name,
                        "tableID": x.proj_workitem_type_fid,
                        "tableName": x.workitem_type,
                        "tableLevel": 2,
                        "tableDisplayName": x.wki_display_name
                    };
                })),
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
    name: "ProjectCategories",
    description: "Fetch state categories for a specific project by project ID",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Project ID (fid). Required to fetch state categories for the project.",
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
server.addTool({
    name: "DetailedRecord",
    description: `Fetch full details of a record (task). Includes:
- Basic record fields
- Users (created by, modified by, assignee)
- Comments
- Attachments
- Linked tasks
- Checklist items`,
    parameters: z.object({
        recordId: z.number().describe("Required. Record ID."),
    }),
    canAccess(auth) {
        return checkAccess(auth);
    },
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_detailed_tasks",
                version: 2,
                parameters: {
                    p_task_Id: args.recordId,
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            const taskDetails = normalizeTaskRecord(responseData?.data ?? []);
            return {
                type: "text",
                text: JSON.stringify(taskDetails),
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
    name: "ProjectSprints",
    description: "Fetch all sprints for a specific project by project ID.(Note: if project is not scrum template project then it will return empty array)",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Project ID. Required to fetch sprints for the project.",
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
    description: "Fetch field tabs (sections of fields) for a given table (workitem type).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        tableId: z.number({
            description: "Table (workitem type) ID. Required to fetch field tabs.",
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
    description: "Fetch custom fields for a given table (workitem type) within a project (Fetch all and ask all required fields before create new in table).",
    canAccess(auth) {
        return checkAccess(auth);
    },
    parameters: z.object({
        projectId: z.number({
            description: "Project ID. Required to fetch fields for the project.",
        }),
        tableId: z.number({
            description: "Table (workitem type) ID. Required to fetch fields for the table.",
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
    description: "LEGACY (NUMERIC-ID workflow) — Creates a new record via stored procedure.\n" +
        "ID TYPES: projectId/statusId/tableId/parentTaskId/sprintId are NUMERIC (NOT encrypted strings).",
    parameters: z.object({
        projectId: z.number().describe("Project ID where the task will be created."),
        title: z.string().describe("Title of the new record."),
        statusId: z.number().describe("Workflow status ID for the record."),
        tableId: z.number().describe("Table ID."),
        description: z.string().optional().describe("Description of the record."),
        parentTaskId: z.number().default(0).describe("Parent record ID. Default 0 = no parent."),
        sprintId: z.number().default(0).describe("Sprint ID. Default 0 = backlog or non-scrum projects."),
        priority: z.string().default("Medium").describe("Priority (e.g., Low, Medium, High)."),
        assignedUsers: z.array(z.object({
            userId: z.string().describe("User ID."),
            role: z.number().describe("User type: 1=assignee, 2=reviewer, etc."),
        })).default([]),
    }),
    execute: async (args, { session }) => {
        const data = {
            storeProcedureName: "usp_proj_add_quick_tasks",
            version: 4,
            parameters: {
                p_Json: [
                    {
                        project_fid: args.projectId,
                        title: args.title,
                        proj_workflow_status_fid: args.statusId,
                        description: encryptHtml(args.description ?? ""),
                        proj_workitem_type_fid: args.tableId,
                        parent_task_fid: args.parentTaskId ?? 0,
                        sprint_fid: args.sprintId ?? 0,
                        external_task_id: 0,
                        task_priority_fid: args.priority ?? "Medium",
                        loggedin_user_id: String(session?.userId),
                        is_scheduled: 0,
                        is_recurring: 0,
                        sched_task_id: 0,
                        taskusers: [{
                                user_fid: session?.userId,
                                user_type_id: 1
                            }],
                        called_from: "Board"
                    }
                ]
            }
        };
        var recordOutput = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
        return {
            type: "text",
            text: JSON.stringify(recordOutput.data),
        };
    }
});
server.addTool({
    name: "WorkflowStatus",
    description: "Fetch all table record status for a given workflow in a project.",
    parameters: z.object({
        workflowId: z.number().describe("Required. Workflow ID."),
        projectId: z.string().describe("Required. Project ID."),
        tableId: z.string().default("-1").describe("Optional Table ID (table). Default -1 = all tables."),
    }),
    execute: async (args, { session }) => {
        try {
            const data = {
                storeProcedureName: "usp_proj_get_workflow_status_transition",
                parameters: {
                    p_workflow_id: args.workflowId,
                    p_project_fid: args.projectId,
                    p_proj_workitem_type_id: args.tableId ?? "-1",
                },
            };
            const responseData = await spService.storedProcedureComplianceDb(data, session?.companyId, { isOpenApi: false }, String(session?.user_access_token ?? ""));
            // Normalize response: show only fromStatus → toStatus
            const transitions = (responseData?.data ?? []);
            return {
                type: "text",
                text: JSON.stringify(transitions),
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
    name: "UpdateRecordField",
    description: "LEGACY (NUMERIC-ID workflow) — Updates a single field via stored procedure.\n" +
        "ID TYPES: recordId and fieldId are NUMERIC (NOT encrypted strings).",
    parameters: z.object({
        recordId: z.number().describe("Required. Record ID."),
        fieldId: z.number().describe("Required. Project field ID."),
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
await server.start({
    transportType: "httpStream",
    httpStream: { port: 4008 },
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
        if (!auth.access_token)
            return true;
        var payload = verifyToken(auth.access_token);
        if (payload.company != null) {
            return true;
        }
        return false;
    }
    catch (ex) {
        logger.warn(ex);
        return false;
    }
}
