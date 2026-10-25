import { parse } from "../../../../packages/contracts/src/realtime";
import {
  checkKey,
  checkMode,
  checkSession,
  identity,
  type RealtimeRequest,
  type RealtimeSession,
  type CancelMode,
} from "./realtime-client";

export const realtimeJournalKey = "speechnexus.workbench.realtime-asr.v1";
export type RealtimeStorage = Pick<Storage, "getItem" | "setItem">;
export type RealtimeCommand =
  | { kind: "create"; key: string; body: RealtimeRequest }
  | { kind: "cancel"; key: string; sessionId: string; mode: CancelMode };
export type Caption = { sessionId: string; itemId: string; text: string };
export type RealtimeRecord = {
  version: 1;
  request: RealtimeRequest | null;
  session: RealtimeSession | null;
  pending: RealtimeCommand | null;
  captions: Caption[];
};
export function emptyRealtimeRecord(): RealtimeRecord {
  return { version: 1, request: null, session: null, pending: null, captions: [] };
}
export class RealtimeStorageError extends Error {
  constructor() {
    super("Realtime recovery unavailable");
  }
}
function object(input: unknown): Record<string, unknown> {
  identity(!!input && typeof input === "object" && !Array.isArray(input));
  return input as Record<string, unknown>;
}
function fields(input: Record<string, unknown>, keys: string[]) {
  identity(Object.keys(input).length === keys.length && keys.every((k) => Object.hasOwn(input, k)));
}
export function validateRecord(input: unknown): RealtimeRecord {
  const r = object(input);
  fields(r, ["version", "request", "session", "pending", "captions"]);
  identity(r.version === 1);
  const request = r.request === null ? null : parse("RealtimeSessionCreateRequest", r.request);
  if (request)
    identity(
      request.modalities.length === 1 &&
        request.modalities[0] === "asr" &&
        !!request.models.asr &&
        !request.models.tts,
    );
  const session =
    r.session === null ? null : checkSession(r.session, undefined, request ?? undefined);
  if (session) identity(request !== null);
  if (r.pending !== null) {
    const p = object(r.pending);
    identity(request !== null && typeof p.key === "string");
    checkKey(p.key);
    if (p.kind === "create") {
      fields(p, ["kind", "key", "body"]);
      const body = parse("RealtimeSessionCreateRequest", p.body);
      identity(session === null && JSON.stringify(body) === JSON.stringify(request));
    } else {
      fields(p, ["kind", "key", "sessionId", "mode"]);
      identity(p.kind === "cancel" && session !== null && p.sessionId === session.id);
      checkMode(p.mode as CancelMode);
    }
  }
  identity(Array.isArray(r.captions) && r.captions.length <= 10000);
  const ids = new Set<string>();
  let size = 0;
  for (const input of r.captions) {
    const c = object(input);
    fields(c, ["sessionId", "itemId", "text"]);
    identity(
      typeof c.sessionId === "string" &&
        c.sessionId.length > 0 &&
        c.sessionId.length <= 200 &&
        typeof c.itemId === "string" &&
        c.itemId.length > 0 &&
        c.itemId.length <= 200 &&
        typeof c.text === "string",
    );
    const id = JSON.stringify([c.sessionId, c.itemId]);
    identity(!ids.has(id));
    ids.add(id);
    size += new TextEncoder().encode(c.text).byteLength;
  }
  identity(size <= 4 * 1024 * 1024);
  return structuredClone(r) as RealtimeRecord;
}
function bounded(raw: string) {
  identity(new TextEncoder().encode(raw).byteLength <= 8 * 1024 * 1024);
}
/** Metadata, exact command intent and finalized text only. No preview, audio, credentials or automatic reconnect. */
export class RealtimeJournal {
  #raw: string | null;
  #record: RealtimeRecord;
  constructor(private readonly storage: RealtimeStorage) {
    try {
      this.#raw = storage.getItem(realtimeJournalKey);
      if (this.#raw !== null) bounded(this.#raw);
      this.#record =
        this.#raw === null
          ? emptyRealtimeRecord()
          : validateRecord(JSON.parse(this.#raw) as unknown);
    } catch {
      throw new RealtimeStorageError();
    }
  }
  load() {
    return structuredClone(this.#record);
  }
  save(record: RealtimeRecord) {
    try {
      const next = validateRecord(record),
        raw = JSON.stringify(next);
      bounded(raw);
      identity(this.storage.getItem(realtimeJournalKey) === this.#raw);
      this.storage.setItem(realtimeJournalKey, raw);
      this.#raw = raw;
      this.#record = next;
    } catch {
      throw new RealtimeStorageError();
    }
  }
}
