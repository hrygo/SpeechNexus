// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import { validators } from "./speechnexus_contracts/scenario_profiles.validators.generated.mjs";
import type { components } from "./scenario_profiles.generated";
export type { components } from "./scenario_profiles.generated";
export type Contracts = Pick<components["schemas"], "ScenarioProfileRevision">;
const parseValue = createProfileParser(validators);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
