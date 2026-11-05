import assert from "node:assert/strict";
import test from "node:test";
import { createHttpValidator } from "../src/http-runtime.mjs";
import schema from "../src/speechnexus_contracts/profile_public-http.schema.json" with { type: "json" };
const validator = createHttpValidator(schema);
const summary = {
  id: "synthetic-profile",
  revision: "9999999999999999999",
  display_name: "Synthetic",
  kind: "asr",
  release_tier: "experimental",
  execution_classes: ["batch"],
};
const list = (data) => ({
  body: { object: "nexus.list", data, has_more: false, next_cursor: null },
});

test("v1 list keeps the original closed summary without the family pointer", () => {
  const value = list([summary]);
  assert.equal(validator.validateResponse("listProfiles", 200, value), value);
  assert.throws(() =>
    validator.validateResponse("listProfiles", 200, list([{ ...summary, current_revision: null }])),
  );
});

test("v2 list validates nullable current revision and rejects private/draft fields", () => {
  for (const pointer of [null, "1", "9999999999999999999"]) {
    const value = list([{ ...summary, current_revision: pointer }]);
    assert.equal(validator.validateResponse("listPublishedProfiles", 200, value), value);
  }
  for (const change of [
    { current_revision: "latest" },
    { current_revision: 1 },
    { key_digest: "private" },
    { publication_status: "draft" },
  ]) {
    assert.throws(() =>
      validator.validateResponse("listPublishedProfiles", 200, list([{ ...summary, ...change }])),
    );
  }
  assert.throws(() => validator.validateResponse("listPublishedProfiles", 200, list([summary])));
  assert.equal(
    validator.validateResponse("listPublishedProfiles", 200, list([])).body.data.length,
    0,
  );
});

test("public requests validate bounds, unknown fields and full decimal fixed identity", () => {
  for (const operation of ["listProfiles", "listPublishedProfiles"]) {
    for (const limit of ["1", "50", "100"]) {
      validator.validateRequest(operation, { query: { limit } });
    }
    for (const query of [{ limit: "0" }, { limit: "101" }, { limit: "bad" }, { unknown: "yes" }]) {
      assert.throws(() => validator.validateRequest(operation, { query }));
    }
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
    return new Response(JSON.stringify(list([{ ...summary, current_revision: null }]).body), {
      headers: { "Content-Type": "application/json" },
    });
  });
  const result = await client.GET("/v2/nexus/profiles", { params: { query: { limit: 1 } } });
  assert.equal(result.data.data[0].current_revision, null);
  assert.deepEqual(calls, ["http://profile.synthetic/v2/nexus/profiles?limit=1"]);
});
