import { createValidatedClient, validateRequest } from "../src/admin-http";
import { parse } from "../src/admin";

const client = createValidatedClient("https://deployment.invalid", 8192);
void client.GET("/v1/nexus/workspaces");
void client.GET("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
});
// The selected surface carries the two accepted writes as well as the reads.
void client.POST("/v1/nexus/workspaces", { body: { display_name: "Synthetic" } });
void client.PATCH("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
  body: { expected_metadata_revision: 1, display_name: "Synthetic" },
});
// @ts-expect-error A create declares a required body.
void client.POST("/v1/nexus/workspaces");
// @ts-expect-error display_name is required inside the create body.
void client.POST("/v1/nexus/workspaces", { body: { policy_id: "policy-quota-r1" } });
// @ts-expect-error POST is selected on the collection, not on one workspace.
void client.POST("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
  body: { display_name: "Synthetic" },
});
void client.PATCH("/v1/nexus/workspaces/{workspace_id}", {
  params: { path: { workspace_id: "workspace-default" } },
  // @ts-expect-error expected_metadata_revision is required inside the patch body.
  body: { display_name: "Synthetic" },
});
// @ts-expect-error DELETE is outside the selected workspace surface.
void client.DELETE("/v1/nexus/workspaces");
// @ts-expect-error Only selected operation identities are exposed.
validateRequest("publishProfileRevision", {});
validateRequest("createWorkspace", { body: { display_name: "Synthetic" } });
declare const wire: unknown;
const policyRevision: string = parse("Workspace", wire).policy_revision;
const metadataRevision: number = parse("Workspace", wire).metadata_revision;
void [policyRevision, metadataRevision];
