import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import test from "node:test";
import {
  validateRequest,
  validateResponse,
  createValidatedClient,
  ContractValidationError,
} from "../src/workbench-http.ts";

const examples = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/workbench-http.json", import.meta.url)),
).examples;
const sample = (operation, kind, status) =>
  structuredClone(
    examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === kind &&
        e.valid &&
        (status === undefined || e.status === status),
    ).value,
  );

for (const example of examples) {
  test(`http-${example.id}`, () => {
    const value = structuredClone(example.value);
    const call = () =>
      example.kind === "request"
        ? validateRequest(example.operation, value)
        : validateResponse(example.operation, example.status, value);
    if (example.valid) assert.equal(call(), value);
    else
      assert.throws(call, {
        name: "ContractValidationError",
        message: `Invalid HTTP ${example.kind}`,
      });
    assert.deepEqual(value, example.value);
  });
}

test("HTTP client keeps the exact original key and body and validates success", async () => {
  const body = sample("createTranscript", "request").body;
  const response = sample("createTranscript", "response", 201).body;
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 8192, async (request) => {
    calls++;
    assert.equal(
      request.url,
      "http://deployment.invalid/v1/workbench/projects/project-demo/transcripts",
    );
    assert.equal(request.headers.get("Idempotency-Key"), "original-synthetic-key");
    assert.equal(request.redirect, "error");
    assert.deepEqual(await request.json(), body);
    return Response.json(response, { status: 201 });
  });
  const result = await client.POST("/v1/workbench/projects/{project_id}/transcripts", {
    params: {
      path: { project_id: "project-demo" },
      header: { "Idempotency-Key": "original-synthetic-key" },
    },
    body,
  });
  assert.deepEqual(result.data, response);
  assert.equal(calls, 1);
});

test("invalid path, header, query or body fails before any network call", async () => {
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 8192, async () => {
    calls++;
    throw new Error("Unexpected network call");
  });
  const invalid = [
    () =>
      client.GET("/v1/workbench/projects/{project_id}", {
        params: { path: { project_id: "../private" } },
      }),
    () => client.GET("/v1/workbench/projects", { params: { query: { limit: 101 } } }),
    () =>
      client.POST("/v1/workbench/deletion-plans", {
        body: sample("workbenchCreateDeletionPlan", "request").body,
      }),
    () =>
      client.POST("/v1/workbench/projects", {
        body: { name: "Synthetic", private_field: "synthetic-private-marker" },
      }),
    () =>
      client.GET("/v1/workbench/projects", {
        params: { query: { limit: 1 } },
        querySerializer: () => "limit=1&limit=2",
      }),
    () =>
      client.GET("/v1/workbench/projects/{project_id}", {
        params: { path: { project_id: "project-demo" } },
        pathSerializer: () => "http://deployment.invalid/v1/workbench/projects/other-project",
      }),
    () => client.GET("/v1/workbench/projects", { redirect: "follow" }),
    () => client.GET("/v1/workbench/projects", { baseUrl: "http://unapproved.invalid" }),
    () =>
      client.POST("/v1/workbench/deletion-plans", {
        params: { header: { "Idempotency-Key": "x", "idempotency-key": "y" } },
        body: sample("workbenchCreateDeletionPlan", "request").body,
      }),
  ];
  for (const call of invalid) await assert.rejects(call, ContractValidationError);
  assert.equal(calls, 0);
});

test("typed and already serialized scalar queries stay equivalent without defaults", async () => {
  const response = sample("listProjects", "response", 200).body;
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 8192, async (request) => {
    calls++;
    assert.equal(new URL(request.url).searchParams.get("limit"), "50");
    return Response.json(response);
  });
  await client.GET("/v1/workbench/projects", { params: { query: { limit: 50 } } });
  assert.equal(calls, 1);
});

test("declared error is preserved once; invalid status or body never retries", async () => {
  const error = sample("listProjects", "response", 503).body;
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 8192, async () => {
    calls++;
    return Response.json(error, { status: 503, headers: { "Retry-After": "1" } });
  });
  const result = await client.GET("/v1/workbench/projects");
  assert.deepEqual(result.error, error);
  assert.equal(calls, 1);
  for (const [status, body] of [
    [299, error],
    [200, error],
    [503, { ...error, private_field: "synthetic-private-marker" }],
  ]) {
    const invalid = createValidatedClient("http://deployment.invalid", 8192, async () => {
      calls++;
      return Response.json(body, { status });
    });
    const before = calls;
    await assert.rejects(() => invalid.GET("/v1/workbench/projects"), {
      name: "ContractValidationError",
      message: "Invalid HTTP response",
    });
    assert.equal(calls, before + 1);
  }
});

test("malformed JSON and non-JSON diagnostics become a safe boundary error", async () => {
  for (const type of ["application/json", "text/plain"]) {
    const client = createValidatedClient(
      "http://deployment.invalid",
      8192,
      async () =>
        new Response("synthetic-private-marker", {
          status: 200,
          headers: { "Content-Type": type },
        }),
    );
    await assert.rejects(() => client.GET("/v1/workbench/projects"), {
      name: "ContractValidationError",
      message: "Invalid HTTP response",
    });
  }
});

test("byte limit checks actual request bytes before fetching", async () => {
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 16, async () => {
    calls++;
    throw new Error();
  });
  await assert.rejects(
    () => client.POST("/v1/workbench/projects", { body: { name: "Synthetic oversized name" } }),
    ContractValidationError,
  );
  assert.equal(calls, 0);
});

test(
  "oversized response cancels both tee branches even with a false length",
  { timeout: 2000 },
  async () => {
    let cancelled;
    const stopped = new Promise((resolve) => {
      cancelled = resolve;
    });
    const client = createValidatedClient(
      "http://deployment.invalid",
      16,
      async () =>
        new Response(
          new ReadableStream({
            pull(controller) {
              controller.enqueue(new Uint8Array(32));
            },
            cancel() {
              cancelled();
            },
          }),
          { headers: { "Content-Type": "application/json", "Content-Length": "0" } },
        ),
    );
    await assert.rejects(() => client.GET("/v1/workbench/projects"), ContractValidationError);
    await stopped;
  },
);

test("unknown operations and implicit/unbounded byte limits are rejected", () => {
  assert.throws(() => validateRequest("createMeeting", {}), /Unknown HTTP operation/);
  for (const limit of [undefined, 0, -1, Infinity, 1.5]) {
    assert.throws(
      () => createValidatedClient("http://deployment.invalid", limit),
      /positive safe JSON byte limit/,
    );
  }
});

test("native loopback HTTP validates a reply and never follows a POST redirect", async () => {
  let calls = 0;
  let targetCalls = 0;
  const target = createServer((request, response) => {
    targetCalls++;
    request.resume();
    response.end();
  });
  const server = createServer((request, response) => {
    calls++;
    request.resume();
    if (request.method === "POST") {
      response.writeHead(307, { Location: `http://127.0.0.1:${target.address().port}/redirected` });
      response.end();
    } else {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(sample("listProjects", "response", 200).body));
    }
  });
  const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const close = (server) =>
    new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  try {
    await listen(target);
    await listen(server);
    const client = createValidatedClient(`http://127.0.0.1:${server.address().port}`, 8192);
    const result = await client.GET("/v1/workbench/projects");
    assert.deepEqual(result.data, sample("listProjects", "response", 200).body);
    await assert.rejects(() =>
      client.POST("/v1/workbench/projects", {
        body: sample("createProject", "request").body,
      }),
    );
    assert.equal(calls, 2);
    assert.equal(targetCalls, 0);
  } finally {
    await close(server);
    await close(target);
  }
});
