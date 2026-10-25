import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse as parseWorkbench } from "../src/workbench.ts";
import {
  createValidatedClient,
  validateResponse,
  ContractValidationError,
} from "../src/voice_catalogue-http.ts";

const examples = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/voice-catalogue-http.json", import.meta.url),
  ),
).examples;
for (const example of examples) {
  test(`voice catalogue HTTP: ${example.id}`, () => {
    const value = structuredClone(example.value);
    const check = () => validateResponse(example.operation, example.status, value);
    if (example.valid) assert.equal(check(), value);
    else assert.throws(check, ContractValidationError);
    assert.deepEqual(value, example.value);
  });
}

test("the workbench v2 projection keeps all 2000 characters while v1 remains frozen", () => {
  const publicRevision = examples.find((example) => example.id === "disabled-reason-2000").value
    .body;
  const { resource_type, object, source, capabilities, validation, ...projection } = publicRevision;
  Object.assign(projection, {
    languages: capabilities.languages,
    audio_formats: capabilities.audio_formats,
  });
  assert.equal(parseWorkbench("VoiceRevisionV2", projection), projection);
  assert.throws(() => parseWorkbench("VoiceRevision", projection), ContractValidationError);
  assert.throws(
    () => parseWorkbench("VoiceRevisionV2", { ...projection, disabled_reason: "停".repeat(2001) }),
    ContractValidationError,
  );
});

test("catalogue client validates native reads once and refuses publication before fetching", async () => {
  const reply = examples.find((example) => example.id === "disabled-reason-2000").value.body;
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 32768, async (request) => {
    calls++;
    assert.equal(request.method, "GET");
    assert.equal(
      request.url,
      "https://deployment.invalid/v1/nexus/voices/voice-demo-1/revisions/2",
    );
    return Response.json(reply);
  });
  await assert.rejects(
    client.POST("/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements", {
      params: { path: { voice_id: "voice-demo-1", revision: "2" } },
      body: {},
    }),
    /Unknown HTTP operation/,
  );
  assert.equal(calls, 0);
  const result = await client.GET("/v1/nexus/voices/{voice_id}/revisions/{revision}", {
    params: { path: { voice_id: "voice-demo-1", revision: "2" } },
  });
  assert.deepEqual(result.data, reply);
  assert.equal(calls, 1);
});
