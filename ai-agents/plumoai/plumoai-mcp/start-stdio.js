import server from "./server.js";
await server.start({
    transportType: "stdio",
});
