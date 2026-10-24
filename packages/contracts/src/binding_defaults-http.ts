// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/binding_defaults-http.schema.json" with { type: "json" };
import type { paths } from "./binding_defaults_producer.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation = "createModelBindingDraft" | "getModelBindingParameterDefaults";
type HttpPaths = {
  "/v1/nexus/admin/model-bindings": Pick<
    paths["/v1/nexus/admin/model-bindings"],
    "parameters" | "post"
  >;
  "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults": Pick<
    paths["/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults"],
    "parameters" | "get"
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
