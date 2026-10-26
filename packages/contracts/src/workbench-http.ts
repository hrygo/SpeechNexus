// Generated HTTP validation entrypoints; do not edit.
import openapiFetch from "openapi-fetch";
import { createHttpValidator } from "./http-runtime.mjs";
import bundle from "./speechnexus_contracts/workbench-http.schema.json" with { type: "json" };
import type { paths, components } from "./workbench.generated";
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
  | "workbenchGetDeletion"
  | "createMeeting"
  | "getMeeting"
  | "sealMeeting"
  | "createMeetingMinutes"
  | "getMeetingMinutes"
  | "generateRenderUnit"
  | "getTeleprompterState"
  | "updateTeleprompterState"
  | "saveProjectAsset"
  | "getSavedAsset"
  | "listBooks"
  | "createBook"
  | "getBook"
  | "listChapters"
  | "createChapter"
  | "getChapter"
  | "listRenderUnits"
  | "createRenderUnit"
  | "getRenderUnit"
  | "updateRenderUnit"
  | "createAssistantTurn"
  | "getAssistantTurn"
  | "cancelAssistantTurn"
  | "createBookExport"
  | "getBookExport"
  | "cancelBookExport"
  | "listVoices"
  | "getVoiceRevision"
  | "getVoiceRevisionV2"
  | "getWorkbenchStatusV2"
  | "listRenderUnitsV2"
  | "createRenderUnitV2"
  | "getRenderUnitV2"
  | "updateRenderUnitV2"
  | "listRenderUnitGenerationsV2"
  | "generateRenderUnitV2"
  | "downloadSavedAsset"
  | "downloadBookExport"
  | "streamExportEvents";
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
  | "/v1/workbench/projects/{project_id}/meetings"
  | "/v1/workbench/meetings/{meeting_id}"
  | "/v1/workbench/meetings/{meeting_id}/seal"
  | "/v1/workbench/meetings/{meeting_id}/minutes"
  | "/v1/workbench/meetings/{meeting_id}/minutes/{minutes_id}"
  | "/v1/workbench/render-units/{unit_id}/generations"
  | "/v1/workbench/projects/{project_id}/teleprompter"
  | "/v1/workbench/projects/{project_id}/saved-assets"
  | "/v1/workbench/saved-assets/{asset_id}"
  | "/v1/workbench/projects/{project_id}/books"
  | "/v1/workbench/books/{book_id}"
  | "/v1/workbench/books/{book_id}/chapters"
  | "/v1/workbench/chapters/{chapter_id}"
  | "/v1/workbench/chapters/{chapter_id}/render-units"
  | "/v1/workbench/render-units/{unit_id}"
  | "/v1/workbench/projects/{project_id}/assistant-turns"
  | "/v1/workbench/assistant-turns/{turn_id}"
  | "/v1/workbench/assistant-turns/{turn_id}/cancellations"
  | "/v1/workbench/books/{book_id}/exports"
  | "/v1/workbench/exports/{export_id}"
  | "/v1/workbench/exports/{export_id}/cancellations"
  | "/v1/workbench/voices"
  | "/v1/workbench/voices/{voice_id}/revisions/{revision}"
  | "/v2/workbench/voices/{voice_id}/revisions/{revision}"
  | "/v2/workbench/status"
  | "/v2/workbench/chapters/{chapter_id}/render-units"
  | "/v2/workbench/render-units/{unit_id}"
  | "/v2/workbench/render-units/{unit_id}/generations"
  | "/v1/workbench/saved-assets/{asset_id}/content"
  | "/v1/workbench/exports/{export_id}/content"
  | "/v1/workbench/exports/{export_id}/events"
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
export type HttpEvents = {
  streamExportEvents: components["schemas"]["ExportEvent"];
};
export function validateEvent<K extends keyof HttpEvents>(
  operation: K,
  value: unknown,
): HttpEvents[K] {
  return validator.validateEvent(operation, value) as HttpEvents[K];
}
export function readEvents<K extends keyof HttpEvents>(
  operation: K,
  source: ReadableStream<Uint8Array>,
  request: unknown,
  maxEventBytes: number,
): AsyncIterable<HttpEvents[K]> {
  return validator.readEvents(operation, source, request, maxEventBytes) as AsyncIterable<
    HttpEvents[K]
  >;
}
