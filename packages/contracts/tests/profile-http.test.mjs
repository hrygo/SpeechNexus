import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  createValidatedClient,
  validateRequest,
  validateResponse,
  ContractValidationError,
} from "../src/profile-http.ts";

const examples = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/profile-http.json", import.meta.url)),
).examples;
const sample = (id) => structuredClone(examples.find((example) => example.id === id));

for (const example of examples) {
  test(`profile HTTP: ${example.id}`, () => {
    const value = structuredClone(example.value);
    const check = () =>
      example.kind === "request"
        ? validateRequest(example.operation, value)
        : validateResponse(example.operation, example.status, value);
    if (example.valid) assert.equal(check(), value);
    else assert.throws(check, ContractValidationError);
    assert.deepEqual(value, example.value);
  });
}

for (const keyed of [true, false]) {
  test(`unknown create has no implicit retry or fabricated recovery URI: keyed=${keyed}`, async () => {
    const request = sample(keyed ? "create-original" : "create-keyless").value;
    const unknown = sample("create-unknown");
    const success = sample("create-draft");
    const calls = [];
    const client = createValidatedClient("http://deployment.invalid", 262144, async (wire) => {
      calls.push({
        method: wire.method,
        key: wire.headers.get("Idempotency-Key"),
        body: await wire.json(),
      });
      const reply = calls.length === 1 ? unknown : success;
      return Response.json(reply.value.body, { status: reply.status });
    });
    const first = await client.POST("/v1/nexus/admin/profiles", {
      params: { header: request.headers ?? {} },
      body: request.body,
    });
    assert.equal(first.response.status, 503);
    assert.equal(first.error.error.retryable, false);
    assert.equal(Object.hasOwn(first.error.error, "nexus"), false);
    assert.equal(calls.length, 1);
    if (keyed) {
      const second = await client.POST("/v1/nexus/admin/profiles", {
        params: { header: request.headers },
        body: request.body,
      });
      assert.equal(second.response.status, 201);
      assert.equal(second.data.publication.status, "draft");
      assert.deepEqual(calls[0], calls[1]);
    } else assert.equal(calls[0].key, null);
  });
}

test("unreviewed profile methods fail before transport", async () => {
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 262144, async () => {
    calls++;
    throw new Error("Transport must not be reached");
  });
  await assert.rejects(client.GET("/v1/nexus/admin/profiles"), /Unknown HTTP operation/);
  assert.equal(calls, 0);
});
