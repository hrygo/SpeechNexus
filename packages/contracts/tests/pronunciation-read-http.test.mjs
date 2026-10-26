import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ContractValidationError,
  createValidatedClient,
  validateRequest,
  validateResponse,
} from "../src/pronunciation_read-http.ts";

const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/pronunciation-read-http.json", import.meta.url),
  ),
).examples;
const sample = (id) => structuredClone(examples.find((value) => value.id === id));
const operation = "getPronunciationRevision";
const path = "/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}";
const params = { path: { pronunciation_id: "pron-synthetic-read", revision: "9007199254740993" } };

for (const example of examples) {
  test(`pronunciation fixed read: ${example.id}`, () => {
    const value = structuredClone(example.value);
    const check = () =>
      example.kind === "request"
        ? validateRequest(operation, value)
        : validateResponse(operation, example.status, value);
    if (example.valid) assert.equal(check(), value);
    else assert.throws(check, ContractValidationError);
    assert.deepEqual(value, example.value);
  });
}

test("the read client never sends unselected creates, lists or key-query operations", async () => {
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 4096, async () => {
    ++calls;
    throw Error("Transport must not be reached");
  });
  await assert.rejects(
    client.POST("/v1/nexus/pronunciation-sets", {
      body: { encoding: "ipa", entries: [{ grapheme: "synthetic", phonemes: "synthetic" }] },
    }),
    /Unknown HTTP operation/,
  );
  await assert.rejects(client.GET("/v1/nexus/pronunciation-sets"), /Unknown HTTP operation/);
  assert.throws(() => validateRequest("queryPronunciationKey", {}), /Unknown HTTP operation/);
  assert.equal(calls, 0);
});

test("unavailable metadata stays 503 until the caller explicitly repeats the same fixed read", async () => {
  const unknown = sample("unavailable-metadata");
  const success = sample("draft-representation");
  const calls = [];
  const client = createValidatedClient("http://deployment.invalid", 4096, async (request) => {
    calls.push({ method: request.method, url: request.url });
    const reply = calls.length === 1 ? unknown : success;
    return Response.json(reply.value.body, { status: reply.status, headers: reply.value.headers });
  });
  const first = await client.GET(path, { params });
  assert.equal(first.response.status, 503);
  assert.equal(first.error.error.code, "pronunciation_unavailable");
  assert.equal(first.error.error.retryable, true);
  assert.equal(Object.hasOwn(first.error.error, "nexus"), false);
  assert.equal(calls.length, 1);
  const second = await client.GET(path, { params });
  assert.equal(second.response.status, 200);
  assert.equal(second.data.revision, "9007199254740993");
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[0].method, "GET");
});

test("a status/body mismatch is rejected without another request", async () => {
  const unavailable = sample("unavailable-metadata").value.body;
  let calls = 0;
  const client = createValidatedClient("http://deployment.invalid", 4096, async () => {
    ++calls;
    return Response.json(unavailable, {
      status: 404,
      headers: { "Cache-Control": "no-store", "X-Should-Retry": "false" },
    });
  });
  await assert.rejects(client.GET(path, { params }), ContractValidationError);
  assert.equal(calls, 1);
});
