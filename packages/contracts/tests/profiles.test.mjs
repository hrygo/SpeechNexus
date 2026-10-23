import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as admin from "../src/admin.ts";
import * as workbench from "../src/workbench.ts";
import * as execution from "../src/execution.ts";
import * as runtime from "../src/runtime.ts";

const profiles = { admin, workbench, execution, runtime };
const fixtures = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/contracts/consumer-profiles.json", import.meta.url),
  ),
);
for (const example of fixtures.examples) {
  test(example.id, () => {
    const value = structuredClone(example.value);
    const before = structuredClone(value);
    const { parse } = profiles[example.profile];
    if (example.valid) {
      assert.equal(parse(example.schema, value), value);
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
    assert.deepEqual(value, before);
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
