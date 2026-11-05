import assert from "node:assert/strict";
import test from "node:test";
import { createHttpValidator } from "../src/http-runtime.mjs";
import schema from "../src/speechnexus_contracts/profile_public-http.schema.json" with { type: "json" };
const validator = createHttpValidator(schema);
const summary = {
  id: "synthetic-profile",
  revision: "9999999999999999999",
  current_revision: null,
  display_name: "Synthetic",
  kind: "asr",
  release_tier: "experimental",
  execution_classes: ["batch"],
};
const list = (data) => ({
  body: { object: "nexus.list", data, has_more: false, next_cursor: null },
});

test("published list validates nullable current revision and rejects private/draft fields", () => {
  for (const pointer of [null, "1", "9999999999999999999"]) {
    const value = list([{ ...summary, current_revision: pointer }]);
    assert.equal(validator.validateResponse("listProfiles", 200, value), value);
  }
  for (const change of [
    { current_revision: "latest" },
    { current_revision: 1 },
    { key_digest: "private" },
    { publication_status: "draft" },
  ]) {
    assert.throws(() =>
      validator.validateResponse("listProfiles", 200, list([{ ...summary, ...change }])),
    );
  }
  assert.equal(validator.validateResponse("listProfiles", 200, list([])).body.data.length, 0);
});

test("public requests validate bounds, unknown fields and full decimal fixed identity", () => {
  for (const limit of ["1", "50", "100"]) {
    validator.validateRequest("listProfiles", { query: { limit } });
  }
  for (const query of [{ limit: "0" }, { limit: "101" }, { limit: "bad" }, { unknown: "yes" }]) {
    assert.throws(() => validator.validateRequest("listProfiles", { query }));
  }
  validator.validateRequest("getProfileRevision", {
    path: { profile_id: summary.id, revision: summary.revision },
  });
  assert.throws(() =>
    validator.validateRequest("getProfileRevision", {
      path: { profile_id: summary.id, revision: "current" },
    }),
  );
});

test("generated public client validates the response with one explicit transport request", async () => {
  const { createValidatedClient } = await import("../src/profile_public-http.ts");
  const calls = [];
  const client = createValidatedClient("http://profile.synthetic", 262144, async (request) => {
    calls.push(request.url);
    return new Response(JSON.stringify(list([summary]).body), {
      headers: { "Content-Type": "application/json" },
    });
  });
  const result = await client.GET("/v1/nexus/profiles", { params: { query: { limit: 1 } } });
  assert.equal(result.data.data[0].current_revision, null);
  assert.deepEqual(calls, ["http://profile.synthetic/v1/nexus/profiles?limit=1"]);
});
