import jwt from "jsonwebtoken";
import crypto from "crypto";
export function issueToken(appId, companyId, scopes = []) {
    const secret = process.env.JWT_SECRET;
    const signOptions = {
        algorithm: "HS256",
        expiresIn: "1h",
    };
    return jwt.sign({
        sub: appId,
        company: companyId,
        scopes,
        jti: crypto.randomUUID()
    }, secret, signOptions);
}
export function verifyToken(token) {
    const secret = process.env.JWT_SECRET;
    return jwt.verify(token, secret, { algorithms: ["HS256"] });
}
export function verifyTokenWithSecret(token, secret) {
    return jwt.verify(token, secret, { algorithms: ["HS256"] });
}
