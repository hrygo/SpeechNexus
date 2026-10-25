import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ContractValidationError,
  createValidatedClient,
  validateRequest,
  validateResponse,
} from "../src/profile_lifecycle-http.ts";

const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/profile-lifecycle-http.json", import.meta.url),
  ),
).examples;

for (const example of examples) {
  test(`future profile lifecycle: ${example.id}`, () => {
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

test("future lifecycle client rejects an unselected draft write before transport", async () => {
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 262144, async () => {
    calls++;
    throw new Error("unselected operation reached transport");
  });
  await assert.rejects(client.POST("/v1/nexus/admin/profiles", { body: {} }));
  assert.equal(calls, 0);
});
