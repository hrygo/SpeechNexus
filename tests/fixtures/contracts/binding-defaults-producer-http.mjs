// Synthetic loopback fixture consumption, not a product route/storage test.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createValidatedClient } from "../../../packages/contracts/src/binding_defaults-http.ts";

const [base, useKey] = process.argv.slice(2);
const keyed = useKey === "true";
const cases = JSON.parse(
  readFileSync(new URL("binding-defaults-http.json", import.meta.url)),
).examples;
const request = structuredClone(
  cases.find((e) => e.id === (keyed ? "defaults-create-sparse" : "defaults-create-keyless")).value,
);
let calls = 0;
const client = createValidatedClient(base, 262144, async (...args) => {
  calls++;
  return fetch(...args);
});
const send = () =>
  client.POST("/v1/nexus/admin/model-bindings", {
    params: { header: request.headers ?? {} },
    body: request.body,
  });
const unknown = await send();
assert.equal(unknown.response.status, 503);
assert.equal(unknown.error.error.retryable, false);
assert.equal(calls, 1);
if (!keyed) {
  assert.equal(unknown.data, undefined);
  process.exit(0);
}
const replay = await send();
assert.equal(replay.response.status, 201);
assert.equal(calls, 2);
const path = "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults";
const fixed = { params: { path: { binding_id: "asr-quality", revision: "1" } } };
const unavailable = await client.GET(path, fixed);
assert.equal(unavailable.response.status, 503);
assert.equal(unavailable.error.error.retryable, true);
assert.equal(calls, 3);
assert.equal(unavailable.data, undefined);
const known = await client.GET(path, fixed);
assert.equal(known.response.status, 200);
assert.deepEqual(known.data.parameters, request.body.parameter_defaults);
assert.equal(calls, 4);
const missing = await client.GET(path, {
  params: { path: { binding_id: "asr-quality", revision: "2" } },
});
assert.equal(missing.response.status, 404);
assert.equal(missing.error.error.code, "binding_defaults_not_found");
assert.equal(missing.data, undefined);
assert.equal(calls, 5);
