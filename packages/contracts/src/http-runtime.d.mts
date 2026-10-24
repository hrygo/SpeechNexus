import type { Middleware } from "openapi-fetch";

export function createHttpValidator(bundle: object): {
  validateRequest<T>(operation: string, value: T): T;
  validateResponse<T>(operation: string, status: number, value: T): T;
  middleware(baseUrl: string, maxJsonBytes: number): Middleware;
};
