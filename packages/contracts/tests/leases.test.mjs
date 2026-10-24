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

for (const command of examples.valid_commands.filter(
  (item) => item.operation === "quarantine_lease",
)) {
  test("shared quarantine request preserves original reason and observation", () => {
    assert.equal(parse("quarantine_lease", command.request), command.request);
  });
}
for (const example of examples.invalid_commands.filter(
  (item) => item.command.operation === "quarantine_lease",
)) {
  test(`shared quarantine rejects ${example.reason}`, () => {
    assert.throws(() => parse("quarantine_lease", example.command.request), {
      name: "ContractValidationError",
      message: "Invalid quarantine_lease",
    });
  });
}
for (const result of examples.valid_results.filter(
  (item) => item.operation === "quarantine_lease",
)) {
  test("shared quarantine snapshot retains string revision", () => {
    assert.equal(parse("TransactionResult", result), result);
    assert.equal(result.value.lease_state, "quarantined");
    assert.equal(typeof result.value.version, "string");
  });
}
