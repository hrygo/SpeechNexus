// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/release_evidence.schema.json" with { type: "json" };
import type { components } from "./release_evidence.generated";
export type { components } from "./release_evidence.generated";
export type Contracts = Pick<components["schemas"], "QualificationReport" | "CapacityManifest">;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
