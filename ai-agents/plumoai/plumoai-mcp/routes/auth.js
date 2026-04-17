import { Router } from "express";
import authService from "../services/auth.serve.js";
import { logger } from "../utils/logger.js";
const router = Router();
router.post("/token", async (req, res) => {
    try {
        const { client_id, client_secret } = req.body;
        var token = await authService.authenticate(client_id, client_secret);
        return res.json({ access_token: token, token_type: "Bearer", expires_in: 900 });
    }
    catch (err) {
        if (err instanceof Response) {
            var body = await err.text();
            if (body) {
                body = JSON.parse(body);
            }
            res.status(err.status).json(body);
            return;
        }
        logger.error(err);
        res.status(500).json({ error: "Server error" });
    }
});
export default router;
