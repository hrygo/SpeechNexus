import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createProfileParser } from "../src/profile-runtime.mjs";
import bundle from "../src/speechnexus_contracts/coordination.schema.json" with { type: "json" };

const parse = createProfileParser(bundle);
const fixtures = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/coordination/transaction-examples.json", import.meta.url),
  ),
);

for (const command of fixtures.valid_commands.filter((item) =>
  ["create_runtime_intent", "observe_runtime"].includes(item.operation),
)) {
  test(`runtime coordination accepts the shared ${command.operation} request`, () => {
    const value = structuredClone(command.request);
    const before = structuredClone(value);
    assert.equal(parse(command.operation, value), value);
    assert.deepEqual(value, before);
  });
}

for (const [index, result] of fixtures.valid_results.entries()) {
  if (result.value?.kind !== "runtime") continue;
  test(`runtime coordination preserves shared snapshot ${index}`, () => {
    const value = structuredClone(result);
    const before = structuredClone(value);
    assert.equal(parse("TransactionResult", value), value);
    if (value.value.runtime === null) {
      assert.ok(value.value.request);
      assert.equal(value.value.observed_instance_state, undefined);
      assert.notEqual(value.value.intent_state, "succeeded");
    }
    assert.deepEqual(value, before);
  });
}

for (const example of fixtures.invalid_results) {
  test(`runtime coordination rejects ${example.reason}`, () => {
    const value = structuredClone(example.result);
    const before = structuredClone(value);
    assert.throws(() => parse("TransactionResult", value), {
      name: "ContractValidationError",
      message: "Invalid TransactionResult",
    });
    assert.deepEqual(value, before);
  });
}
