import { webcrypto } from "node:crypto";
import { afterEach, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { parse, WorkbenchClient, WorkbenchResponseError } from "./workbench-client";

afterEach(() => vi.unstubAllGlobals());

it("downloads the saved copy through the generated stream on the fixed origin", async () => {
  vi.stubGlobal("crypto", webcrypto);
  const content = new Uint8Array([0, 255, 1, 2]);
  const digest =
    "sha256:" + Buffer.from(await webcrypto.subtle.digest("SHA-256", content)).toString("hex");
  const fixture = fixtures.examples.find(
    (e) => e.operation === "getSavedAsset" && e.kind === "response" && e.valid && e.status === 200,
  )!;
  const asset = parse("SavedAsset", { ...fixture.value.body, bytes: 4, sha256: digest });
  const transport = vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response(content, {
        headers: {
          "Content-Type": "application/octet-stream",
          "Content-Length": "4",
          ETag: `"${digest}"`,
        },
      }),
    ),
  );
  const blob = await new WorkbenchClient(transport).downloadSavedAsset(asset);
  expect(blob.size).toBe(4);
  expect(transport).toHaveBeenCalledOnce();
  const request = transport.mock.calls[0]![0] as Request;
  expect(new URL(request.url).origin).toBe(location.origin);
  expect(new URL(request.url).pathname).toBe(`/v1/workbench/saved-assets/${asset.id}/content`);
  expect(request.redirect).toBe("error");
  expect(request.cache).toBe("no-store");
});

it("keeps a declared 410 as an error and never creates an alternative request", async () => {
  const fixture = fixtures.examples.find(
    (e) => e.operation === "getBookExport" && e.kind === "response" && e.valid && e.status === 200,
  )!;
  const base = fixture.value.body as Record<string, unknown>;
  const job = parse("ExportJob", {
    ...base,
    status: "succeeded",
    progress_completed: base.progress_total,
    error: null,
    artifact: {
      object: "nexus.export_artifact",
      id: "artifact-download",
      export_id: base.id,
      manifest_revision: base.manifest_revision,
      media_type: "application/zip",
      bytes: 4,
      sha256: "sha256:" + "ab".repeat(32),
      created_at: base.created_at,
    },
    format: "zip",
  });
  const transport = vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          error: {
            code: "download_expired",
            type: "invalid_request_error",
            message: "Content expired.",
            param: null,
            request_id: "request-demo",
            retryable: false,
          },
        }),
        { status: 410, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );
  await expect(new WorkbenchClient(transport).downloadBookExport(job)).rejects.toBeInstanceOf(
    WorkbenchResponseError,
  );
  expect(transport).toHaveBeenCalledOnce();
});
