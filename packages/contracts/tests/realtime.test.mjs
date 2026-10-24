import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as realtime from "../src/realtime.ts";
import * as events from "../src/realtime_events.ts";
import * as runtime from "../src/runtime.ts";
import { exampleValue } from "./contract-examples.mjs";

const traces = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/transcription-sequences.json", import.meta.url),
  ),
).examples;
for (const trace of traces) {
  test(`trace frame shapes only: ${trace.id}`, () => {
    assert.equal(typeof trace.semantic_valid, "boolean");
    assert.ok(trace.rule);
    for (const step of trace.steps) {
      if (!Object.hasOwn(step, "value")) {
        assert.equal(step.transport, "connection_lost");
        continue;
      }
      const value = exampleValue(step);
      assert.equal(events.parse("TranscriptionEvent", value) === value, true);
      assert.equal(events.parse("RealtimeEvent", value) === value, true);
    }
  });
}

test("public realtime imports no client defaults or internal control entrypoints", () => {
  assert.equal(Object.hasOwn(realtime, "createClient"), false);
  assert.equal(Object.hasOwn(events, "createClient"), false);
  assert.throws(
    () => realtime.parse("RuntimeInstanceIntentRequest", {}),
    /Unknown profile contract/,
  );
  assert.throws(() => runtime.parse("RealtimeSession", {}), /Unknown profile contract/);
  assert.throws(
    () => events.parse("RuntimeInstanceIntentResponse", {}),
    /Unknown profile contract/,
  );
});
