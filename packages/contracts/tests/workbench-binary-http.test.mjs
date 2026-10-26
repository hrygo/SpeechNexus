import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import test from "node:test";
import { createValidatedClient, validateResponse } from "../src/workbench-http.ts";

const path = "/v1/workbench/saved-assets/{asset_id}/content";
const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/workbench-binary-http.json", import.meta.url),
  ),
).examples;
const callOptions = {
  params: { path: { asset_id: "asset-demo" }, header: { Range: "bytes=0-3" } },
  parseAs: "stream",
};

for (const example of examples) {
  test(`binary-${example.id}`, () => {
    const value = structuredClone(example.value);
    if (example.body_kind === "bytes") value.body = Uint8Array.from(value.body);
    const before = structuredClone(value);
    const call = () => validateResponse(example.operation, example.status, value);
    if (example.valid) assert.equal(call(), value);
    else assert.throws(call, { name: "ContractValidationError", message: "Invalid HTTP response" });
    assert.deepEqual(value, before);
  });
}

test(
  "download returns before EOF, keeps original range, and caller abort closes the native stream",
  { timeout: 5000 },
  async () => {
    let observed;
    let closed;
    const connectionClosed = new Promise((resolve) => {
      closed = resolve;
    });
    const server = createServer((request, response) => {
      observed = { method: request.method, url: request.url, range: request.headers.range };
      response.on("close", closed);
      response.writeHead(206, {
        "Content-Type": "application/octet-stream",
        "Content-Range": "bytes 0-3/8",
      });
      response.write(Buffer.from([0, 255, 1, 2]));
      // Deliberately never end: a buffering/JSON middleware cannot return.
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const controller = new AbortController();
    try {
      const client = createValidatedClient(`http://127.0.0.1:${server.address().port}`, 1);
      const result = await client.GET(path, { ...callOptions, signal: controller.signal });
      assert.equal(result.response.status, 206);
      const reader = result.data.getReader();
      assert.deepEqual(Array.from((await reader.read()).value), [0, 255, 1, 2]);
      assert.deepEqual(observed, {
        method: "GET",
        url: "/v1/workbench/saved-assets/asset-demo/content",
        range: "bytes=0-3",
      });
      controller.abort();
      await assert.rejects(reader.read());
      await connectionClosed;
    } finally {
      controller.abort();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  },
);

test(
  "native download checks media/errors/status, rejects redirects and validates before I/O",
  { timeout: 5000 },
  async () => {
    const replies = [200, 206, 401, 404, 417, 200, 302];
    let calls = 0;
    let follows = 0;
    const server = createServer((request, response) => {
      if (request.url === "/redirect-target") {
        follows++;
        response.end();
        return;
      }
      const index = calls++;
      const status = replies[index];
      assert.equal(request.headers.range, "bytes=0-3");
      if (status === 302) {
        response.writeHead(status, { Location: "/redirect-target" });
        response.end();
      } else if (status === 200 || status === 206) {
        response.writeHead(status, {
          "Content-Type": index === 5 ? "application/json" : "application/octet-stream",
        });
        response.end(Buffer.from([0, 255]));
      } else {
        response.writeHead(status, { "Content-Type": "application/json" });
        response.end(
          JSON.stringify(
            examples.find((e) => e.status === (status === 417 ? 404 : status) && e.valid).value
              .body,
          ),
        );
      }
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      const client = createValidatedClient(`http://127.0.0.1:${server.address().port}`, 4096);
      for (const status of [200, 206]) {
        const result = await client.GET(path, callOptions);
        assert.equal(result.response.status, status);
        const bytes = await new Response(result.data).arrayBuffer();
        assert.deepEqual(Array.from(new Uint8Array(bytes)), [0, 255]);
      }
      for (const status of [401, 404]) {
        const result = await client.GET(path, callOptions);
        assert.equal(result.response.status, status);
        assert.deepEqual(
          result.error,
          examples.find((e) => e.status === status && e.valid).value.body,
        );
      }
      for (let i = 0; i < 3; i++) await assert.rejects(client.GET(path, callOptions));
      assert.equal(calls, 7);
      assert.equal(follows, 0);
      await assert.rejects(client.GET(path, { params: callOptions.params }));
      await assert.rejects(
        client.GET(path, {
          ...callOptions,
          params: { ...callOptions.params, header: { Range: "bytes=0-3,8-9" } },
        }),
      );
      assert.equal(calls, 7);
    } finally {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  },
);

test("binary metadata validation never locks or consumes the caller stream", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array([1]));
    },
  });
  const value = { body: stream };
  assert.equal(validateResponse("downloadSavedAsset", 200, value), value);
  assert.equal(stream.locked, false);
  await stream.cancel();
});
