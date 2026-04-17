import express from "express";
import dotenv from "dotenv";
import { logger } from "./utils/logger.js";
dotenv.config();
const app = express();
app.use(express.json());
const PORT = process.env.PORT || 4000;
app.listen(PORT, () => logger.info(`Server running on port ${PORT}`));
logger.info(`MCP Server running on port ${4001}`);
