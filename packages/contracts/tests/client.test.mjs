import assert from "node:assert/strict";
import test from "node:test";
import { createSpeechClient, parse } from "../src/index.ts";

test("typed client uses only the supplied deployment and preserves canonical input", async () => {
  const command = parse("JobCreateRequest", {
    kind: "asr",
    model: "asr-quality",
    input: { audio_asset_id: "asset-demo" },
  });
  let requests = 0;
  const client = createSpeechClient("http://deployment.invalid", async (request) => {
    requests++;
    assert.equal(request.url, "http://deployment.invalid/v1/nexus/jobs");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.get("Idempotency-Key"), "synthetic-key");
    assert.deepEqual(await request.json(), command);
    return Response.json(
      {
        error: {
          code: "operation_in_progress",
          type: "invalid_request_error",
          message: "Query original operation.",
          param: null,
          request_id: "req-demo",
          retryable: false,
          nexus: { operation_id: "op-demo" },
        },
      },
      { status: 409 },
    );
  });
  const response = await client.POST("/v1/nexus/jobs", {
    headers: { "Idempotency-Key": "synthetic-key" },
    body: command,
  });
  const error = parse("ErrorResponse", response.error);
  assert.equal(error.error.nexus.operation_id, "op-demo");
  assert.equal(requests, 1, "Unknown/in-progress results must not create a new execution");
});
