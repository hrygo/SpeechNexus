import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ContractValidationError,
  createValidatedClient,
  validateRequest,
  validateResponse,
} from "../src/voice_drafts-http.ts";

const fixture = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/voice-drafts-http.json", import.meta.url),
  ),
);

test("voice client consumes a created draft and reads its pinned revision", async () => {
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 65536, async (request) => {
    calls++;
    if (request.method === "POST") {
      assert.equal(request.headers.get("idempotency-key"), "synthetic-voice-key");
      assert.equal(request.url, "https://deployment.invalid/v1/nexus/voice-designs");
      return Response.json(fixture.accepted, { status: 202 });
    }
    assert.equal(request.method, "GET");
    assert.equal(request.url.split("/").at(-1), fixture.accepted.draft.id);
    return Response.json(fixture.view);
  });
  const created = await client.POST("/v1/nexus/voice-designs", {
    headers: { "Idempotency-Key": "synthetic-voice-key" },
    body: {
      model: "tts-quality",
      input: { description: "Synthetic warm voice", preview_text: "Synthetic preview" },
    },
  });
  assert.deepEqual(created.data, fixture.accepted);
  const read = await client.GET("/v1/nexus/voice-drafts/{draft_id}", {
    params: { path: { draft_id: created.data.draft.id } },
  });
  assert.deepEqual(read.data, fixture.view);
  assert.equal(calls, 2);
});

test("voice metadata and clone validation reject unsupported intent before fetching", () => {
  assert.throws(
    () =>
      validateRequest("updateVoiceDraftMetadata", {
        path: { draft_id: fixture.view.draft.id },
        body: { expected_metadata_revision: 1, display_name: "Synthetic", status: "published" },
      }),
    ContractValidationError,
  );
  assert.throws(
    () =>
      validateRequest("createVoiceClone", {
        body: {
          model: "tts-quality",
          input: {
            reference_audio_asset_id: "synthetic-reference",
            source_use_confirmed: false,
          },
        },
      }),
    ContractValidationError,
  );
  const invalid = structuredClone(fixture.accepted);
  invalid.draft.internal_model_path = "synthetic-private-path";
  assert.throws(
    () => validateResponse("createVoiceDesign", 202, { body: invalid }),
    ContractValidationError,
  );
});
