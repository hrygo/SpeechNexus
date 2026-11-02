import { parse } from "../src/index";
import type { components } from "../src/speech.generated";

declare const wire: unknown;
const job = parse("JobCreateRequest", wire);
if (job.kind === "tts" && job.model_binding) {
  const revision: string = job.model_binding.revision;
  const binding: string = job.model_binding.id;
  void [revision, binding];
  // @ts-expect-error Exact decimal revisions never become JavaScript numbers.
  const rounded: number = job.model_binding.revision;
  void rounded;
}

const selected: components["schemas"]["TtsJobCreate"] = {
  kind: "tts",
  model: "narrator-tts",
  model_binding: { id: "binding-narrator", revision: "9999999999999999999" },
  input: { text: "synthetic pin" },
  voice: { id: "voice-demo", revision: "2" },
};
void selected;
