import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import test from "node:test";
import * as admin from "../src/admin.ts";
import * as workbench from "../src/workbench.ts";
import * as execution from "../src/execution.ts";
import * as runtime from "../src/runtime.ts";
import * as realtime from "../src/realtime.ts";
import * as realtime_events from "../src/realtime_events.ts";
import * as model_bindings from "../src/model_bindings.ts";
import * as scenario_profiles from "../src/scenario_profiles.ts";
import * as gateway_recovery from "../src/gateway_recovery.ts";
import { exampleValue } from "./contract-examples.mjs";

const profiles = {
  admin,
  workbench,
  execution,
  runtime,
  realtime,
  realtime_events,
  scenario_profiles,
  model_bindings,
  gateway_recovery,
};
const fixtures = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/consumer-profiles.json", import.meta.url),
  ),
);
test("profile draft consumer preserves original intent and idempotency header", async () => {
  const example = (schema) =>
    structuredClone(fixtures.examples.find((e) => e.schema === schema && e.valid).value);
  const request = admin.parse(
    "ProfileRevisionCreateRequest",
    example("ProfileRevisionCreateRequest"),
  );
  const reply = example("ScenarioProfileRevision");
  let calls = 0;
  const client = admin.createClient("http://deployment.invalid", async (wire) => {
    calls++;
    assert.equal(wire.headers.get("Idempotency-Key"), "original-profile-key");
    assert.deepEqual(await wire.json(), request);
    return Response.json(reply, { status: 201 });
  });
  const response = await client.POST("/v1/nexus/admin/profiles", {
    params: { header: { "Idempotency-Key": "original-profile-key" } },
    body: request,
  });
  assert.equal(calls, 1);
  const draft = scenario_profiles.parse("ScenarioProfileRevision", response.data);
  assert.equal(draft.publication.status, "draft");
  assert.equal(draft.profile_id, request.profile_id);
});

for (const example of fixtures.examples) {
  test(example.id, () => {
    const value = exampleValue(example);
    const digest = () => createHash("sha256").update(JSON.stringify(value)).digest("hex");
    const before = digest();
    const { parse } = profiles[example.profile];
    if (example.valid) {
      assert.equal(parse(example.schema, value) === value, true);
    } else {
      assert.throws(() => parse(example.schema, value), {
        name: "ContractValidationError",
        message: `Invalid ${example.schema}`,
      });
      assert.throws(
        () => parse(example.schema, value),
        profiles[example.profile].ContractValidationError,
      );
    }
    assert.equal(digest(), before);
  });
}
test("profiles reject undeclared entrypoints and internal names in workbench", () => {
  for (const { parse } of Object.values(profiles)) {
    assert.throws(() => parse("NotAContract", {}), /Unknown profile contract/);
  }
  assert.throws(() => workbench.parse("SubmitExecutionRequest", {}), /Unknown profile contract/);
});

for (const [profile, schema, path, resolvedPath, params] of [
  ["admin", "WorkspaceCreateRequest", "/v1/nexus/workspaces", "/v1/nexus/workspaces", {}],
  ["workbench", "ProjectCreateRequest", "/v1/workbench/projects", "/v1/workbench/projects", {}],
  [
    "execution",
    "SubmitExecutionRequest",
    "/internal/v1/executions/submit",
    "/internal/v1/executions/submit",
    {},
  ],
  [
    "runtime",
    "RuntimeInstanceIntentRequest",
    "/internal/v1/nodes/{node_id}/instance-intents",
    "/internal/v1/nodes/node-demo/instance-intents",
    { path: { node_id: "node-demo" } },
  ],
]) {
  test(`${profile} client preserves the supplied request and never retries an unknown outcome`, async () => {
    const body = fixtures.examples.find((e) => e.schema === schema && e.valid).value;
    let requests = 0;
    const client = profiles[profile].createClient("http://deployment.invalid", async (request) => {
      requests++;
      assert.equal(request.url, `http://deployment.invalid${resolvedPath}`);
      assert.equal(request.method, "POST");
      assert.deepEqual(await request.json(), body);
      return Response.json({ error: { code: "outcome_unknown" } }, { status: 503 });
    });
    const response = await client.POST(path, { params, body });
    assert.equal(response.response.status, 503);
    assert.equal(requests, 1);
  });
}

test("workbench transcript consumer explicitly parses both transport bodies", async () => {
  const sample = (name) =>
    structuredClone(
      fixtures.examples.find((e) => e.profile === "workbench" && e.schema === name && e.valid)
        .value,
    );
  const request = workbench.parse("TranscriptCreateRequest", sample("TranscriptCreateRequest"));
  const reply = sample("TranscriptView");
  let requests = 0;
  const client = workbench.createClient("http://deployment.invalid", async (wire) => {
    requests++;
    assert.equal(
      wire.url,
      "http://deployment.invalid/v1/workbench/projects/project-demo/transcripts",
    );
    assert.equal(wire.headers.get("Idempotency-Key"), "synthetic-transcript-key");
    assert.deepEqual(await wire.json(), request);
    return Response.json(reply, { status: 201 });
  });
  const result = await client.POST("/v1/workbench/projects/{project_id}/transcripts", {
    params: {
      path: { project_id: "project-demo" },
      header: { "Idempotency-Key": "synthetic-transcript-key" },
    },
    body: request,
  });
  assert.deepEqual(workbench.parse("TranscriptView", result.data), reply);
  assert.equal(requests, 1);
  assert.throws(
    () =>
      workbench.parse("TranscriptView", { ...reply, revision: { ...reply.revision, revision: 1 } }),
    workbench.ContractValidationError,
  );
});
