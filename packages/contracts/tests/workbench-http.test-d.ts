import { createValidatedClient } from "../src/workbench-http";
import { parse, type Contracts } from "../src/workbench";

const client = createValidatedClient("http://deployment.invalid", 262144);
declare const request: Contracts["RenderUnitGenerationRequest"];
declare const accepted: Contracts["RenderUnitGenerationAccepted"];
const publicJobId: string = accepted.speech_job_id;
void publicJobId;
void parse("RenderUnitGenerationRequest", request);
void parse("RenderUnitGenerationAccepted", accepted);
void client.POST("/v1/workbench/render-units/{unit_id}/generations", {
  params: { path: { unit_id: "unit-demo" }, header: { "Idempotency-Key": "original-render-key" } },
  body: { expected_unit_revision: "9999999999999999999" },
});
// @ts-expect-error source Revision is a string, never a JavaScript number
const numericRevision: Contracts["RenderUnitGenerationRequest"] = { expected_unit_revision: 7 };
void numericRevision;
// @ts-expect-error source operation has no GET method
void client.GET("/v1/workbench/render-units/{unit_id}/generations", {});
const download = client.GET("/v1/workbench/saved-assets/{asset_id}/content", {
  params: { path: { asset_id: "asset-demo" }, header: { Range: "bytes=0-3" } },
  parseAs: "stream",
});
void download.then((result) => {
  const stream: ReadableStream<Uint8Array> | null | undefined = result.data;
  void stream;
});
// @ts-expect-error binary download declares GET only
void client.POST("/v1/workbench/saved-assets/{asset_id}/content", {});
// @ts-expect-error book download remains outside the selected runtime surface
void client.GET("/v1/workbench/exports/{export_id}/content", {});
