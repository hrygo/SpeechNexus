import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ContractValidationError, parse } from "../src/runtime.mjs";

const fixtures = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/file-workflow.json", import.meta.url)),
);
for (const example of fixtures.examples) {
  test(example.id, () => {
    const value = structuredClone(example.value);
    const before = structuredClone(value);
    if (example.valid) {
      assert.equal(parse(example.schema, value), value);
    } else {
      assert.throws(() => parse(example.schema, value), {
        name: ContractValidationError.name,
        message: `Invalid ${example.schema}`,
      });
    }
    assert.deepEqual(value, before, "Boundary validation must not coerce or mutate input");
  });
}
test("unknown contracts fail closed", () => {
  assert.throws(() => parse("NotAContract", {}), /Unknown file contract/);
});
