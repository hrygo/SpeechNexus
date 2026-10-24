import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { parse } from "../src/admin.ts";
import {
  createValidatedClient,
  validateRequest,
  validateResponse,
  ContractValidationError,
} from "../src/admin-http.ts";

const examples = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/admin-http.json", import.meta.url)),
).examples;
for (const example of examples) {
  test(`admin HTTP: ${example.id}`, () => {
    const value = structuredClone(example.value);
    const check = () =>
      example.kind === "request"
        ? validateRequest(example.operation, value)
        : validateResponse(example.operation, example.status, value);
    if (example.valid) {
      assert.equal(check(), value);
      if (example.kind === "response") {
        const name =
          example.status !== 200
            ? "ErrorResponse"
            : example.operation === "listWorkspaces"
              ? "WorkspaceList"
              : "Workspace";
        assert.equal(parse(name, value.body), value.body);
      }
    } else {
      assert.throws(check, ContractValidationError);
    }
    assert.deepEqual(value, example.value);
  });
}

test("workspace client keeps omitted defaults omitted and parses the original response", async () => {
  const reply = examples.find((example) => example.id === "list-response").value.body;
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 8192, async (request) => {
    calls++;
    assert.equal(request.url, "https://deployment.invalid/v1/nexus/workspaces");
    assert.equal(request.method, "GET");
    return Response.json(reply);
  });
  const result = await client.GET("/v1/nexus/workspaces");
  assert.deepEqual(result.data, reply);
  assert.equal(calls, 1);
});

test("invalid paths never fetch and a declared missing workspace never retries", async () => {
  const reply = examples.find((example) => example.id === "getWorkspace-error-404").value.body;
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 8192, async () => {
    calls++;
    return Response.json(reply, { status: 404 });
  });
  await assert.rejects(
    client.GET("/v1/nexus/workspaces/{workspace_id}", {
      params: { path: { workspace_id: "../private" } },
    }),
    ContractValidationError,
  );
  assert.equal(calls, 0);
  const result = await client.GET("/v1/nexus/workspaces/{workspace_id}", {
    params: { path: { workspace_id: "workspace-missing" } },
  });
  assert.deepEqual(result.error, reply);
  assert.equal(calls, 1);
});

test("the validated client rejects unselected writes at runtime before fetching", async () => {
  let calls = 0;
  const client = createValidatedClient("https://deployment.invalid", 8192, async () => {
    calls++;
    return Response.json({});
  });
  await assert.rejects(
    client.POST("/v1/nexus/workspaces", { body: { display_name: "Synthetic" } }),
    /Unknown HTTP operation/,
  );
  assert.equal(calls, 0);
});
