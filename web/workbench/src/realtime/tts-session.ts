import { freeze } from "./asr-stream";
import {
  checkSession,
  connectionUrl,
  RealtimeResponseError,
  type RealtimeClient,
  type RealtimeRequest,
  type RealtimeSession,
  type CancelMode,
} from "./realtime-client";
import type { RealtimeSocket } from "./realtime-session";
import type { TtsJournal, TtsRecord } from "./tts-journal";
import { TtsStream, ttsCheck, type TtsSnapshot } from "./tts-stream";
import { openTtsPlayback, PlaybackCleanupError, type TtsPlayback } from "./tts-playback";

export type TtsControllerSnapshot = {
  record: TtsRecord;
  current: RealtimeSession | null;
  stream: TtsSnapshot | null;
  playback: ReturnType<TtsPlayback["snapshot"]> | null;
  busy: boolean;
  connecting: boolean;
  starting: boolean;
  blocked: boolean;
  problem:
    | "storage"
    | "unknown"
    | "rejected"
    | "read_failed"
    | "protocol"
    | "playback"
    | "playback_cleanup"
    | null;
};
type Dependencies = { socket: (url: URL) => RealtimeSocket; playback: typeof openTtsPlayback };
const released = (s: RealtimeSession) =>
  ["closed", "failed", "expired"].includes(s.status) &&
  ["complete", "not_required"].includes(s.cleanup_state);

