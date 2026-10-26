import { createValidatedClient, validateRequest } from "../src/voice_catalogue-http";

const client = createValidatedClient("https://deployment.invalid", 32768);
void client.GET("/v1/nexus/voices");
void client.GET("/v1/nexus/voices/{voice_id}/revisions/{revision}", {
  params: { path: { voice_id: "voice-demo-1", revision: "2" } },
});
// @ts-expect-error The read-only surface grants no retirement operation.
void client.POST("/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements", {});
// @ts-expect-error Publication is outside the selected operation identities.
validateRequest("publishVoiceDraft", {});
