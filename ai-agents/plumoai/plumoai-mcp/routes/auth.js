import { Router } from "express";
import authService from "../services/auth.serve.js";
const router = Router();
router.post("/token", async (req, res) => {
    try {
        const { client_id, client_secret } = req.body;
        const result = await authService.authenticate(client_id, client_secret);
        return res.json({
            access_token: result.token,
            token_type: "Bearer",
            expires_in: 900,
        });
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
        console.log(err);
        res.status(500).json({ error: "Server error" });
    }
});
export default router;
