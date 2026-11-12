// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/admin-http.schema.json" with { type: "json" };
import type { paths } from "./admin.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation =
  | "listWorkspaces"
  | "createWorkspace"
  | "getWorkspace"
  | "updateWorkspace"
  | "listResourcePools"
  | "listModelBindings"
  | "listModelInstances"
  | "getModelInstance"
  | "listAuditEvents";
type HttpPaths = {
  "/v1/nexus/workspaces": Pick<paths["/v1/nexus/workspaces"], "parameters" | "get" | "post">;
  "/v1/nexus/workspaces/{workspace_id}": Pick<
    paths["/v1/nexus/workspaces/{workspace_id}"],
    "parameters" | "get" | "patch"
  >;
  "/v1/nexus/admin/resource-pools": Pick<
    paths["/v1/nexus/admin/resource-pools"],
    "parameters" | "get"
  >;
  "/v1/nexus/admin/model-bindings": Pick<
    paths["/v1/nexus/admin/model-bindings"],
    "parameters" | "get"
  >;
  "/v1/nexus/admin/model-instances": Pick<
    paths["/v1/nexus/admin/model-instances"],
    "parameters" | "get"
  >;
  "/v1/nexus/admin/model-instances/{instance_id}/generations/{generation}": Pick<
    paths["/v1/nexus/admin/model-instances/{instance_id}/generations/{generation}"],
    "parameters" | "get"
  >;
  "/v1/nexus/audit-events": Pick<paths["/v1/nexus/audit-events"], "parameters" | "get">;
};
const validator = createHttpValidator(bundle);
export function validateRequest<T>(operation: HttpOperation, value: T): T {
  return validator.validateRequest(operation, value);
}
export function validateResponse<T>(operation: HttpOperation, status: number, value: T): T {
  return validator.validateResponse(operation, status, value);
}
export function createValidatedClient(
  baseUrl: string,
  maxJsonBytes: number,
  fetch?: typeof globalThis.fetch,
) {
  const client = openapiFetch<HttpPaths>({
    baseUrl,
    redirect: "error",
    ...(fetch ? { fetch } : {}),
  });
  client.use(validator.middleware(baseUrl, maxJsonBytes));
  return client;
}
