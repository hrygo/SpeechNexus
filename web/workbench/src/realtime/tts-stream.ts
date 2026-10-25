import { parse, type Contracts } from "../../../../packages/contracts/src/realtime_events";
import type { RealtimeSession } from "./realtime-client";
import { freeze } from "./asr-stream";

type Event = Contracts["RealtimeEvent"];
type Ready = Extract<Event, { type: "nexus.session.ready" }>;
type Done = Extract<Event, { type: "nexus.operation.done" }>;
export type TtsUtterance = {
  id: string;
  operationId: string | null;
  phase: "starting" | "accepting" | "finishing" | "cancelling" | "terminal";
  sent: number;
  accepted: number;
  committed: number;
  text: string;
  credit: number;
  received: number;
  chunk: number;
  done: Done | null;
};
export type TtsSnapshot = {
  phase: "awaiting" | "active" | "draining" | "closed" | "disconnected";
  epoch: number | null;
  seq: number;
  configuration: Ready["effective_configuration"] | null;
  limits: Ready["limits"] | null;
  utterance: TtsUtterance | null;
  gap: boolean;
};
export type TtsAudio = {
  utteranceId: string;
  operationId: string;
  startSample: number;
  samples: Float32Array;
};
export function ttsCheck(value: boolean): asserts value {
  if (!value) throw Error("TTS protocol boundary");
}
const bytes = (value: string) => new TextEncoder().encode(value).byteLength;
const safe = (n: number) => ttsCheck(Number.isSafeInteger(n) && n >= 0);
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
const same = (a: unknown, b: unknown) => canonical(a) === canonical(b);
function ref(
  a: { id: string; revision: string } | null | undefined,
  b: { id: string; revision: string } | null | undefined,
) {
  return a?.id === b?.id && a?.revision === b?.revision;
}
async function hash(value: string) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))]
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
}
const TYPES = new Set([
  "nexus.tts.start",
  "nexus.tts.append_text",
  "nexus.tts.commit_text",
  "nexus.tts.finish_text",
  "nexus.tts.cancel",
]);

