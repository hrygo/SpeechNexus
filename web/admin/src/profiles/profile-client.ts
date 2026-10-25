import { parse, type Contracts } from "../../../../packages/contracts/src/admin";
import {
  parse as parseProfile,
  type Contracts as Profiles,
} from "../../../../packages/contracts/src/scenario_profiles";
import {
  createValidatedClient,
  validateRequest,
  validateResponse,
} from "../../../../packages/contracts/src/profile-http";

export type ProfileRequest = Contracts["ProfileRevisionCreateRequest"];
export type ProfileRevision = Profiles["ScenarioProfileRevision"];
export interface ProfileApi {
  create(body: ProfileRequest, key: string, signal?: AbortSignal): Promise<ProfileRevision>;
  read(id: string, revision: string, signal?: AbortSignal): Promise<ProfileRevision>;
}
export class ProfileHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

export const PROFILE_JSON_BYTES = 262144;
// A complete receipt adds revision/publication metadata to the bounded request.
export const PROFILE_RESPONSE_BYTES = 524288;

export function profileDefinition(value: Record<string, unknown>): Record<string, unknown> {
  const copy = { ...value };
  for (const key of [
    "profile_id",
    "expected_latest_revision",
    "schema_version",
    "revision",
    "publication",
    "validation",
    "supersedes_revision",
  ])
    delete copy[key];
  return copy;
}

/** UI metadata is separate from the complete definition; never silently override its fields. */
export function profileRequest(
  id: string,
  expected: string | null,
  definition: string,
): ProfileRequest {
  if (new TextEncoder().encode(definition).byteLength > PROFILE_JSON_BYTES)
    throw new Error("profile_input_invalid");
  const value: unknown = JSON.parse(definition);
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.hasOwn(value, "profile_id") ||
    Object.hasOwn(value, "expected_latest_revision")
  )
    throw new Error("profile_input_invalid");
  const body = parse("ProfileRevisionCreateRequest", {
    ...value,
    profile_id: id,
    expected_latest_revision: expected,
  });
  if (new TextEncoder().encode(JSON.stringify(body)).byteLength > PROFILE_JSON_BYTES)
    throw new Error("profile_input_invalid");
  return body;
}

export function validateProfileIntent(body: ProfileRequest, key: string) {
  validateRequest("createProfileRevision", {
    path: {},
    query: {},
    headers: { "idempotency-key": key },
    body,
  });
  if (new TextEncoder().encode(JSON.stringify(body)).byteLength > PROFILE_JSON_BYTES)
    throw new Error("profile_input_invalid");
}

function same(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (
    !a ||
    !b ||
    typeof a !== "object" ||
    typeof b !== "object" ||
    Array.isArray(a) !== Array.isArray(b)
  )
    return false;
  const left = a as Record<string, unknown>,
    right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  return (
    keys.length === Object.keys(right).length &&
    keys.every((key) => Object.hasOwn(right, key) && same(left[key], right[key]))
  );
}

/** Shape validity alone cannot resolve another profile/revision/definition's intent. */
export function acceptedProfile(body: ProfileRequest, value: unknown): ProfileRevision {
  validateResponse("createProfileRevision", 201, { body: value });
  const result = parseProfile("ScenarioProfileRevision", value);
  const expected =
    body.expected_latest_revision === null
      ? "1"
      : String(BigInt(body.expected_latest_revision) + 1n);
  if (
    result.revision !== expected ||
    result.profile_id !== body.profile_id ||
    !same(profileDefinition(body), profileDefinition(result))
  )
    throw new Error("profile_response_mismatch");
  return result;
}

const deadline = (signal?: AbortSignal) =>
  AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(15000)]);

export class ProfileClient implements ProfileApi {
  readonly #client: ReturnType<typeof createValidatedClient>;
  constructor(baseUrl: string, fetch: typeof globalThis.fetch = globalThis.fetch) {
    const origin = new URL(baseUrl).origin;
    this.#client = createValidatedClient(baseUrl, PROFILE_RESPONSE_BYTES, (input, init) => {
      const request = new Request(input, { ...init, redirect: "error", cache: "no-store" });
      if (new URL(request.url).origin !== origin) throw new Error("profile_origin_mismatch");
      return fetch(request);
    });
  }
  #check(response: Response, error: unknown) {
    if (!response.ok)
      throw new ProfileHttpError(response.status, parse("ErrorResponse", error).error.code);
  }
  async create(body: ProfileRequest, key: string, signal?: AbortSignal) {
    validateProfileIntent(body, key);
    const result = await this.#client.POST("/v1/nexus/admin/profiles", {
      params: { header: { "Idempotency-Key": key } },
      body,
      signal: deadline(signal),
    });
    this.#check(result.response, result.error);
    return acceptedProfile(body, result.data);
  }
  async read(id: string, revision: string, signal?: AbortSignal) {
    const result = await this.#client.GET(
      "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
      { params: { path: { profile_id: id, revision } }, signal: deadline(signal) },
    );
    this.#check(result.response, result.error);
    const value = parseProfile("ScenarioProfileRevision", result.data);
    if (value.profile_id !== id || value.revision !== revision)
      throw new Error("profile_response_mismatch");
    return value;
  }
}
