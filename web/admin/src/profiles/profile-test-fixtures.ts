/** Synthetic contract material imported only by tests. */
import { parse } from "../../../../packages/contracts/src/admin";
import { parse as parseProfile } from "../../../../packages/contracts/src/scenario_profiles";
import fixtures from "../../../../tests/fixtures/contracts/consumer-profiles.json";
import type { ProfileRequest } from "./profile-client";

export function request(changes: Partial<ProfileRequest> = {}) {
  const value = fixtures.examples.find(
    (e) => e.schema === "ProfileRevisionCreateRequest" && e.valid,
  )!.value;
  return parse("ProfileRevisionCreateRequest", { ...structuredClone(value), ...changes });
}

export function draft(body = request()) {
  const { expected_latest_revision: expected, ...definition } = body;
  return parseProfile("ScenarioProfileRevision", {
    ...definition,
    schema_version: "1.0.0",
    revision: expected === null ? "1" : String(BigInt(expected) + 1n),
    supersedes_revision: expected,
    publication: { status: "draft", published_at: null, retired_at: null, disabled_reason: null },
  });
}

export function failure(code = "profile_result_unknown", status = 503) {
  return Response.json(
    {
      error: {
        code,
        type: status === 503 ? "service_unavailable_error" : "conflict_error",
        message: "PRIVATE_PROVIDER_DETAIL",
        param: null,
        request_id: "req-profile-test",
        retryable: false,
      },
    },
    { status },
  );
}

export function storage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
  };
}
