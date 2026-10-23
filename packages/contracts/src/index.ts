import createClient from "openapi-fetch";
import type { paths } from "./speech.generated";

export { ContractValidationError, parse } from "./runtime.mjs";
export type { FileContracts } from "./file.generated";
export type { components, paths } from "./speech.generated";

/** Caller supplies the deployment URL and may inject its local fetch transport. */
export function createSpeechClient(baseUrl: string, fetch?: typeof globalThis.fetch) {
  return createClient<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });
}
