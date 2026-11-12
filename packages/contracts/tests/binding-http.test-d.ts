import { createValidatedClient, validateRequest } from "../src/binding-http";
import type { components } from "../src/admin.generated";

const client = createValidatedClient("http://deployment.invalid", 262144);
declare const request: components["schemas"]["ModelBindingCreateRequest"];
declare const created: components["schemas"]["ModelBindingDraftRevision"];
const draftStatus: "draft" = created.publication.status;
declare const unknown: components["schemas"]["BindingCreationUnknownError"];
const noRetry: false = unknown.error.retryable;
const noOperation: undefined = unknown.error.nexus;
void [draftStatus, noRetry, noOperation];
void client.POST("/v1/nexus/admin/model-bindings", { body: request });
void client.GET("/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}", {
  params: { path: { binding_id: "profile-demo", revision: "9999999999999999999" } },
});
// @ts-expect-error unselected GET on a path that also has a reviewed POST
void client.GET("/v1/nexus/admin/model-bindings");
declare const publication: components["schemas"]["ModelBindingPublicationRequest"];
void client.POST("/v1/nexus/admin/model-bindings/{binding_id}/publications", {
  params: { path: { binding_id: "asr-quality" } },
  body: publication,
});
// @ts-expect-error the publication path is a reviewed POST with no GET
void client.GET("/v1/nexus/admin/model-bindings/{binding_id}/publications", {
  params: { path: { binding_id: "asr-quality" } },
});
void validateRequest("publishModelBindingRevision", {
  path: { binding_id: "asr-quality" },
  headers: { "Idempotency-Key": "publish-asr-quality-2-20261113" },
  body: publication,
});
// @ts-expect-error there is no Operation/query-by-key recovery resource
void client.GET("/v1/nexus/operations/{operation_id}", {});
