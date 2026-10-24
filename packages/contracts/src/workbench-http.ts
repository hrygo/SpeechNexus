// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/workbench-http.schema.json" with { type: "json" };
import type { paths } from "./workbench.generated";
export { ContractValidationError } from "./errors.mjs";
export type HttpOperation =
  | "getWorkbenchStatus"
  | "listProjects"
  | "createProject"
  | "getProject"
  | "updateProject"
  | "listTranscripts"
  | "createTranscript"
  | "getTranscript"
  | "createTranscriptRevision"
  | "getTranscriptRevision"
  | "workbenchCreateDeletionPlan"
  | "workbenchCreateDeletion"
  | "workbenchGetDeletion";
type HttpPaths = Pick<
  paths,
  | "/v1/workbench/status"
  | "/v1/workbench/projects"
  | "/v1/workbench/projects/{project_id}"
  | "/v1/workbench/projects/{project_id}/transcripts"
  | "/v1/workbench/transcripts/{transcript_id}"
  | "/v1/workbench/transcripts/{transcript_id}/revisions"
  | "/v1/workbench/transcripts/{transcript_id}/revisions/{revision}"
  | "/v1/workbench/deletion-plans"
  | "/v1/workbench/deletions"
  | "/v1/workbench/deletions/{deletion_id}"
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
