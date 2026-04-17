import bcrypt from "bcrypt";
import { pool } from "../db.js";
import { issueToken } from "../utils/jwt.js";
async function authenticate(client_id, client_secret) {
    if (!client_id || !client_secret) {
        throw new Response(JSON.stringify({ error: "Missing Credentials" }), {
            status: 400, headers: { "Content-Type": "application/json" }
        });
    }
    const [rows] = await pool.query("SELECT * FROM oauth_apps WHERE client_id = ? AND is_active = 1", [client_id]);
    const apps = rows;
    if (apps.length === 0) {
        throw new Response(JSON.stringify({ error: "Invalid client_id" }), {
            status: 401,
            statusText: "Unauthorized",
        });
    }
    const app = apps[0];
    const validSecret = await bcrypt.compare(client_secret, app.client_secret);
    if (!validSecret) {
        throw new Response(JSON.stringify({ error: "Invalid client_secret" }), {
            status: 401,
            statusText: "Unauthorized",
        });
        // return res.status(401).json({ error: "Invalid client_secret" });
    }
    const token = issueToken(app.app_id, app.companyId, app.scopes?.split(",") ?? []);
    return { token, companyId: app.companyId, };
}
export default {
    authenticate
};
