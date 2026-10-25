import type { operations } from "../src/speech.generated";

type Unavailable = operations["getPronunciationRevision"]["responses"][503];
type ReadHeaders = Unavailable["headers"];
declare const body: Unavailable["content"]["application/json"];

// Retry-After is optional in the source, unlike the two required policy headers.
const omitted: ReadHeaders = { "Cache-Control": "no-store", "X-Should-Retry": "true" };
const present: ReadHeaders = { ...omitted, "Retry-After": 1 };
const response: Unavailable = {
  headers: omitted,
  content: { "application/json": body },
};
// @ts-expect-error The schema-typed header is numeric; transport decoding is separate.
const invalidRetry: ReadHeaders = { ...omitted, "Retry-After": "soon" };
// @ts-expect-error Optional Retry-After does not make cache policy optional.
const invalidCache: ReadHeaders = { "X-Should-Retry": "true" };
// @ts-expect-error Unavailable and not-found retry policies remain distinct.
const invalidPolicy: ReadHeaders = { "Cache-Control": "no-store", "X-Should-Retry": "false" };
void [present, response, invalidRetry, invalidCache, invalidPolicy];
