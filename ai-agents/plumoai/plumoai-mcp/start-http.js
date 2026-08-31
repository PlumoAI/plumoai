import server from "./server.js";
await server.start({
    transportType: "httpStream",
    httpStream: { port: 4008 },
});
