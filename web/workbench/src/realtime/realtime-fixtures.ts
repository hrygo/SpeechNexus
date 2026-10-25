import traces from "../../../../tests/fixtures/contracts/transcription-sequences.json";
import { parse, type Contracts } from "../../../../packages/contracts/src/realtime";

/** Synthetic test inputs only; no production defaults or capability claims. */
export function request(): Contracts["RealtimeSessionCreateRequest"] {
  return parse("RealtimeSessionCreateRequest", {
    profile: { id: "profile-demo", revision: "1" },
    modalities: ["asr"],
    models: { asr: { id: "asr-quality", revision: "1" } },
    audio: { input: { encoding: "pcm_s16le", sample_rate_hz: 24000, channels: 1 } },
  });
}
export function session(): Contracts["RealtimeSession"] {
  return parse("RealtimeSession", {
    id: "session-demo",
    object: "nexus.realtime_session",
    status: "created",
    expires_at: "2099-01-01T00:00:00Z",
    connection_url: "/v1/realtime?session_id=session-demo",
    effective_configuration: {
      profile: request().profile,
      models: request().models,
      policy_revision: "1",
      catalog_revision: "1",
    },
    limits: {
      max_input_chunk_ms: 100,
      max_message_bytes: 65536,
      input_credit_samples: 24000,
      output_credit_samples: 0,
      max_segment_ms: 20000,
      max_session_duration_ms: 3600000,
    },
    cleanup_state: "not_required",
    terminal_operations: [],
    closed_at: null,
    error: null,
  });
}
export function storage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (k: string) => values.get(k) ?? null,
    setItem: (k: string, v: string) => {
      values.set(k, v);
    },
  };
}

export function created() {
  return structuredClone(traces.examples[0]!.steps[0]!.value);
}
export function event(type: string, fields: Record<string, unknown> = {}, seq = 1) {
  return {
    type,
    event_id: `event-${seq}`,
    session_id: "session-demo",
    seq,
    stream_epoch: 1,
    sent_at: "2026-10-24T00:00:00Z",
    ...fields,
  };
}
export function ready() {
  return event("nexus.session.ready", {
    effective_configuration: {
      config_revision: "1",
      profile: { id: "profile-demo", revision: "1" },
      models: { asr: { id: "asr-quality", revision: "1" } },
      policy_revision: "1",
      parameters: {
        asr: {
          language: "zh",
          preview_interval_ms: 500,
          max_segment_ms: 20000,
          finalization: "full_segment",
          final_deadline_ms: 5000,
          auxiliary_deadline_ms: 5000,
          rollback_tokens: 0,
          turn_detection: { mode: "manual", silence_duration_ms: 500 },
          alignment: "off",
          diarization: "off",
        },
      },
    },
    limits: {
      max_input_chunk_ms: 100,
      max_message_bytes: 65536,
      input_credit_samples: 24000,
      output_credit_samples: 0,
      max_segment_ms: 20000,
      max_session_duration_ms: 3600000,
    },
    input_credit_samples: 24000,
  });
}
