// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/profile_lifecycle-http.schema.json" with { type: "json" };
import type { paths } from "./admin.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation = "publishProfileRevision" | "activatePublishedProfileRevision";
type HttpPaths = {
  "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications": Pick<
    paths["/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications"],
    "parameters" | "post"
  >;
  "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/activations": Pick<
    paths["/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/activations"],
    "parameters" | "post"
  >;
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
