// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/admin.schema.json" with { type: "json" };
import type { components } from "./admin.generated";
export type { components } from "./admin.generated";
export type Contracts = Pick<
  components["schemas"],
  | "WorkspaceCreateRequest"
  | "WorkspacePatch"
  | "Workspace"
  | "WorkspaceList"
  | "ErrorResponse"
  | "ProfileRevisionCreateRequest"
  | "ModelBindingCreateRequest"
  | "ProfilePublicationRequest"
  | "ProfileActivationRequest"
  | "ProfilePublishedRevision"
>;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
import openapiFetch from "openapi-fetch";
import type { paths } from "./admin.generated";
export type { paths } from "./admin.generated";
export function createClient(baseUrl: string, fetch?: typeof globalThis.fetch) {
  return openapiFetch<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });
}
