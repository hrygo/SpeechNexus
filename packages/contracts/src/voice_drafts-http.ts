// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/voice_drafts-http.schema.json" with { type: "json" };
import type { paths } from "./speech.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation =
  | "createVoiceDesign"
  | "createVoiceClone"
  | "getVoiceDraft"
  | "updateVoiceDraftMetadata"
  | "generateVoiceDraftRevision"
  | "createVoicePreview"
  | "validateVoiceDraftRevision";
type HttpPaths = {
  "/v1/nexus/voice-designs": Pick<paths["/v1/nexus/voice-designs"], "parameters" | "post">;
  "/v1/nexus/voice-clones": Pick<paths["/v1/nexus/voice-clones"], "parameters" | "post">;
  "/v1/nexus/voice-drafts/{draft_id}": Pick<
    paths["/v1/nexus/voice-drafts/{draft_id}"],
    "parameters" | "get" | "patch"
  >;
  "/v1/nexus/voice-drafts/{draft_id}/generations": Pick<
    paths["/v1/nexus/voice-drafts/{draft_id}/generations"],
    "parameters" | "post"
  >;
  "/v1/nexus/voice-drafts/{draft_id}/previews": Pick<
    paths["/v1/nexus/voice-drafts/{draft_id}/previews"],
    "parameters" | "post"
  >;
  "/v1/nexus/voice-drafts/{draft_id}/validations": Pick<
    paths["/v1/nexus/voice-drafts/{draft_id}/validations"],
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
