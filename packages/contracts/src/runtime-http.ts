// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/runtime-http.schema.json" with { type: "json" };
import type { paths } from "./runtime.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation =
  "submitRuntimeInstanceIntent" | "getRuntimeInstanceIntent" | "getVerifiedInstanceStop";
type HttpPaths = Pick<
  paths,
  | "/internal/v1/nodes/{node_id}/instance-intents"
  | "/internal/v1/nodes/{node_id}/instance-intents/{intent_id}"
  | "/internal/v1/nodes/{node_id}/instances/{instance_id}/generations/{instance_generation}/verified-stop"
>;
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
