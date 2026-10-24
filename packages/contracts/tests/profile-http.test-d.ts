import { createValidatedClient, validateRequest } from "../src/profile-http";
import type { components } from "../src/admin.generated";

const client = createValidatedClient("http://deployment.invalid", 262144);
declare const request: components["schemas"]["ProfileRevisionCreateRequest"];
declare const created: components["schemas"]["ProfileDraftRevision"];
const draftStatus: "draft" = created.publication.status;
const noValidation: undefined = created.validation;
declare const unknown: components["schemas"]["ProfileCreationUnknownError"];
const noRetry: false = unknown.error.retryable;
const noOperation: undefined = unknown.error.nexus;
void [draftStatus, noValidation, noRetry, noOperation];
void client.POST("/v1/nexus/admin/profiles", { body: request });
void client.GET("/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}", {
  params: { path: { profile_id: "profile-demo", revision: "9999999999999999999" } },
});
// @ts-expect-error unselected GET on a path that also has a reviewed POST
void client.GET("/v1/nexus/admin/profiles");
// @ts-expect-error publication is outside this contract-only surface
void validateRequest("publishProfileRevision", {});
// @ts-expect-error there is no Operation/query-by-key recovery resource
void client.GET("/v1/nexus/operations/{operation_id}", {});
