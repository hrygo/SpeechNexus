import type { components, operations } from "../src/speech.generated";

declare const error: components["schemas"]["ErrorResponse"];
const transcription: operations["createTranscription"]["responses"][404]["content"]["application/json"] =
  error;
const cancellation: operations["cancelJob"]["responses"][400]["content"]["application/json"] =
  error;
const artifactUnavailable: operations["getArtifact"]["responses"][503]["content"]["application/json"] =
  error;
const range: operations["downloadArtifactContent"]["responses"][416]["content"]["application/json"] =
  error;
const wrongMethod: operations["listModels"]["responses"][405] = {
  headers: { Allow: "GET" },
  content: { "application/json": error },
};
// @ts-expect-error A framework 405 must retain its Allow header.
const missingAllow: operations["listModels"]["responses"][405]["headers"] = {};
// @ts-expect-error A conditional artifact 304 has no JSON body.
const notModified: operations["getArtifact"]["responses"][304]["content"] = error;
void [
  transcription,
  cancellation,
  artifactUnavailable,
  range,
  wrongMethod,
  missingAllow,
  notModified,
];