/** Public consumer only. Audio exists only in the transient receive return value. */
export class TtsStream {
  #s: TtsSnapshot = freeze({
    phase: "awaiting",
    epoch: null,
    seq: -1,
    configuration: null,
    limits: null,
    utterance: null,
    gap: false,
  });
  #events = new Map<string, string>();
  #commands = new Map<string, string>();
  #commandBytes = 0;
  #utterances = new Set<string>();
  #operations = new Set<string>();
  constructor(private readonly expected: RealtimeSession) {}
  getSnapshot = () => this.#s;
  disconnect() {
    this.#s = freeze({ ...this.#s, phase: "disconnected", gap: true });
  }
  drain() {
    if (this.#s.phase === "active") this.#s = freeze({ ...this.#s, phase: "draining" });
  }
  /** Validate first, dispatch synchronously, then publish. A failed send changes no command facts. */
  command(input: unknown, send: (wire: string) => void = () => {}) {
    const wire = JSON.stringify(input);
    ttsCheck(bytes(wire) <= Math.min(65536, this.#s.limits?.max_message_bytes ?? 65536));
    const e = parse("RealtimeEvent", input);
    ttsCheck(typeof e.event_id === "string");
    ttsCheck(TYPES.has(e.type) && "utterance_id" in e && typeof e.utterance_id === "string");
    const old = this.#commands.get(e.event_id);
    if (old !== undefined) {
      ttsCheck(old === wire);
      return;
    }
    ttsCheck(this.#commands.size < 30000 && this.#commandBytes + bytes(wire) <= 4 * 1024 * 1024);
    ttsCheck(this.#s.phase === "active" || this.#s.phase === "draining");
    const next = structuredClone(this.#s);
    const u = next.utterance;
    if (e.type === "nexus.tts.start") {
      ttsCheck(next.phase === "active");
      ttsCheck(
        !u ||
          (u.phase === "terminal" &&
            !!u.done &&
            ["not_required", "complete"].includes(u.done.cleanup_state)),
      );
      ttsCheck(this.#utterances.size < 10000 && !this.#utterances.has(e.utterance_id));
      const cfg = next.configuration!;
      ttsCheck(!e.voice || ref(e.voice, cfg.voice));
      ttsCheck(!e.text_commit_mode || e.text_commit_mode === cfg.parameters.tts?.text_commit_mode);
      ttsCheck(!e.audio_delivery || e.audio_delivery === cfg.parameters.tts?.audio_delivery);
      next.utterance = {
        id: e.utterance_id,
        operationId: null,
        phase: "starting",
        sent: 0,
        accepted: 0,
        committed: 0,
        text: "",
        credit: 0,
        received: 0,
        chunk: 0,
        done: null,
      };
    } else {
      ttsCheck(
        !!u && u.id === e.utterance_id && u.phase !== "terminal" && u.phase !== "cancelling",
      );
      if (e.type === "nexus.tts.cancel") u.phase = "cancelling";
      else {
        ttsCheck(!!u.operationId && u.phase === "accepting");
        if (e.type === "nexus.tts.append_text") {
          ttsCheck(next.phase === "active");
          ttsCheck(u.sent === u.accepted && e.text_seq === u.sent + 1 && bytes(e.text) <= u.credit);
          ttsCheck(bytes(u.text) + bytes(e.text) <= 1024 * 1024);
          u.sent = e.text_seq;
          u.text += e.text;
          u.credit -= bytes(e.text);
        } else if (e.type === "nexus.tts.commit_text") {
          ttsCheck(e.through_text_seq <= u.accepted && e.through_text_seq > u.committed);
          u.committed = e.through_text_seq;
        } else if (e.type === "nexus.tts.finish_text") {
          ttsCheck(u.sent === u.accepted && u.sent > 0);
          u.committed = u.sent;
          u.phase = "finishing";
        } else ttsCheck(false);
      }
    }
    send(wire);
    this.#commands.set(e.event_id, wire);
    this.#commandBytes += bytes(wire);
    if (e.type === "nexus.tts.start") this.#utterances.add(e.utterance_id);
    this.#s = freeze(next);
  }
  async receive(input: unknown): Promise<TtsAudio | null> {
    ttsCheck(
      bytes(JSON.stringify(input)) <= Math.min(65536, this.#s.limits?.max_message_bytes ?? 65536),
    );
    const e = parse("RealtimeEvent", input);
    ttsCheck(!TYPES.has(e.type));
    if (e.type === "session.created") {
      ttsCheck(e.session.id === this.expected.id);
      return null;
    }
    ttsCheck(
      "session_id" in e && e.session_id === this.expected.id && "stream_epoch" in e && "seq" in e,
    );
    safe(e.stream_epoch);
    safe(e.seq);
    ttsCheck(e.stream_epoch > 0);
    const body = await hash(canonical(e)),
      old = this.#events.get(e.event_id);
    if (old !== undefined) {
      ttsCheck(old === body);
      return null;
    }
    ttsCheck(e.seq > this.#s.seq && (this.#s.epoch === null || e.stream_epoch === this.#s.epoch));
    ttsCheck(this.#s.phase !== "disconnected" && this.#s.phase !== "closed");
    const next = structuredClone(this.#s);
    let audio: TtsAudio | null = null;
    if (e.type === "nexus.session.ready") {
      ttsCheck(next.phase === "awaiting");
      const c = e.effective_configuration,
        expected = this.expected.effective_configuration;
      ttsCheck(
        ref(c.profile, expected.profile) &&
          ref(c.models.tts, expected.models.tts) &&
          !c.models.asr &&
          !!c.parameters.tts &&
          !c.parameters.asr &&
          ref(c.voice, expected.voice) &&
          c.policy_revision === expected.policy_revision,
      );
      for (const n of Object.values(e.limits)) safe(n);
      ttsCheck(e.limits.max_message_bytes > 0 && e.limits.max_message_bytes <= 65536);
      ttsCheck(e.limits.max_session_duration_ms <= 8 * 60 * 60 * 1000);
      next.configuration = c;
      next.limits = e.limits;
      next.epoch = e.stream_epoch;
      next.phase = "active";
    } else {
      ttsCheck(next.phase !== "awaiting");
      const u = next.utterance;
      if (e.type === "nexus.session.closed") next.phase = "closed";
      else if (e.type === "nexus.session.gap") {
        next.gap = true;
        next.phase = "disconnected";
      } else if (e.type === "nexus.session.updated")
        ttsCheck(same(e.effective_configuration, next.configuration));
      else if (e.type === "nexus.tts.started") {
        ttsCheck(
          !!u &&
            (u.phase === "starting" || u.phase === "cancelling") &&
            u.operationId === null &&
            u.id === e.utterance_id &&
            typeof e.operation_id === "string" &&
            !this.#operations.has(e.operation_id),
        );
        safe(e.text_credit);
        ttsCheck(e.text_credit <= 1024 * 1024);
        u.operationId = e.operation_id;
        if (u.phase === "starting") u.phase = "accepting";
        u.credit = e.text_credit;
      } else if (e.type === "nexus.tts.text_ack") {
        ttsCheck(
          !!u &&
            u.id === e.utterance_id &&
            u.operationId === e.operation_id &&
            u.phase !== "terminal",
        );
        safe(e.accepted_through_text_seq);
        safe(e.text_credit);
        ttsCheck(
          e.text_credit <= 1024 * 1024 &&
            e.accepted_through_text_seq >= u.accepted &&
            e.accepted_through_text_seq <= u.sent,
        );
        // One outstanding append; a duplicate older ACK cannot replenish in-flight credit.
        if (e.accepted_through_text_seq === u.sent) {
          u.accepted = e.accepted_through_text_seq;
          u.credit = e.text_credit;
        }
      } else if (e.type === "nexus.tts.audio.delta") {
        ttsCheck(!!u && u.id === e.utterance_id && u.operationId === e.operation_id);
        if (u.phase !== "cancelling" && u.done?.status !== "cancelled") {
          ttsCheck(u.phase !== "starting" && u.phase !== "terminal");
          ttsCheck(
            u.committed > 0 || next.configuration!.parameters.tts!.text_commit_mode === "sentence",
          );
          safe(e.start_sample);
          safe(e.sample_count);
          safe(e.chunk_seq);
          safe(e.start_sample + e.sample_count);
          ttsCheck(
            e.chunk_seq === u.chunk + 1 &&
              e.start_sample === u.received &&
              e.sample_count > 0 &&
              e.sample_count <= 24000 * 5,
          );
          const raw = atob(e.audio);
          ttsCheck(btoa(raw) === e.audio && raw.length === e.sample_count * 2);
          const pcm = new Float32Array(e.sample_count);
          for (let i = 0; i < pcm.length; i++) {
            const n = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
            pcm[i] = (n >= 32768 ? n - 65536 : n) / 32768;
          }
          audio = {
            utteranceId: u.id,
            operationId: u.operationId!,
            startSample: e.start_sample,
            samples: pcm,
          };
          u.received += e.sample_count;
          u.chunk = e.chunk_seq;
        }
      } else if (e.type === "nexus.operation.done") {
        ttsCheck(e.kind === "tts" && !!u && e.operation_id === u.operationId && u.done === null);
        if (e.status === "succeeded")
          ttsCheck(
            u.phase === "finishing" &&
              !!e.receipt &&
              ["complete", "not_required"].includes(e.cleanup_state),
          );
        u.done = e;
        u.phase = "terminal";
      } else ttsCheck(false);
    }
    next.seq = e.seq;
    if (e.type === "nexus.tts.started") this.#operations.add(e.operation_id!);
    this.#events.set(e.event_id, body);
    if (this.#events.size > 512) this.#events.delete(this.#events.keys().next().value!);
    this.#s = freeze(next);
    return audio;
  }
}
