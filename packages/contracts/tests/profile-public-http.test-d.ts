import {
  createValidatedClient,
  validateRequest,
  validateResponse,
} from "../src/profile_public-http";
const client = createValidatedClient("http://localhost", 262144);
void client.GET("/v1/nexus/profiles", { params: { query: { limit: 1 } } });
validateRequest("listProfiles", { query: { limit: "1" } });
validateResponse("getProfileRevision", 200, {});
// @ts-expect-error mutation is not a public profile read operation
validateRequest("publishProfileRevision", {});
