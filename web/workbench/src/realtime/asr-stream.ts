import { parse, type Contracts } from "../../../../packages/contracts/src/realtime_events";
import type { Contracts as RestContracts } from "../../../../packages/contracts/src/realtime";

export type RealtimeEvent = Contracts["RealtimeEvent"];
type Ready = Extract<RealtimeEvent, { type: "nexus.session.ready" }>;
type Done = Extract<RealtimeEvent, { type: "nexus.operation.done" }>;
export type AsrOperation = {
  id: string;
  status: Done["status"];
  cleanup: Done["cleanup_state"];
  receiptId: string | null;
};
export type AsrSegment = {
  id: string;
  segmentId: string | null;
  committed: boolean;
  previousItemId: string | null;
  itemId: string | null;
  operationId: string | null;
  contentIndex: number | null;
  preview: string;
  revision: number;
  delta: string;
  final: string | null;
  failed: boolean;
  span: { start: number; end: number } | null;
  closed: boolean;
  operation: AsrOperation | null;
  auxiliary: {
    textRevision: number;
    metadataRevision: number;
    alignment: string;
    diarization: string;
    verified: false;
  } | null;
};
export type AsrSnapshot = {
  phase: "awaiting" | "active" | "draining" | "disconnected" | "closed";
  sessionId: string;
  epoch: number | null;
  seq: number;
  sent: number;
  uncommitted: number;
  accepted: number | null;
  credit: number | null;
  limits: Ready["limits"] | null;
  configuration: Ready["effective_configuration"] | null;
  segments: AsrSegment[];
  operations: AsrOperation[];
  gaps: { reason: string; span: { start: number; end: number } | null }[];
  cleanup: Done["cleanup_state"] | null;
};
export class AsrProtocolError extends Error {
  constructor(readonly code = "realtime_protocol_error") {
    super(code);
  }
}
function check(valid: boolean, code?: string): asserts valid {
  if (!valid) throw new AsrProtocolError(code);
}
export function freeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
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
async function fingerprint(value: unknown) {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(value))),
    ),
  ]
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
}
function safe(n: number) {
  check(Number.isSafeInteger(n) && n >= 0);
}
function blank(id: string, itemId: string | null): AsrSegment {
  return {
    id,
    segmentId: null,
    committed: false,
    previousItemId: null,
    itemId,
    operationId: null,
    contentIndex: null,
    preview: "",
    revision: 0,
    delta: "",
    final: null,
    failed: false,
    span: null,
    closed: false,
    operation: null,
    auxiliary: null,
  };
}
const CLIENT_TYPES = new Set([
  "session.update",
  "input_audio_buffer.append",
  "input_audio_buffer.commit",
  "input_audio_buffer.clear",
  "nexus.session.close",
]);
const TEXT_LIMIT = 4 * 1024 * 1024,
  SEGMENT_LIMIT = 10000,
  COMMAND_LIMIT = 300000;

