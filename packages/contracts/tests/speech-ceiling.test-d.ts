import { createSpeechClient, parse } from "../src/index";
import type { components } from "../src/speech.generated";

const request: components["schemas"]["SpeechRequest"] = {
  model: "tts-quality",
  voice: "voice-demo",
  input: "Synthetic speech.",
  nexus: { max_output_audio_ms: 100 },
};
const validated = parse("SpeechRequest", request);
const ceiling: number | undefined = validated.nexus?.max_output_audio_ms;
const client = createSpeechClient("https://deployment.invalid");
void client.POST("/v1/audio/speech", { body: request });
void client.POST("/v1/audio/speech", {
  body: { model: "tts-quality", voice: "voice-demo", input: "Synthetic speech." },
});
// @ts-expect-error The ceiling is numeric; string coercion is forbidden.
request.nexus = { max_output_audio_ms: "100" };
// @ts-expect-error Extensions belong inside nexus.
request.max_output_audio_ms = 100;
void ceiling;
