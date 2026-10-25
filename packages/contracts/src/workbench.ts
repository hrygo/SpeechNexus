// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/workbench.schema.json" with { type: "json" };
import type { components } from "./workbench.generated";
export type { components } from "./workbench.generated";
export type Contracts = Pick<
  components["schemas"],
  | "ProjectCreateRequest"
  | "ProjectPatch"
  | "Project"
  | "ProjectList"
  | "TranscriptCreateRequest"
  | "TranscriptRevisionCreateRequest"
  | "Transcript"
  | "TranscriptRevision"
  | "TranscriptView"
  | "TranscriptList"
  | "WorkbenchStatus"
  | "ErrorResponse"
  | "DeletionPlanRequest"
  | "DeletionCreateRequest"
  | "DeletionPlan"
  | "Deletion"
  | "Meeting"
  | "MeetingCreateRequest"
  | "MeetingSealRequest"
  | "MinutesCreateRequest"
  | "MinutesJob"
  | "RenderUnitGenerationRequest"
  | "RenderUnitGenerationAccepted"
  | "TeleprompterUpdateRequest"
  | "TeleprompterState"
  | "SavedAsset"
  | "SavedAssetCreateRequest"
  | "Book"
  | "BookCreateRequest"
  | "Chapter"
  | "ChapterCreateRequest"
  | "RenderUnit"
  | "RenderUnitCreateRequest"
  | "RenderUnitPatch"
  | "AssistantTurn"
  | "AssistantTurnCreateRequest"
  | "ExportArtifact"
  | "ExportCreateRequest"
  | "ExportJob"
  | "BookList"
  | "ChapterList"
  | "RenderUnitList"
  | "VoiceList"
  | "VoiceSummary"
  | "VoiceRevision"
  | "WorkbenchStatusV2"
  | "RenderUnitV2"
  | "RenderUnitCreateRequestV2"
  | "RenderUnitPatchV2"
  | "RenderUnitListV2"
  | "RenderUnitGenerationAcceptedV2"
>;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
import openapiFetch from "openapi-fetch";
import type { paths } from "./workbench.generated";
export type { paths } from "./workbench.generated";
export function createClient(baseUrl: string, fetch?: typeof globalThis.fetch) {
  return openapiFetch<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });
}