/** Consumer facts only: neither a speech producer nor an owner/lease authority. */
export class AsrStream {
  #s: AsrSnapshot;
  #created = false;
  #commands = new Map<string, string>(); // SHA-256 only; never retain audio payloads.
  #nexus = new Map<string, { seq: number; body: string }>();
  #standard = new Map<string, { item: string | null; body: string }>();
  #standardBytes = 0;
  #finalEvents = new Map<string, { id: string; body: string }>();
  constructor(
    private readonly options: {
      sessionId: string;
      mode: "nexus" | "transcription";
      expected?: RestContracts["RealtimeSession"];
    },
  ) {
    this.#s = freeze({
      phase: "awaiting",
      sessionId: options.sessionId,
      epoch: null,
      seq: -1,
      sent: 0,
      uncommitted: 0,
      accepted: null,
      credit: null,
      limits: null,
      configuration: null,
      segments: [],
      operations: [],
      gaps: [],
      cleanup: null,
    });
  }
  getSnapshot = () => this.#s;
  /** Local drain intent; neither operation success nor resource release evidence. */
  drain() {
    if (this.#s.phase === "active") this.#publish({ ...this.#s, phase: "draining" });
  }
  #publish(next: AsrSnapshot) {
    check(
      next.segments.length <= SEGMENT_LIMIT && next.operations.length <= SEGMENT_LIMIT,
      "realtime_capacity",
    );
    const chars = next.segments.reduce(
      (n, s) => n + s.preview.length + s.delta.length + (s.final?.length ?? 0),
      0,
    );
    // UTF-16 strings use at most four UTF-8 bytes per code point; also cap exact encoded bytes.
    check(
      chars <= TEXT_LIMIT &&
        new TextEncoder().encode(
          next.segments.map((s) => s.preview + s.delta + (s.final ?? "")).join(""),
        ).byteLength <= TEXT_LIMIT,
      "realtime_capacity",
    );
    if (
      next.segments.some((row, i) => row !== this.#s.segments[i]) ||
      next.segments.length !== this.#s.segments.length
    )
      next.segments = this.#ordered(next.segments);
    this.#s = freeze(next);
  }
  #ordered(rows: AsrSegment[]) {
    const items = new Map(
        rows.flatMap((r, i) => (r.itemId === null ? [] : [[r.itemId, i] as const])),
      ),
      parents = rows.map(() => new Set<number>());
    rows.forEach((r, i) => {
      if (r.committed && r.previousItemId !== null) {
        const previous = items.get(r.previousItemId);
        if (previous !== undefined) parents[i]!.add(previous);
      }
    });
    const spans = rows
      .map((r, i) => ({ row: r, index: i }))
      .filter((x) => x.row.span !== null)
      .sort((a, b) => a.row.span!.start - b.row.span!.start);
    for (let i = 1; i < spans.length; i++) {
      const before = spans[i - 1]!,
        after = spans[i]!;
      if (before.row.span!.start < after.row.span!.start) parents[after.index]!.add(before.index);
    }
    const states = new Uint8Array(rows.length),
      ordered: AsrSegment[] = [];
    for (let i = 0; i < rows.length; i++) {
      if (states[i] === 2) continue;
      const stack: [number, boolean][] = [[i, false]];
      while (stack.length) {
        const [index, finish] = stack.pop()!;
        if (finish) {
          states[index] = 2;
          ordered.push(rows[index]!);
          continue;
        }
        if (states[index] === 2) continue;
        check(states[index] === 0, "realtime_input_order_conflict");
        states[index] = 1;
        stack.push([index, true]);
        for (const parent of parents[index]!) stack.push([parent, false]);
      }
    }
    return ordered;
  }
  #limits(value: Ready["limits"]) {
    Object.values(value).forEach(safe);
    check(
      value.max_message_bytes <= 65536 &&
        value.max_input_chunk_ms <= 250 &&
        value.input_credit_samples <= 48000,
    );
    return value;
  }
  #session(value: Extract<RealtimeEvent, { type: "session.created" }>["session"]) {
    check(value.id === this.options.sessionId);
    const input = value.audio?.input;
    if (input?.format)
      check(
        input.format.type === "audio/pcm" &&
          (input.format.rate === undefined || input.format.rate === 24000),
      );
  }
  #row(next: AsrSnapshot, segmentId: string | null, itemId: string | null): AsrSegment {
    const bySegment = next.segments.findIndex(
      (s) => segmentId !== null && s.segmentId === segmentId,
    );
    const byItem = next.segments.findIndex((s) => itemId !== null && s.itemId === itemId);
    let index = bySegment >= 0 ? bySegment : byItem;
    if (bySegment >= 0 && byItem >= 0 && bySegment !== byItem) {
      const segment = next.segments[bySegment]!,
        item = next.segments[byItem]!;
      check(item.segmentId === null && segment.itemId === null);
      check(
        segment.final === null &&
          !segment.failed &&
          (segment.contentIndex === null || segment.contentIndex === item.contentIndex),
      );
      next.segments[bySegment] = {
        ...segment,
        itemId: item.itemId,
        contentIndex: item.contentIndex,
        final: item.final,
        failed: item.failed,
        delta: item.delta,
        preview: item.final !== null || item.failed ? "" : segment.preview,
        committed: item.committed,
        previousItemId: item.previousItemId,
      };
      next.segments.splice(byItem, 1);
      index = bySegment - (byItem < bySegment ? 1 : 0);
    }
    if (index < 0) {
      index = next.segments.length;
      next.segments.push(blank(segmentId ?? `item:${itemId!}`, itemId));
    }
    const old = next.segments[index]!,
      row = { ...old };
    if (itemId !== null) check(row.itemId === null || row.itemId === itemId);
    if (segmentId !== null) {
      check(row.segmentId === null || row.segmentId === segmentId);
      row.id = segmentId;
      row.segmentId = segmentId;
    }
    if (itemId !== null) row.itemId = itemId;
    next.segments[index] = row;
    return row;
  }
  /** Validate, enqueue synchronously, then commit local send facts; this is not a server ACK. */
  async command(input: unknown, send: (wire: string) => void = () => {}) {
    const event = parse("RealtimeEvent", input);
    check(CLIENT_TYPES.has(event.type));
    const hash = await fingerprint(event);
    if (event.event_id) {
      const old = this.#commands.get(event.event_id);
      if (old) {
        check(old === hash, "idempotency_conflict");
        check(this.#s.phase !== "disconnected" && this.#s.phase !== "closed");
        send(JSON.stringify(event));
        return { event, duplicate: true };
      }
    }
    check(this.#s.phase === "active" || (event.type === "nexus.session.close" && this.#created));
    const next = { ...this.#s };
    let uncommitted = this.#s.uncommitted;
    const size = new TextEncoder().encode(JSON.stringify(event)).byteLength;
    check(size <= (next.limits?.max_message_bytes ?? 65536), "realtime_message_limit");
    if (event.type === "input_audio_buffer.append") {
      const bytes = atob(event.audio).length;
      check(bytes > 0 && bytes % 2 === 0, "invalid_audio");
      check(bytes / 48 <= (next.limits?.max_input_chunk_ms ?? 250), "realtime_chunk_limit");
      if (this.options.mode === "nexus")
        check(next.credit !== null && bytes / 2 <= next.credit, "realtime_backpressure");
      safe(next.sent + bytes / 2);
      next.sent += bytes / 2;
      uncommitted += bytes / 2;
      if (next.credit !== null) next.credit -= bytes / 2;
    } else if (event.type === "input_audio_buffer.commit") {
      check(uncommitted > 0, "empty_audio_buffer");
      uncommitted = 0;
    } else if (event.type === "input_audio_buffer.clear") uncommitted = 0;
    else if (event.type === "nexus.session.close") next.phase = "draining";
    else if (event.type === "session.update") {
      check(this.options.mode === "transcription", "realtime_update_unsupported");
      const format = event.session.audio?.input?.format;
      if (format)
        check(format.type === "audio/pcm" && (format.rate === undefined || format.rate === 24000));
    }
    if (event.event_id) {
      check(this.#commands.size < COMMAND_LIMIT, "realtime_capacity");
    }
    send(JSON.stringify(event));
    if (event.event_id) {
      this.#commands.set(event.event_id, hash);
    }
    next.uncommitted = uncommitted;
    this.#publish(next);
    return { event, duplicate: false };
  }
  receive(input: unknown) {
    const e = parse("RealtimeEvent", input);
    check(!CLIENT_TYPES.has(e.type) && !e.type.startsWith("nexus.tts."));
    check(this.#s.phase !== "disconnected" && this.#s.phase !== "closed");
    const body = canonical(e);
    check(
      new TextEncoder().encode(body).byteLength <= (this.#s.limits?.max_message_bytes ?? 65536),
      "realtime_message_limit",
    );
    let nexus = false;
    if ("seq" in e) {
      nexus = true;
      check(this.options.mode === "nexus" && e.session_id === this.options.sessionId);
      safe(e.seq);
      safe(e.stream_epoch);
      if (this.#s.epoch !== null)
        check(e.stream_epoch === this.#s.epoch, "realtime_epoch_mismatch");
      const old = this.#nexus.get(e.event_id);
      if (old) {
        check(old.body === body, "realtime_event_conflict");
        return;
      }
      if (e.seq <= this.#s.seq) return;
    }
    if (!nexus && e.event_id) {
      const old = this.#standard.get(e.event_id);
      if (old) {
        check(old.body === body, "realtime_event_conflict");
        return;
      }
    }
    if (this.options.mode === "nexus" && this.#s.epoch === null)
      check(
        ["session.created", "session.updated", "nexus.session.ready", "error"].includes(e.type),
        "realtime_not_ready",
      );
    const next = {
      ...this.#s,
      segments: this.#s.segments.slice(),
      operations: this.#s.operations.slice(),
      gaps: this.#s.gaps.slice(),
    };
    if (e.type === "session.created") {
      this.#session(e.session);
      check(!this.#created || this.#standard.get(e.event_id)?.body === body);
      if (!this.#created) {
        next.limits = e.session.nexus?.limits ? this.#limits(e.session.nexus.limits) : null;
        next.phase = this.options.mode === "transcription" ? "active" : "awaiting";
      }
    } else {
      check(this.#created, "realtime_not_created");
      if (e.type === "nexus.session.ready") {
        check(next.epoch === null);
        const config = e.effective_configuration,
          expected = this.options.expected?.effective_configuration;
        check(
          !!config.models.asr &&
            !config.models.tts &&
            !!config.parameters.asr &&
            !config.parameters.tts,
        );
        if (expected) {
          check(
            config.profile.id === expected.profile?.id &&
              config.profile.revision === expected.profile.revision &&
              config.models.asr.id === expected.models.asr?.id &&
              config.models.asr.revision === expected.models.asr.revision &&
              config.policy_revision === expected.policy_revision,
          );
        }
        safe(e.input_credit_samples);
        next.limits = this.#limits(e.limits);
        check(e.input_credit_samples <= next.limits.input_credit_samples);
        next.configuration = config;
        next.epoch = e.stream_epoch;
        next.accepted = 0;
        next.credit = e.input_credit_samples;
        next.phase = "active";
      } else if (e.type === "session.updated") {
        this.#session(e.session);
      } else if (e.type === "nexus.session.updated") {
        check(
          next.configuration !== null &&
            BigInt(e.effective_configuration.config_revision) >=
              BigInt(next.configuration.config_revision),
        );
        // This consumer does not issue live policy changes; do not apply a future segment configuration early.
        check(
          canonical(e.effective_configuration) === canonical(next.configuration),
          "realtime_configuration_changed",
        );
      } else if (e.type === "nexus.input.ack") {
        safe(e.accepted_through_sample);
        safe(e.credit_samples);
        check(
          next.accepted !== null &&
            e.accepted_through_sample >= next.accepted &&
            e.accepted_through_sample <= next.sent &&
            next.limits !== null &&
            e.credit_samples <= next.limits.input_credit_samples,
        );
        next.accepted = e.accepted_through_sample;
        // ACK credit was computed before any later locally sent input reached the server.
        next.credit = Math.max(0, e.credit_samples - (next.sent - e.accepted_through_sample));
      } else if (e.type === "input_audio_buffer.committed") {
        const row = this.#row(next, null, e.item_id),
          previous = e.previous_item_id ?? null;
        check(!row.committed || row.previousItemId === previous, "realtime_input_order_conflict");
        row.committed = true;
        row.previousItemId = previous;
      } else if (
        e.type === "nexus.transcription.hypothesis" ||
        e.type === "nexus.transcription.segment_closed"
      ) {
        check(typeof e.operation_id === "string");
        safe(e.sample_span.start);
        safe(e.sample_span.end);
        const row = this.#row(next, e.segment_id, e.item_id ?? null);
        check(row.operationId === null || row.operationId === e.operation_id);
        row.operationId = e.operation_id;
        row.operation = next.operations.find((op) => op.id === e.operation_id) ?? row.operation;
        if (e.type === "nexus.transcription.hypothesis") {
          safe(e.revision);
          if (row.final !== null || row.failed) return;
          check(e.revision >= row.revision);
          if (e.revision === row.revision) check(row.preview === e.text);
          row.preview = e.text;
          row.revision = e.revision;
          if (!row.closed) row.span = e.sample_span;
        } else {
          check(!row.closed || canonical(row.span) === canonical(e.sample_span));
          row.span = e.sample_span;
          row.closed = true;
          if (e.content_index !== undefined) {
            check(row.contentIndex === null || row.contentIndex === e.content_index);
            row.contentIndex = e.content_index;
          }
        }
      } else if (
        e.type === "conversation.item.input_audio_transcription.delta" ||
        e.type === "conversation.item.input_audio_transcription.completed" ||
        e.type === "conversation.item.input_audio_transcription.failed"
      ) {
        const row = this.#row(next, null, e.item_id);
        check(row.contentIndex === null || row.contentIndex === e.content_index);
        row.contentIndex = e.content_index;
        const final = this.#finalEvents.get(e.item_id);
        if (row.final !== null || row.failed) {
          if (e.type === "conversation.item.input_audio_transcription.delta") return;
          check(
            e.type === "conversation.item.input_audio_transcription.completed" &&
              row.final === e.transcript,
          );
          if (final?.id === e.event_id) check(final.body === body, "realtime_event_conflict");
          return;
        }
        const old = this.#standard.get(e.event_id);
        if (old) {
          check(old.body === body, "realtime_event_conflict");
          return;
        }
        if (e.type === "conversation.item.input_audio_transcription.delta") row.delta += e.delta;
        else {
          row.final =
            e.type === "conversation.item.input_audio_transcription.completed"
              ? e.transcript
              : null;
          row.failed = e.type === "conversation.item.input_audio_transcription.failed";
          row.preview = "";
          row.delta = "";
        }
      } else if (e.type === "nexus.transcription.metadata") {
        check(typeof e.operation_id === "string");
        safe(e.text_revision);
        safe(e.metadata_revision);
        const row = next.segments.find((s) => s.id === e.segment_id);
        check(!!row && row.operationId === e.operation_id);
        if (
          !row.auxiliary ||
          (e.text_revision >= row.auxiliary.textRevision &&
            e.metadata_revision > row.auxiliary.metadataRevision)
        ) {
          const changed = {
            ...row,
            auxiliary: {
              textRevision: e.text_revision,
              metadataRevision: e.metadata_revision,
              alignment: e.alignment.status,
              diarization: e.diarization.status,
              verified: false as const,
            },
          };
          next.segments[next.segments.indexOf(row)] = changed;
        }
      } else if (e.type === "nexus.operation.done") {
        check(e.kind === "asr" && typeof e.operation_id === "string");
        const op: AsrOperation = {
          id: e.operation_id,
          status: e.status,
          cleanup: e.cleanup_state,
          receiptId: e.receipt?.receipt_id ?? null,
        };
        const old = next.operations.find((x) => x.id === op.id);
        if (old) check(canonical(old) === canonical(op), "realtime_operation_conflict");
        else next.operations.push(op);
        next.segments = next.segments.map((s) =>
          s.operationId === op.id ? { ...s, operation: op } : s,
        );
      } else if (e.type === "nexus.session.gap") {
        next.gaps.push({ reason: e.reason, span: e.sample_span ?? null });
      } else if (e.type === "nexus.session.closed") {
        check(new Set(e.operations.map((o) => o.operation_id)).size === e.operations.length);
        next.phase = "closed";
        next.cleanup = e.cleanup_state;
      } else if (e.type === "error") throw new AsrProtocolError("realtime_server_error");
      else
        check(
          [
            "input_audio_buffer.cleared",
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
          ].includes(e.type),
        );
    }
    if ("seq" in e) next.seq = e.seq;
    check(next.gaps.length <= 1000, "realtime_capacity");
    if (!nexus)
      check(
        this.#standard.size < COMMAND_LIMIT &&
          this.#standardBytes + body.length * 2 <= 16 * 1024 * 1024,
        "realtime_capacity",
      );
    this.#publish(next);
    if (e.type === "session.created") this.#created = true;
    if (nexus && "seq" in e) {
      this.#nexus.set(e.event_id, { seq: e.seq, body });
      if (this.#nexus.size > 512) this.#nexus.delete(this.#nexus.keys().next().value!);
    } else {
      check(typeof e.event_id === "string");
      const item = "item_id" in e ? e.item_id : null;
      if (
        e.type === "conversation.item.input_audio_transcription.completed" ||
        e.type === "conversation.item.input_audio_transcription.failed"
      ) {
        this.#finalEvents.set(e.item_id, { id: e.event_id, body });
      }
      this.#standard.set(e.event_id, { item, body });
      this.#standardBytes += body.length * 2;
    }
  }
  disconnect(reason: string) {
    if (this.#s.phase === "disconnected" || this.#s.phase === "closed") return;
    this.#publish({
      ...this.#s,
      phase: "disconnected",
      gaps: [
        ...this.#s.gaps,
        {
          reason,
          span: this.#s.accepted === null ? null : { start: this.#s.accepted, end: this.#s.sent },
        },
      ],
    });
  }
}
