// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/gateway_recovery.schema.json" with { type: "json" };
import type { components } from "./gateway_recovery.generated";
export type { components } from "./gateway_recovery.generated";
export type Contracts = Pick<components["schemas"], "GatewayRecoveryConfig">;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
