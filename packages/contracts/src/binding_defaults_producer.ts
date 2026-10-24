// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/binding_defaults_producer.schema.json" with { type: "json" };
import type { components } from "./binding_defaults_producer.generated";
export type { components } from "./binding_defaults_producer.generated";
export type Contracts = Pick<components["schemas"], "BindingDefaultsCreateRequest">;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
