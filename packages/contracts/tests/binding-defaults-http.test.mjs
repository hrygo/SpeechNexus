import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  validateRequest,
  validateResponse,
  ContractValidationError,
} from "../src/binding_defaults-http.ts";

const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/binding-defaults-http.json", import.meta.url),
  ),
).examples;
for (const example of examples) {
  test(`contract-only default producer: ${example.id}`, () => {
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
