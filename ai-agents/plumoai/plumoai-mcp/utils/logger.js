const levelOrder = {
    silent: 0,
    error: 1,
    warn: 2,
    info: 3,
    debug: 4,
    trace: 5,
};
function parseLogLevel(raw) {
    const s = String(raw ?? "").trim().toLowerCase();
    if (s === "0" || s === "silent" || s === "none" || s === "off")
        return "silent";
    if (s === "1" || s === "error")
        return "error";
    if (s === "2" || s === "warn" || s === "warning")
        return "warn";
    if (s === "3" || s === "info" || s === "")
        return "info";
    if (s === "4" || s === "debug")
        return "debug";
    if (s === "5" || s === "trace")
        return "trace";
    return "info";
}
function shouldLog(target) {
    const configured = parseLogLevel(process.env.LOG_LEVEL);
    return levelOrder[target] <= levelOrder[configured] && configured !== "silent";
}
function prefix(level) {
    return `[${new Date().toISOString()}] [${level}]`;
}
export const logger = {
    error: (...args) => {
        if (!shouldLog("error"))
            return;
        console.error(prefix("error"), ...args);
    },
    warn: (...args) => {
        if (!shouldLog("warn"))
            return;
        console.warn(prefix("warn"), ...args);
    },
    info: (...args) => {
        if (!shouldLog("info"))
            return;
        console.log(prefix("info"), ...args);
    },
    debug: (...args) => {
        if (!shouldLog("debug"))
            return;
        console.log(prefix("debug"), ...args);
    },
    trace: (...args) => {
        if (!shouldLog("trace"))
            return;
        console.log(prefix("trace"), ...args);
    },
};
