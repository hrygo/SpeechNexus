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
const source = JSON.parse(
  readFileSync(
    new URL("../../../contracts/internal/coordination-transactions.schema.json", import.meta.url),
  ),
);

test("all 18 source ports and the result have runtime entrypoints", () => {
  const ports = source.oneOf
    .filter((branch) => branch.properties)
    .map((branch) => branch.properties.operation.const);
  assert.equal(new Set(ports).size, 18);
  assert.deepEqual(
    new Set(bundle["x-nexus-entrypoints"]),
    new Set([...ports, "TransactionResult"]),
  );
});

for (const [index, command] of fixtures.valid_commands.entries()) {
  test(`coordination accepts shared ${index}: ${command.operation}`, () => {
    const value = structuredClone(command.request);
    const before = structuredClone(value);
    assert.equal(parse(command.operation, value), value);
    assert.deepEqual(value, before);
  });
}
for (const [index, example] of fixtures.invalid_commands.entries()) {
  test(`coordination rejects shared ${index}: ${example.reason}`, () => {
    const value = structuredClone(example.command.request);
    const before = structuredClone(value);
    assert.throws(() => parse(example.command.operation, value), {
      name: "ContractValidationError",
    });
    assert.deepEqual(value, before);
  });
}
for (const [index, result] of fixtures.valid_results.entries()) {
  test(`coordination accepts complete shared result ${index}`, () => {
    const value = structuredClone(result);
    assert.equal(parse("TransactionResult", value), value);
    assert.deepEqual(value, result);
  });
}
