import crypto from "crypto";
import { issueToken } from "../utils/jwt.js";
function timingSafeEqualStrings(a, b) {
    try {
        const ba = Buffer.from(a, "utf8");
        const bb = Buffer.from(b, "utf8");
        if (ba.length !== bb.length)
            return false;
        return crypto.timingSafeEqual(ba, bb);
    }
    catch {
        return false;
    }
}
async function authenticate(client_id, client_secret) {
    if (!client_id || !client_secret) {
        throw new Response(JSON.stringify({ error: "Missing Credentials" }), {
            status: 400,
            headers: { "Content-Type": "application/json" },
        });
    }
    const expectedId = process.env.OAUTH_CLIENT_ID?.trim() ?? "";
    const expectedSecret = process.env.OAUTH_CLIENT_SECRET ?? "";
    if (!expectedId || !expectedSecret) {
        throw new Response(JSON.stringify({
            error: "oauth_not_configured",
            error_description: "Set OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET (and OAUTH_COMPANY_ID) in the environment.",
        }), { status: 503, headers: { "Content-Type": "application/json" } });
    }
    if (!timingSafeEqualStrings(client_id, expectedId)) {
        throw new Response(JSON.stringify({ error: "Invalid client_id" }), {
            status: 401,
            statusText: "Unauthorized",
        });
    }
    if (!timingSafeEqualStrings(client_secret, expectedSecret)) {
        throw new Response(JSON.stringify({ error: "Invalid client_secret" }), {
            status: 401,
            statusText: "Unauthorized",
        });
    }
    const appId = process.env.OAUTH_APP_ID?.trim() || expectedId;
    const companyId = Number(process.env.OAUTH_COMPANY_ID);
    if (!Number.isFinite(companyId)) {
        throw new Response(JSON.stringify({
            error: "oauth_misconfigured",
            error_description: "OAUTH_COMPANY_ID must be set to a numeric company id.",
        }), { status: 503, headers: { "Content-Type": "application/json" } });
    }
    const scopes = process.env.OAUTH_SCOPES?.split(",")
        .map((s) => s.trim())
        .filter(Boolean) ?? [];
    const token = issueToken(appId, companyId, scopes);
    return { token, companyId };
}
export default {
    authenticate,
};
