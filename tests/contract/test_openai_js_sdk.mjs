import assert from "node:assert/strict";
import test from "node:test";

import OpenAI, { RateLimitError } from "openai";

const ERROR_BODY = {
  error: {
    code: "quota_exceeded",
    type: "rate_limit_error",
    message: "quota exceeded",
    param: null,
    request_id: "req_mock",
    retryable: true,
    retry_after_ms: 1000,
  },
};

test("OpenAI JavaScript SDK requests and responses match the declared subset", async () => {
  const calls = [];
  const fetchMock = async (input, init = {}) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    // OpenAI's Node file conversion uses a data: fetch to inspect Blob input;
    // it is not a server request and must not be counted as SDK parity traffic.
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return new Response("");
    }
    const call = {
      path: url.pathname,
      method: request.method,
      authorization: request.headers.get("authorization"),
      fields: null,
      body: null,
    };

    if (url.pathname === "/v1/audio/transcriptions") {
      const form = await request.clone().formData();
      call.fields = [...form.entries()].map(([name, value]) => [
        name,
        typeof value === "string"
          ? value
          : { filename: value.name, mediaType: value.type, size: value.size },
      ]);
    } else if (request.method !== "GET") {
      call.body = JSON.parse(await request.clone().text());
    }
    calls.push(call);

    if (url.pathname === "/v1/models") {
      return Response.json({
        object: "list",
        data: [
          {
            id: "asr-quality",
            object: "model",
            created: 1700000000,
            owned_by: "speechnexus",
          },
        ],
      });
    }
    if (url.pathname === "/v1/models/asr-quality") {
      return Response.json({
        id: "asr-quality",
        object: "model",
        created: 1700000000,
        owned_by: "speechnexus",
      });
    }
    if (url.pathname === "/v1/audio/transcriptions") {
      return Response.json({ text: "hello" });
    }
    if (url.pathname === "/v1/audio/speech") {
      if (call.body.input === "fail") {
        return Response.json(ERROR_BODY, {
          status: 429,
          headers: { "retry-after": "1" },
        });
      }
      return new Response(new Uint8Array([1, 2, 3]), {
        headers: { "content-type": "audio/wav" },
      });
    }
    throw new Error(`unexpected JavaScript SDK path: ${url.pathname}`);
  };

  const client = new OpenAI({
    baseURL: "http://testserver/v1",
    apiKey: "test-key",
    maxRetries: 0,
    fetch: fetchMock,
  });

  const models = await client.models.list();
  assert.equal(models.data[0].id, "asr-quality");
  assert.equal((await client.models.retrieve("asr-quality")).id, "asr-quality");

  const transcription = await client.audio.transcriptions.create({
    file: new Blob(["RIFF"], { type: "audio/wav" }),
    model: "asr-quality",
    response_format: "json",
    timestamp_granularities: ["segment", "word"],
  });
  assert.equal(transcription.text, "hello");

  const speech = await client.audio.speech.create({
    model: "tts-interactive",
    voice: "narrator",
    input: "hello",
    response_format: "wav",
  });
  assert.deepEqual(new Uint8Array(await speech.arrayBuffer()), new Uint8Array([1, 2, 3]));

  await assert.rejects(
    client.audio.speech.create({
      model: "tts-interactive",
      voice: "narrator",
      input: "fail",
      response_format: "wav",
    }),
    (error) => error instanceof RateLimitError && error.status === 429,
  );

  assert.deepEqual(
    calls.map((call) => `${call.method} ${call.path}`),
    [
      "GET /v1/models",
      "GET /v1/models/asr-quality",
      "POST /v1/audio/transcriptions",
      "POST /v1/audio/speech",
      "POST /v1/audio/speech",
    ],
  );
  assert.ok(calls.every((call) => call.authorization === "Bearer test-key"));

  const transcriptionCall = calls.find(
    (call) => call.path === "/v1/audio/transcriptions",
  );
  assert.ok(transcriptionCall?.fields);
  assert.deepEqual(
    transcriptionCall.fields.map(([name]) => name).sort(),
    [
      "file",
      "model",
      "response_format",
      "timestamp_granularities[]",
      "timestamp_granularities[]",
    ].sort(),
  );
  assert.deepEqual(
    transcriptionCall.fields.find(([name]) => name === "model"),
    ["model", "asr-quality"],
  );

  const speechCall = calls.find(
    (call) => call.path === "/v1/audio/speech" && call.body?.input === "hello",
  );
  assert.deepEqual(speechCall?.body, {
    model: "tts-interactive",
    voice: "narrator",
    input: "hello",
    response_format: "wav",
  });
});
