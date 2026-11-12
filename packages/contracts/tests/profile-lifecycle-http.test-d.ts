import { createValidatedClient, validateRequest } from "../src/profile_lifecycle-http";
import { parse } from "../src/admin";
import type { components } from "../src/admin.generated";

declare const publication: components["schemas"]["ProfilePublicationRequest"];
declare const activation: components["schemas"]["ProfileActivationRequest"];
declare const retirement: components["schemas"]["ProfileRevisionRetirementRequest"];
declare const published: components["schemas"]["ProfilePublishedRevision"];
const state: "published" = published.publication.status;
const report: string = published.validation.validation_report_id;
const parsedPublication = parse("ProfilePublicationRequest", publication);
const parsedActivation = parse("ProfileActivationRequest", activation);
void validateRequest("retireProfileRevision", retirement);
const parsedPublished = parse("ProfilePublishedRevision", published);
void [state, report, parsedPublication, parsedActivation, parsedPublished];

const client = createValidatedClient("http://deployment.invalid", 262144);
const path = { profile_id: "profile-synthetic", revision: "9999999999999999999" };
void client.POST("/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/publications", {
  params: { path },
  body: publication,
});
void client.POST("/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/activations", {
  params: { path },
  body: activation,
});
void client.POST("/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}/retirements", {
  params: { path },
  body: retirement,
});
// @ts-expect-error the future lifecycle surface does not add a draft creation method
void client.POST("/v1/nexus/admin/profiles", { body: {} });
// @ts-expect-error there is no fabricated Operation/query-by-key resource
void client.GET("/v1/nexus/operations/{operation_id}", {});
// @ts-expect-error a binding retirement is not a profile lifecycle operation
void validateRequest("retireModelBindingRevision", {});
