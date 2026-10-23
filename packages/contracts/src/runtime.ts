// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/runtime.schema.json" with { type: "json" };
import type { components } from "./runtime.generated";
export type { components } from "./runtime.generated";
export type Contracts = Pick<
  components["schemas"],
  "RuntimeInstanceIntentRequest" | "RuntimeInstanceIntentResponse" | "NodeReconcileRequest"
>;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
import openapiFetch from "openapi-fetch";
import type { paths } from "./runtime.generated";
export type { paths } from "./runtime.generated";
export function createClient(baseUrl: string, fetch?: typeof globalThis.fetch) {
  return openapiFetch<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });
}
