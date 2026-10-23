import type { FileContracts } from "./file.generated";

export { ContractValidationError } from "./errors.mjs";
export function parse<K extends keyof FileContracts>(name: K, value: unknown): FileContracts[K];
