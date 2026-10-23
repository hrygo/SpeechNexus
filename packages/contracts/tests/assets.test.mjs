import assert from "node:assert/strict";
import test from "node:test";
import { createSpeechClient, uploadAsset, ContractValidationError } from "../src/index.ts";

const asset = {
  id: "asset-test",
  object: "nexus.asset",
  workspace_id: "workspace-default",
  purpose: "text_input",
  status: "ready",
  media_type: "text/plain; charset=utf-8",
  bytes: 4,
  sha256: "sha256:" + "a".repeat(64),
  created_at: "2026-10-23T00:00:00Z",
};

test("asset helper sends real multipart bytes without adding mutable defaults", async () => {
  let calls = 0;
  const file = new File(["text"], "private-name.txt", { type: "application/octet-stream" });
  const input = Object.freeze({ purpose: "text_input", file });
  const client = createSpeechClient("http://deployment.test", async (request) => {
    calls++;
    assert.equal(request.url, "http://deployment.test/v1/nexus/assets");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.get("Idempotency-Key"), "original-key");
    assert.match(request.headers.get("Content-Type"), /^multipart\/form-data; boundary=/);
    const form = await request.formData();
    assert.deepEqual([...form.keys()], ["purpose", "file"]);
    assert.equal(form.get("purpose"), "text_input");
    assert.equal(await form.get("file").text(), "text");
    assert.equal(form.get("file").name, "private-name.txt");
    return new Response(JSON.stringify(asset), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  });
  const result = await uploadAsset(client, input, { idempotencyKey: "original-key" });
  assert.deepEqual(result.data, asset);
  assert.equal(input.file, file);
  assert.equal(calls, 1);
});

test("asset helper returns the original recovery headers and never retries an unknown upload", async () => {
  let calls = 0;
  const error = {
    error: {
      code: "asset_upload_unknown",
      type: "service_unavailable_error",
      message: "Reconcile original upload.",
      param: null,
      request_id: "trace-test",
      retryable: false,
    },
  };
  const client = createSpeechClient("http://deployment.test", async () => {
    calls++;
    return new Response(JSON.stringify(error), {
      status: 503,
      headers: {
        "Content-Type": "application/json",
        Location: "/v1/nexus/assets/asset-test",
        "X-Should-Retry": "false",
      },
    });
  });
  const result = await uploadAsset(client, { purpose: "text_input", file: new Blob(["text"]) });
  assert.deepEqual(result.error, error);
  assert.equal(result.response.headers.get("Location"), "/v1/nexus/assets/asset-test");
  assert.equal(calls, 1);
});

test("asset helper validates metadata and public replies without exposing input diagnostics", async () => {
  let calls = 0;
  const client = createSpeechClient("http://deployment.test", async () => {
    calls++;
    return new Response(JSON.stringify({ ...asset, bytes: 0 }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  });
  await assert.rejects(
    uploadAsset(client, { purpose: "private-invalid-purpose", file: new Blob(["text"]) }),
    ContractValidationError,
  );
  assert.equal(calls, 0);
  await assert.rejects(
    uploadAsset(client, { purpose: "text_input", file: new Blob(["text"]) }),
    ContractValidationError,
  );
  assert.equal(calls, 1);
});

test("an explicit empty idempotency key is never silently converted to a new logical upload", async () => {
  const client = createSpeechClient("http://deployment.test", async (request) => {
    assert.equal(request.headers.has("Idempotency-Key"), true);
    assert.equal(request.headers.get("Idempotency-Key"), "");
    return new Response(null, { status: 204 });
  });
  await assert.rejects(
    uploadAsset(
      client,
      { purpose: "text_input", file: new Blob(["text"]) },
      { idempotencyKey: "" },
    ),
    ContractValidationError,
  );
});
