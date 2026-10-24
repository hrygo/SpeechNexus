import { createValidatedClient, validateRequest } from "../src/admin-http";
import { parse } from "../src/admin";

const client = createValidatedClient("https://deployment.invalid", 8192);
void client.GET("/v1/nexus/workspaces");
void client.GET("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
});
// @ts-expect-error POST is outside the selected workspace read surface.
void client.POST("/v1/nexus/workspaces", { body: { display_name: "Synthetic" } });
// @ts-expect-error PATCH is outside the selected workspace read surface.
void client.PATCH("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
  body: { expected_metadata_revision: 1, display_name: "Synthetic" },
});
// @ts-expect-error Only selected operation identities are exposed.
validateRequest("createWorkspace", {});
declare const wire: unknown;
const policyRevision: string = parse("Workspace", wire).policy_revision;
const metadataRevision: number = parse("Workspace", wire).metadata_revision;
void [policyRevision, metadataRevision];
