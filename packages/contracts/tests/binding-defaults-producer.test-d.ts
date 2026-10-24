import type { Contracts as Legacy } from "../src/admin";
import type { Contracts as Producer } from "../src/binding_defaults_producer";
import { createValidatedClient } from "../src/binding_defaults-http";

type Create = Producer["BindingDefaultsCreateRequest"];
declare const original: Omit<Create, "parameter_defaults">;
const legacy: Create = original;
const sparse: Create = {
  ...original,
  parameter_defaults: { asr: { max_segment_ms: 15000 } },
};
// @ts-expect-error the implemented legacy request still rejects the future input
const legacyFuture: Legacy["ModelBindingCreateRequest"] = { ...original, parameter_defaults: {} };
const empty: Create = { ...original, parameter_defaults: {} };
// @ts-expect-error null is not an omitted or explicitly empty default declaration
const invalid: Create = { ...original, parameter_defaults: null };
const client = createValidatedClient("http://deployment.invalid", 262144);
const response = await client.GET(
  "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults",
  { params: { path: { binding_id: "binding-synthetic", revision: "1" } } },
);
if (response.data) {
  const snapshot: string = response.data.snapshot_id;
  const parent: string = response.data.binding_revision_id;
  const revision: string = response.data.binding_revision;
  void [snapshot, parent, revision];
}
void [legacy, sparse, empty, invalid, legacyFuture];
