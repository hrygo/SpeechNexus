import assert from "node:assert/strict";
import {
  createValidatedClient,
  readEvents,
} from "../../../packages/contracts/src/workbench-http.ts";

const client = createValidatedClient(process.argv[2], 8192);
const path = "/v1/workbench/exports/{export_id}/content";
for (const status of [200, 206, 401, 404, 409, 410, 416]) {
  const options = {
    params: { path: { export_id: "export-" + status }, header: { Range: "bytes=0-3" } },
    parseAs: "stream",
  };
  if (status === 416) await assert.rejects(client.GET(path, options));
  else {
    const result = await client.GET(path, options);
    assert.equal(result.response.status, status);
    if (status === 200 || status === 206) {
      assert.deepEqual(
        Array.from(new Uint8Array(await new Response(result.data).arrayBuffer())),
        status === 200 ? [0, 255] : [0, 255, 1, 2],
      );
    } else assert.equal(typeof result.error.error.code, "string");
  }
}
const binary = await client.GET(path, {
  params: { path: { export_id: "export-stream" }, header: { Range: "bytes=0-3" } },
  parseAs: "stream",
});
const reader = binary.data.getReader();
assert.deepEqual(Array.from((await reader.read()).value), [0, 255, 1, 2]);
await reader.cancel();
reader.releaseLock();
let cursor = "9007199254740993";
const eventPath = "/v1/workbench/exports/{export_id}/events";
await assert.rejects(
  client.GET(eventPath, {
    params: { path: { export_id: "export_demo" }, header: { "Last-Event-ID": cursor } },
  }),
);
await assert.rejects(
  client.GET(eventPath, {
    params: { path: { export_id: "export_demo" }, header: { "Last-Event-ID": "0" } },
    parseAs: "stream",
  }),
);
await assert.rejects(
  client.GET(path, {
    params: { path: { export_id: "export-200" }, header: { Range: "bytes=0-3,9-10" } },
    parseAs: "stream",
  }),
);
for (let i = 0; i < 2; i++) {
  const request = { path: { export_id: "export_demo" }, headers: { "Last-Event-ID": cursor } };
  const response = await client.GET(eventPath, {
    params: { path: request.path, header: request.headers },
    parseAs: "stream",
  });
  const events = readEvents("streamExportEvents", response.data, request, 8192)[
    Symbol.asyncIterator
  ]();
  const { value } = await events.next();
  assert.equal(value.id, (BigInt(cursor) + 1n).toString());
  cursor = value.id;
  await events.return();
}
const gone = await client.GET(eventPath, {
  params: { path: { export_id: "export_demo" }, header: { "Last-Event-ID": cursor } },
  parseAs: "stream",
});
assert.equal(gone.response.status, 410);
assert.equal(typeof gone.error.error.code, "string");
const request = {
  path: { export_id: "export_broken" },
  headers: { "Last-Event-ID": "9007199254740993" },
};
const broken = await client.GET(eventPath, {
  params: { path: request.path, header: request.headers },
  parseAs: "stream",
});
const events = readEvents("streamExportEvents", broken.data, request, 8192)[Symbol.asyncIterator]();
const first = await events.next();
assert.equal(first.value.id, "9007199254740994");
await assert.rejects(events.next(), {
  name: "ContractValidationError",
  message: "Invalid SSE stream",
});
