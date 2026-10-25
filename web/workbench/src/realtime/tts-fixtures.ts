import { parse } from "../../../../packages/contracts/src/realtime";
import { event, session } from "./realtime-fixtures";

/** Synthetic inputs only; no deployment defaults. */
export function ttsRequest() {
  return parse("RealtimeSessionCreateRequest", {
    profile: { id: "profile-tts", revision: "1" },
    modalities: ["tts"],
    models: { tts: { id: "tts-demo", revision: "1" } },
    voice: { id: "voice-demo", revision: "1" },
    audio: { output: { encoding: "pcm_s16le", sample_rate_hz: 24000, channels: 1 } },
  });
}
export function ttsSession() {
  return parse("RealtimeSession", {
    ...session(),
    effective_configuration: {
      ...session().effective_configuration,
      profile: ttsRequest().profile,
      models: ttsRequest().models,
      voice: ttsRequest().voice,
    },
  });
}
export function ttsReady(): Record<string, unknown> {
  return event("nexus.session.ready", {
    effective_configuration: {
      config_revision: "1",
      ...ttsSession().effective_configuration,
      parameters: {
        tts: {
          language: "zh",
          text_commit_mode: "explicit",
          audio_delivery: "chunked",
          speed: 1,
          planner: { max_chars: 400, max_wait_ms: 3000 },
        },
      },
    },
    limits: { ...ttsSession().limits, output_credit_samples: 24000 },
    input_credit_samples: 0,
  });
}
export function ttsEvent(type: string, fields: Record<string, unknown> = {}, seq = 2) {
  return event(
    type,
    {
      ...(type.startsWith("nexus.tts.") ? { utterance_id: "utterance-1" } : {}),
      operation_id: "tts-op-1",
      ...fields,
    },
    seq,
  );
}
export function ttsStarted(credit = 100) {
  return ttsEvent("nexus.tts.started", { audio: ttsRequest().audio!.output, text_credit: credit });
}
export function ttsCommand(type: string, fields: Record<string, unknown> = {}) {
  return {
    type: `nexus.tts.${type}`,
    event_id: crypto.randomUUID(),
    utterance_id: "utterance-1",
    ...fields,
  };
}
export function ttsPcm(samples = [0, 16384, -32768]) {
  const bytes = new Uint8Array(samples.length * 2),
    view = new DataView(bytes.buffer);
  samples.forEach((value, i) => view.setInt16(i * 2, value, true));
  return btoa(String.fromCharCode(...bytes));
}
