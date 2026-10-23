import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createProfileParser } from "../src/profile-runtime.mjs";
import bundle from "../src/speechnexus_contracts/coordination.schema.json" with { type: "json" };

const parse = createProfileParser(bundle);
const fixtures = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/execution-recovery.json", import.meta.url),
  ),
);
for (const example of fixtures.examples) {
  test(example.id, () => {
    const value = structuredClone(example.value);
    const before = structuredClone(value);
    if (example.valid) {
      assert.equal(parse(example.schema, value), value);
    } else {
      assert.throws(() => parse(example.schema, value), {
        name: "ContractValidationError",
        message: `Invalid ${example.schema}`,
      });
    }
    assert.deepEqual(value, before);
  });
}
