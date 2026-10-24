import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createProfileParser } from "../src/profile-runtime.mjs";
import bundle from "../src/speechnexus_contracts/coordination.schema.json" with { type: "json" };

const parse = createProfileParser(bundle);
const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/coordination/transaction-examples.json", import.meta.url),
  ),
);

for (const command of examples.valid_commands.filter((item) =>
  ["quarantine_lease", "release_lease"].includes(item.operation),
)) {
  test(`shared ${command.operation} request preserves its original fields`, () => {
    assert.equal(parse(command.operation, command.request), command.request);
  });
}
for (const example of examples.invalid_commands.filter((item) =>
  ["quarantine_lease", "release_lease"].includes(item.command.operation),
)) {
  test(`shared ${example.command.operation} rejects ${example.reason}`, () => {
    assert.throws(() => parse(example.command.operation, example.command.request), {
      name: "ContractValidationError",
      message: `Invalid ${example.command.operation}`,
    });
  });
}
for (const result of examples.valid_results.filter((item) =>
  ["quarantine_lease", "release_lease"].includes(item.operation),
)) {
  test(`shared ${result.operation} snapshot retains string revision`, () => {
    assert.equal(parse("TransactionResult", result), result);
    assert.equal(
      result.value.lease_state,
      result.operation === "release_lease" ? "released" : "quarantined",
    );
    assert.equal(typeof result.value.version, "string");
  });
}
