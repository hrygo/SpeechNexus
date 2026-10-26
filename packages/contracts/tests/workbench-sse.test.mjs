import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { readEvents, validateEvent, validateResponse } from "../src/workbench-http.ts";

const corpus = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/workbench-sse.json", import.meta.url)),
);
for (const event of corpus.events) {
  test("source-export-event-" + event.type, () => {
    const before = structuredClone(event);
    assert.equal(validateEvent("streamExportEvents", event), event);
    assert.deepEqual(event, before);
  });
}
for (const example of corpus.streams)
  for (const size of [1, 7, 4096]) {
    test("bounded-sse-" + example.id + "-" + size, async () => {
      const wire =
        example.wire_kind === "utf8"
          ? new TextEncoder().encode(example.wire)
          : Uint8Array.from(example.wire);
      let offset = 0;
      let cancelled = false;
      let ended = false;
      const stream = new ReadableStream({
        pull(controller) {
          if (offset >= wire.length) {
            ended = true;
            controller.close();
          } else {
            controller.enqueue(wire.slice(offset, offset + size));
            offset += size;
          }
        },
        cancel() {
          cancelled = true;
        },
      });
      const values = [];
      async function collect() {
        for await (const event of readEvents(
          "streamExportEvents",
          stream,
          example.request,
          example.max_event_bytes,
        ))
          values.push(event);
      }
      if (example.valid) await collect();
      else {
        await assert.rejects(collect, {
          name: "ContractValidationError",
          message: "Invalid SSE stream",
        });
        assert.equal(cancelled || ended, true);
      }
      assert.deepEqual(values, example.expected);
      assert.equal(stream.locked, false);
    });
  }
test("SSE metadata validation never locks or reads a stream", async () => {
  const body = new ReadableStream();
  const envelope = { body, headers: { "Cache-Control": "no-cache" } };
  assert.equal(validateResponse("streamExportEvents", 200, envelope), envelope);
  assert.equal(body.locked, false);
  await body.cancel();
});
