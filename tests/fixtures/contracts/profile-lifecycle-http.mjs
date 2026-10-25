// Synthetic loopback contract consumer: no publisher or trusted evidence exists here.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createValidatedClient } from "../../../packages/contracts/src/profile_lifecycle-http.ts";
import { createValidatedClient as createDraftClient } from "../../../packages/contracts/src/profile-http.ts";

const [base, prefix, keyedValue] = process.argv.slice(2);
assert.ok(["publish", "activate"].includes(prefix));
const keyed = keyedValue === "true";
const examples = JSON.parse(
  readFileSync(new URL("./profile-lifecycle-http.json", import.meta.url)),
).examples;
const request = structuredClone(
  examples.find((e) => e.id === prefix + (keyed ? "-original" : "-keyless")).value,
);
const client = createValidatedClient(base, 262144);
const route =
  prefix === "publish"
    ? "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications"
    : "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/activations";
const options = { params: { path: request.path, header: request.headers }, body: request.body };
const first = await client.POST(route, options);
assert.equal(first.response.status, 503);
assert.equal(first.error.error.retryable, false);
assert.equal(Object.hasOwn(first.error.error, "nexus"), false);
if (keyed) {
  const read = await createDraftClient(base, 262144).GET(
    "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
    { params: { path: request.path } },
  );
  assert.equal(read.response.status, 503);
  assert.equal(read.error.error.code, "profile_unavailable");
  // Explicit recovery retains the entire original route/body/key after the read failed.
  const replay = await client.POST(route, options);
  assert.equal(replay.response.status, 200);
  assert.equal(replay.data.publication.status, "published");
  assert.equal(replay.data.profile_id, request.path.profile_id);
  assert.equal(replay.data.revision, request.path.revision);
}