/** One active socket/playback generation. Refresh restores intent/text, never audio or WS commands. */
export class TtsController {
  #s: TtsControllerSnapshot;
  #listeners = new Set<() => void>();
  #disposed = false;
  #active = true;
  #socket: RealtimeSocket | null = null;
  #stream: TtsStream | null = null;
  #playback: TtsPlayback | null = null;
  #stopping: Promise<void> | null = null;
  #cleanupFailed = false;
  #abort: AbortController | null = null;
  #generation = 0;
  #reads = 0;
  #timer: ReturnType<typeof setInterval> | null = null;
  #handshake: ReturnType<typeof setTimeout> | null = null;
  #queue = Promise.resolve();
  #queuedBytes = 0;
  #queuedMessages = 0;
  #deps: Dependencies;
  constructor(
    private readonly client: Pick<RealtimeClient, "create" | "current" | "cancel">,
    private readonly journal: TtsJournal,
    deps: Partial<Dependencies> = {},
  ) {
    const record = journal.load();
    this.#s = freeze({
      record,
      current: null,
      stream: null,
      playback: null,
      busy: false,
      connecting: false,
      starting: false,
      blocked: false,
      problem: record.pending ? "unknown" : null,
    });
    this.#deps = { socket: (url) => new WebSocket(url), playback: openTtsPlayback, ...deps };
  }
  getSnapshot = () => this.#s;
  /** Completion of the current bounded event queue; does not wait for network or compute. */
  settled = () => this.#queue;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #publish(change: Partial<TtsControllerSnapshot>) {
    if (this.#disposed) return;
    this.#s = freeze({ ...this.#s, ...change });
    if (this.#cleanupFailed && this.#s.problem !== "storage")
      this.#s = freeze({ ...this.#s, problem: "playback_cleanup" });
    this.#listeners.forEach((f) => f());
  }
  #persist(record: TtsRecord) {
    try {
      this.journal.save(record);
      this.#publish({ record, blocked: false });
      return true;
    } catch {
      this.#publish({ record, blocked: true, problem: "storage" });
      this.#drop();
      return false;
    }
  }
  setText(text: string) {
    if (this.#disposed) return;
    this.#persist({ ...this.#s.record, text });
  }
  retryStorage() {
    if (this.#disposed) return;
    if (this.#persist(this.#s.record))
      this.#publish({ problem: this.#s.record.pending ? "unknown" : null });
  }
  async create(request: RealtimeRequest) {
    if (
      this.#locked() ||
      this.#s.starting ||
      this.#playback ||
      this.#stopping ||
      this.#cleanupFailed ||
      this.#s.connecting ||
      (this.#s.record.session && (!this.#s.current || !released(this.#s.current)))
    )
      return;
    const next = {
      ...this.#s.record,
      request,
      session: null,
      pending: { kind: "create" as const, key: crypto.randomUUID(), body: request },
    };
    if (!this.#persist(next)) return;
    this.#drop();
    this.#stream = null;
    ++this.#reads;
    this.#publish({ stream: null, current: null, playback: null });
    await this.replay();
  }
  #locked() {
    return (
      this.#disposed || !this.#active || this.#s.busy || this.#s.blocked || !!this.#s.record.pending
    );
  }
  async replay() {
    await this.#dispatchPending(false);
  }
  async #dispatchPending(allowInactive: boolean) {
    const pending = this.#s.record.pending;
    if (
      !pending ||
      this.#disposed ||
      (!this.#active && !allowInactive) ||
      this.#s.busy ||
      this.#s.blocked
    )
      return;
    if (!this.#persist(this.#s.record)) return;
    this.#publish({ busy: true, problem: null });
    try {
      const value =
        pending.kind === "create"
          ? await this.client.create(pending.body, pending.key)
          : await this.client.cancel(pending.sessionId, pending.mode, pending.key);
      const session = checkSession(
        value,
        pending.kind === "cancel" ? pending.sessionId : undefined,
        this.#s.record.request ?? undefined,
      );
      if (this.#disposed) return;
      this.#persist({ ...this.#s.record, session, pending: null });
      this.#publish({ current: null });
    } catch (error) {
      if (!this.#disposed)
        this.#publish({
          problem:
            error instanceof RealtimeResponseError && error.status >= 400 && error.status < 500
              ? "rejected"
              : "unknown",
        });
    } finally {
      this.#publish({ busy: false });
    }
  }
  dismissRejected() {
    if (this.#s.problem === "rejected" && !this.#s.busy && this.#s.record.pending)
      this.#persist({ ...this.#s.record, pending: null });
  }
  async refresh() {
    const r = this.#s.record,
      id = r.session?.id,
      read = ++this.#reads;
    if (!id || this.#disposed) return;
    this.#publish({ current: null });
    try {
      const current = checkSession(await this.client.current(id), id, r.request ?? undefined);
      if (!this.#disposed && read === this.#reads && this.#s.record.session?.id === id)
        this.#publish({
          current,
          problem: this.#s.blocked ? "storage" : this.#s.record.pending ? "unknown" : null,
        });
    } catch {
      if (!this.#disposed && read === this.#reads)
        this.#publish({ current: null, problem: "read_failed" });
    }
  }
  async connect() {
    if (
      this.#locked() ||
      this.#socket ||
      this.#stream ||
      this.#s.connecting ||
      !this.#s.record.session
    )
      return;
    this.#publish({ connecting: true });
    const generation = ++this.#generation;
    await this.refresh();
    if (this.#disposed || !this.#active || generation !== this.#generation) {
      this.#publish({ connecting: false });
      return;
    }
    try {
      const current = this.#s.current;
      ttsCheck(
        !!current && current.status === "created" && Date.parse(current.expires_at) > Date.now(),
      );
      ttsCheck(this.#persist(this.#s.record));
      const stream = (this.#stream = new TtsStream(current)),
        socket = (this.#socket = this.#deps.socket(connectionUrl(current)));
      const owns = () =>
        !this.#disposed &&
        this.#active &&
        this.#socket === socket &&
        generation === this.#generation;
      this.#publish({ stream: stream.getSnapshot() });
      this.#handshake = setTimeout(() => {
        if (owns()) this.#fail("protocol");
      }, 30000);
      socket.addEventListener("message", (message) => {
        if (!owns()) return;
        const data = (message as MessageEvent<unknown>).data;
        if (typeof data !== "string" || new TextEncoder().encode(data).length > 65536) {
          this.#fail("protocol");
          return;
        }
        const count = new TextEncoder().encode(data).length;
        this.#queuedBytes += count;
        this.#queuedMessages++;
        if (this.#queuedBytes > 330000 || this.#queuedMessages > 512) {
          this.#fail("protocol");
          return;
        }
        this.#queue = this.#queue
          .then(async () => {
            if (!owns()) return;
            const audio = await stream.receive(JSON.parse(data) as unknown);
            if (!owns()) return;
            if (audio) {
              ttsCheck(!!this.#playback);
              this.#playback.push(audio.startSample, audio.samples);
            }
            const state = stream.getSnapshot();
            this.#publish({
              stream: state,
              current: null,
              playback: this.#playback?.snapshot() ?? this.#s.playback,
            });
            if (state.phase !== "awaiting" && this.#handshake) {
              clearTimeout(this.#handshake);
              this.#handshake = null;
            }
            if (state.phase === "closed") {
              this.#socket = null;
              socket.close();
              void this.refresh();
            }
            if (state.phase === "disconnected") this.#fail("protocol");
          })
          .catch(() => {
            if (owns()) this.#fail("protocol");
          })
          .finally(() => {
            this.#queuedBytes -= count;
            this.#queuedMessages--;
          });
      });
      socket.addEventListener("close", () => {
        if (owns()) {
          stream.disconnect();
          this.#publish({ stream: stream.getSnapshot() });
          this.#drop();
          void this.refresh();
        }
      });
      socket.addEventListener("error", () => {
        if (owns()) this.#fail("protocol");
      });
    } catch {
      this.#publish({ problem: "read_failed" });
      this.#drop();
    } finally {
      this.#publish({ connecting: false });
    }
  }
  #send(input: unknown) {
    ttsCheck(!this.#locked() && !!this.#socket && this.#socket.readyState === 1 && !!this.#stream);
    ttsCheck(this.#persist(this.#s.record));
    this.#stream.command(input, (wire) => {
      ttsCheck(this.#socket!.bufferedAmount + new TextEncoder().encode(wire).length <= 65536);
      this.#socket!.send(wire);
    });
    this.#publish({ stream: this.#stream.getSnapshot(), current: null, problem: null });
  }
  async start(id: string = crypto.randomUUID()) {
    if (
      this.#locked() ||
      this.#s.starting ||
      this.#playback ||
      this.#stopping ||
      this.#cleanupFailed ||
      !this.#stream ||
      this.#stream.getSnapshot().phase !== "active"
    )
      return;
    const old = this.#stream.getSnapshot().utterance;
    if (old && (!old.done || !["complete", "not_required"].includes(old.done.cleanup_state)))
      return;
    const generation = this.#generation,
      abort = (this.#abort = new AbortController());
    this.#publish({ starting: true });
    const timeout = setTimeout(() => abort.abort(), 15000);
    try {
      const playback = await this.#deps.playback(
        this.#stream.getSnapshot().epoch!,
        generation,
        abort.signal,
      );
      this.#playback = playback;
      if (
        this.#disposed ||
        !this.#active ||
        abort.signal.aborted ||
        this.#abort !== abort ||
        generation !== this.#generation
      ) {
        await this.#stopPlayback();
        return;
      }
      this.#send({ type: "nexus.tts.start", event_id: crypto.randomUUID(), utterance_id: id });
      this.#timer = setInterval(() => {
        const p = this.#playback;
        if (!p) return;
        const state = p.snapshot();
        this.#publish({ playback: state });
        if (state.state === "failed") this.#fail("playback");
        const u = this.#stream?.getSnapshot().utterance;
        if (u?.done && state.rendered === state.received) {
          void this.#stopPlayback();
        }
      }, 50);
      this.#publish({ playback: playback.snapshot() });
    } catch (error) {
      if (error instanceof PlaybackCleanupError) {
        this.#cleanupFailed = true;
        this.#publish({ playback: { state: "failed", received: 0, rendered: 0 } });
      } else if (!abort.signal.aborted && !this.#disposed) {
        this.#publish({ problem: "playback" });
        void this.#stopPlayback();
      }
    } finally {
      clearTimeout(timeout);
      this.#publish({ starting: false });
    }
  }
  #command(kind: string, fields: Record<string, unknown> = {}) {
    try {
      const id = this.#stream?.getSnapshot().utterance?.id;
      ttsCheck(!!id);
      this.#send({
        type: `nexus.tts.${kind}`,
        event_id: crypto.randomUUID(),
        utterance_id: id,
        ...fields,
      });
    } catch {
      if (!this.#s.blocked) this.#publish({ problem: "protocol" });
    }
  }
  append() {
    const u = this.#s.stream?.utterance;
    if (!u || u.phase !== "accepting" || u.sent !== u.accepted || !this.#s.record.text) return;
    this.#command("append_text", { text_seq: u.sent + 1, text: this.#s.record.text });
  }
  commit() {
    const u = this.#s.stream?.utterance;
    if (u && u.accepted > u.committed)
      this.#command("commit_text", { through_text_seq: u.accepted });
  }
  finish() {
    const u = this.#s.stream?.utterance;
    if (u?.phase === "accepting" && u.sent > 0 && u.sent === u.accepted)
      this.#command("finish_text");
  }
  cancel() {
    void this.#stopPlayback();
    const u = this.#s.stream?.utterance;
    if (u && u.phase !== "terminal" && u.phase !== "cancelling") this.#command("cancel");
  }
  async close(mode: CancelMode) {
    if (mode === "abort") this.#drop();
    if (this.#locked() || !this.#s.record.session) return;
    if (mode === "drain") {
      this.#stream?.drain();
      this.#publish({ stream: this.#stream?.getSnapshot() ?? null, current: null });
    }
    const r = {
      ...this.#s.record,
      pending: {
        kind: "cancel" as const,
        key: crypto.randomUUID(),
        sessionId: this.#s.record.session.id,
        mode,
      },
    };
    if (this.#persist(r)) await this.replay();
  }
  #stopPlayback() {
    this.#abort?.abort();
    this.#abort = null;
    if (this.#timer) {
      clearInterval(this.#timer);
      this.#timer = null;
    }
    if (this.#stopping) return this.#stopping;
    const p = this.#playback;
    if (!p) return Promise.resolve();
    let closing: Promise<void>;
    try {
      closing = p.stop();
    } catch {
      closing = Promise.reject(new PlaybackCleanupError());
    }
    this.#publish({ playback: p.snapshot() });
    const stopping = closing
      .then(() => {
        ttsCheck(p.snapshot().state === "stopped");
        if (this.#playback !== p) return;
        this.#playback = null;
        this.#publish({ playback: p.snapshot() });
      })
      .catch(() => {
        if (this.#playback !== p) return;
        this.#cleanupFailed = true;
        this.#publish({ playback: { ...p.snapshot(), state: "failed" } });
      })
      .finally(() => {
        if (this.#stopping === stopping) this.#stopping = null;
      });
    this.#stopping = stopping;
    return stopping;
  }
  #drop() {
    ++this.#generation;
    void this.#stopPlayback();
    if (this.#handshake) {
      clearTimeout(this.#handshake);
      this.#handshake = null;
    }
    const socket = this.#socket;
    this.#socket = null;
    socket?.close();
  }
  #fail(problem: "protocol" | "playback") {
    this.#stream?.disconnect();
    this.#publish({ problem, stream: this.#stream?.getSnapshot() ?? null });
    this.#drop();
    if (!this.#s.record.pending && !this.#s.blocked && this.#active)
      void this.close("abort").finally(() => {
        if (!this.#disposed) this.#publish({ problem });
      });
  }
  async setActive(active: boolean) {
    if (this.#disposed) return;
    const previous = this.#active;
    this.#active = active;
    if (previous && !active) {
      this.#stream?.disconnect();
      this.#publish({ stream: this.#stream?.getSnapshot() ?? null });
      this.#drop();
      // Persist cleanup intent while inactive; dispatch only if it is the original accepted session.
      if (!this.#s.record.pending && !this.#s.blocked && !this.#s.busy && this.#s.record.session) {
        const r = {
          ...this.#s.record,
          pending: {
            kind: "cancel" as const,
            key: crypto.randomUUID(),
            sessionId: this.#s.record.session.id,
            mode: "abort" as const,
          },
        };
        if (this.#persist(r)) {
          await this.#dispatchPending(true);
        }
      }
    } else if (active && this.#s.record.session) await this.refresh();
  }
  suspend() {
    void this.setActive(false);
  }
  dispose() {
    if (this.#disposed) return;
    this.#disposed = true;
    ++this.#reads;
    this.#drop();
    this.#listeners.clear();
  }
}
