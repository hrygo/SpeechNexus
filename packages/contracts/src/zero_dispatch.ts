// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/zero_dispatch.schema.json" with { type: "json" };
import type { components } from "./zero_dispatch.generated";
export type { components } from "./zero_dispatch.generated";
export type Contracts = Pick<
  components["schemas"],
  | "OriginalExecutionKey"
  | "ReceiveCancellationRequest"
  | "ReceiveCancellationResult"
  | "OriginalRecoveryQuery"
  | "OriginalRecoverySnapshot"
  | "ZeroDispatchControlError"
>;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
