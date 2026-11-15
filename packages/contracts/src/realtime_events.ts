// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import { validators } from "./speechnexus_contracts/realtime_events.validators.generated.mjs";
import type { components } from "./realtime_events.generated";
export type { components } from "./realtime_events.generated";
export type Contracts = Pick<
  components["schemas"],
  "RealtimeEvent" | "TranscriptionEvent" | "OpenAIPcmFormat"
>;
const parseValue = createProfileParser(validators);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
