import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createHttpValidator } from "../src/http-runtime.mjs";
import { createProfileParser } from "../src/profile-runtime.mjs";
import bundle from "../src/speechnexus_contracts/runtime-http.schema.json" with { type: "json" };
import runtime from "../src/speechnexus_contracts/runtime.schema.json" with { type: "json" };
import { createValidatedClient, ContractValidationError } from "../src/runtime-http.ts";

const validator = createHttpValidator(bundle);
const parse = createProfileParser(runtime);
const examples = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/runtime-http.json", import.meta.url)),
).examples;

for (const example of examples) {
  test(`runtime HTTP: ${example.id}`, () => {
    const value = structuredClone(example.value);
    const before = structuredClone(value);
    const check = () =>
      example.kind === "request"
        ? validator.validateRequest(example.operation, value)
        : validator.validateResponse(example.operation, example.status, value);
    if (example.valid) {
      assert.equal(check(), value);
      if (example.status && example.status !== 200) {
        assert.equal(parse("RuntimeControlErrorResponse", value.body), value.body);
      }
    } else {
      assert.throws(check, { name: "ContractValidationError" });
    }
    assert.deepEqual(value, before);
  });
}

test("the generated typed runtime client submits the original body once", async () => {
  const sample = examples.find((example) => example.id === "submit-unchanged");
  const ready = examples.find((example) => example.id === "submitRuntimeInstanceIntent-ready");
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 8192, async (request) => {
    calls++;
    assert.equal(new URL(request.url).pathname, "/internal/v1/nodes/host-test/instance-intents");
    assert.deepEqual(await request.json(), sample.value.body);
    return Response.json(ready.value.body);
  });
  const result = await client.POST("/internal/v1/nodes/{node_id}/instance-intents", {
    params: { path: sample.value.path, header: sample.value.headers },
    body: sample.value.body,
  });
  assert.deepEqual(result.data, ready.value.body);
  assert.equal(calls, 1);
});

test("invalid typed runtime paths never fetch and a declared 404 never retries", async () => {
  const missing = examples.find((example) => example.id === "original-query-not-found");
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 8192, async () => {
    calls++;
    return Response.json(missing.value.body, { status: 404 });
  });
  await assert.rejects(
    client.GET("/internal/v1/nodes/{node_id}/instance-intents/{intent_id}", {
      params: { path: { node_id: "../node", intent_id: "runtime-new" } },
    }),
    ContractValidationError,
  );
  assert.equal(calls, 0);
  const result = await client.GET("/internal/v1/nodes/{node_id}/instance-intents/{intent_id}", {
    params: { path: { node_id: "host-test", intent_id: "runtime-new" } },
  });
  assert.deepEqual(result.error, missing.value.body);
  assert.equal(calls, 1);
});
