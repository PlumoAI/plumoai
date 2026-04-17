import { FastMCP } from "fastmcp";
import { StoredProcedureService } from "./utils/sp_caller_service.js";
const spService = new StoredProcedureService();
import jwt from "jsonwebtoken";
var projectMCPTools = {};
const server = new FastMCP({
    name: "PlumoAI Project MCP Server",
    version: "1.0.0",
    oauth: {
        enabled: true,
        authorizationServer: {
            issuer: "https://plumoai.com",
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
            resource: "https://cfffa9cdc02b.ngrok-free.app",
            authorizationServers: ["https://api.plumoai.com/Auth"],
            resource_name: "PlumoAI MCP (Beta)",
            resource_documentation: "https://developers.plumoai.com/docs/mcp",
            authorization_servers: ["https://cfffa9cdc02b.ngrok-free.app"],
            bearer_methods_supported: ["header"]
        }
    },
    authenticate: async (request) => {
        const authHeader = request.headers.authorization;
        if (!authHeader?.startsWith("Bearer ")) {
            throw new Response(null, {
                status: 401,
                statusText: JSON.stringify({ "error": "invalid_token", "error_description": "Missing or invalid access token" }),
            });
        }
        try {
            var data = jwt.decode(authHeader.slice(7), { complete: true });
            if ((data?.payload?.companyIds ?? []).length < 0) {
                throw new Response(null, {
                    status: 401,
                    statusText: "Invalid OAuth token",
                });
            }
            return { user_access_token: authHeader.slice(7), expires_in: 900, companyId: (data?.payload?.companyIds ?? [])[0], userId: (data?.payload?.userId ?? 0) };
        }
        catch (error) {
            throw new Response(null, {
                status: 401,
                statusText: "Invalid OAuth token",
            });
        }
    },
});
await server.start({
    transportType: "httpStream",
    httpStream: { port: 4009 },
});
