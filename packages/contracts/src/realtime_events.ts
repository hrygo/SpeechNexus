// Generated from contracts/bindings.json; do not edit.
import { createProfileParser } from "./profile-runtime.mjs";
export { ContractValidationError } from "./errors.mjs";
import bundle from "./speechnexus_contracts/realtime_events.schema.json" with { type: "json" };
import type { components } from "./realtime_events.generated";
export type { components } from "./realtime_events.generated";
export type Contracts = Pick<
  components["schemas"],
  "RealtimeEvent" | "TranscriptionEvent" | "OpenAIPcmFormat"
>;
const parseValue = createProfileParser(bundle);
export function parse<K extends keyof Contracts>(name: K, value: unknown): Contracts[K] {
  return parseValue(name, value) as Contracts[K];
}
