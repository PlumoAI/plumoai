import server from "./server.js";
import dotenv from "dotenv";
// HTTP mode commonly uses local env via .env during development.
dotenv.config();
await server.start({
    transportType: "httpStream",
    httpStream: { port: 4008 },
});
