import type { FileContracts } from "./file.generated";

export class ContractValidationError extends Error {}
export function parse<K extends keyof FileContracts>(name: K, value: unknown): FileContracts[K];
