import server from "./server.js";
// STDIO startup should be as fast as possible.
// Only load `.env` when explicitly requested (e.g. for local dev).
if (process.env.LOAD_DOTENV === "1") {
    const dotenv = await import("dotenv");
    dotenv.default.config();
}
await server.start({
    transportType: "stdio",
});
