import axios from 'axios';
export class StoredProcedureService {
    constructor() {
        this.rootURLCompliance = process.env.COMPLIANCE_URL || "";
    }
    async storedProcedureComplianceDb(dataParam, companyId, options = {}, loginUserToken) {
        const { isOpenApi = false, isLog = false } = options;
        if (companyId === undefined || companyId === null || companyId === "") {
            throw Error("Missing companyId for storedProcedureComplianceDb");
        }
        let companyIds = [];
        companyIds = [companyId.toString()];
        let _loginUserToken = loginUserToken;
        if (isOpenApi && !_loginUserToken) {
            _loginUserToken =
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjgxMiwiaWF0IjoxNzA5MDUzNjM4fQ.7M4gYAj6fReAST4fx9tjb_OZ9ZlPJ17TlbvRUQS8W_4";
        }
        const headers = {
            Authorization: `Bearer ${_loginUserToken}`,
            companyId: JSON.stringify(companyIds),
            'Content-Type': 'application/json',
        };
        try {
            const config = {
                headers,
                timeout: 50000,
            };
            const response = await axios.post(`${process.env.COMPLIANCE_URL}store/procedure/execute`, dataParam, config);
            return response.data;
        }
        catch (error) {
            throw Error(JSON.stringify(error?.response?.data ?? "Unknown error occurred"));
        }
    }
}
