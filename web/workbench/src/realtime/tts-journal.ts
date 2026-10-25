import { parse } from "../../../../packages/contracts/src/realtime";
import {
  checkKey,
  checkMode,
  checkSession,
  type RealtimeRequest,
  type RealtimeSession,
} from "./realtime-client";
import type { RealtimeCommand, RealtimeStorage } from "./realtime-journal";
import { ttsCheck } from "./tts-stream";

export const ttsJournalKey = "speechnexus.workbench.realtime-tts.v1";
export type TtsRecord = {
  version: 1;
  request: RealtimeRequest | null;
  session: RealtimeSession | null;
  pending: RealtimeCommand | null;
  text: string;
};
function fields(value: unknown, keys: string[]): asserts value is Record<string, unknown> {
  ttsCheck(!!value && typeof value === "object" && !Array.isArray(value));
  ttsCheck(Object.keys(value).length === keys.length && keys.every((k) => Object.hasOwn(value, k)));
}
function validate(input: unknown): TtsRecord {
  fields(input, ["version", "request", "session", "pending", "text"]);
  ttsCheck(
    input.version === 1 &&
      typeof input.text === "string" &&
      new TextEncoder().encode(input.text).length <= 1024 * 1024,
  );
  const request =
    input.request === null ? null : parse("RealtimeSessionCreateRequest", input.request);
  if (request)
    ttsCheck(
      request.modalities.length === 1 &&
        request.modalities[0] === "tts" &&
        !!request.models.tts &&
        !request.models.asr,
    );
  const session =
    input.session === null ? null : checkSession(input.session, undefined, request ?? undefined);
  ttsCheck(!session || !!request);
  if (input.pending !== null) {
    ttsCheck(
      !!request &&
        !!input.pending &&
        typeof input.pending === "object" &&
        !Array.isArray(input.pending),
    );
    const p = input.pending as Record<string, unknown>;
    if (p.kind === "create") {
      fields(p, ["kind", "key", "body"]);
      ttsCheck(
        !session &&
          JSON.stringify(parse("RealtimeSessionCreateRequest", p.body)) === JSON.stringify(request),
      );
    } else {
      fields(p, ["kind", "key", "sessionId", "mode"]);
      ttsCheck(p.kind === "cancel" && !!session && p.sessionId === session.id);
      checkMode(p.mode as "drain" | "abort");
    }
    ttsCheck(typeof p.key === "string");
    checkKey(p.key);
  }
  return structuredClone(input) as TtsRecord;
}
/** Exact text and REST intent only. Never PCM or WS command replay. */
export class TtsJournal {
  #raw: string | null;
  #record: TtsRecord;
  constructor(private readonly storage: RealtimeStorage) {
    this.#raw = storage.getItem(ttsJournalKey);
    ttsCheck(this.#raw === null || new TextEncoder().encode(this.#raw).length <= 4 * 1024 * 1024);
    this.#record =
      this.#raw === null
        ? { version: 1, request: null, session: null, pending: null, text: "" }
        : validate(JSON.parse(this.#raw) as unknown);
  }
  load() {
    return structuredClone(this.#record);
  }
  save(input: TtsRecord) {
    const next = validate(input),
      raw = JSON.stringify(next);
    ttsCheck(
      new TextEncoder().encode(raw).length <= 4 * 1024 * 1024 &&
        this.storage.getItem(ttsJournalKey) === this.#raw,
    );
    this.storage.setItem(ttsJournalKey, raw);
    this.#raw = raw;
    this.#record = next;
  }
}
