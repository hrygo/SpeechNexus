import { AsrProtocolError, AsrStream, freeze, type AsrSnapshot } from "./asr-stream";
import {
  RealtimeResponseError,
  checkSession,
  connectionUrl,
  identity,
  type RealtimeClient,
  type RealtimeRequest,
  type RealtimeSession,
  type CancelMode,
} from "./realtime-client";
import {
  emptyRealtimeRecord,
  type RealtimeCommand,
  type RealtimeJournal,
  type RealtimeRecord,
} from "./realtime-journal";
import {
  CaptureError,
  createMicrophoneCapture,
  type MicrophoneCapture,
} from "../media/microphone-capture";
import { encodePcm } from "./pcm";

export interface RealtimeSocket extends EventTarget {
  readonly readyState: number;
  readonly bufferedAmount: number;
  send(value: string): void;
  close(): void;
}
type Dependencies = {
  socket: (url: URL) => RealtimeSocket;
  capture: typeof createMicrophoneCapture;
};
export type RealtimeSnapshot = {
  record: RealtimeRecord;
  current: RealtimeSession | null;
  stream: AsrSnapshot | null;
  busy: boolean;
  blocked: boolean;
  connecting: boolean;
  committing: boolean;
  inputDeviceId: string | null;
  microphone: "idle" | "starting" | "running" | "stopping" | "failed";
  problem:
    | "storage"
    | "unknown"
    | "rejected"
    | "read_failed"
    | "protocol"
    | "capture"
    | "capture_cleanup"
    | null;
};
const terminal = (s: RealtimeSession) => ["closed", "failed", "expired"].includes(s.status);
/** Owns one socket/device generation. A refresh never reconnects or replays microphone input. */
export class RealtimeController {
  #state: RealtimeSnapshot;
  #listeners = new Set<() => void>();
  #disposed = false;
  #active = true;
  #socket: RealtimeSocket | null = null;
  #stream: AsrStream | null = null;
  #epoch = 0;
  #read = 0;
  #reading: AbortController | null = null;
  #device: MicrophoneCapture | null = null;
  #captureAbort: AbortController | null = null;
  #captureGeneration = 0;
  #capturePending = false;
  #captureRelease: Promise<void> | null = null;
  #captureStopRequested = false;
  #captureCleanupFailed = false;
  #queue = Promise.resolve();
  #handshake: ReturnType<typeof setTimeout> | null = null;
  readonly #dependencies: Dependencies;
  constructor(
    private readonly client: Pick<RealtimeClient, "create" | "current" | "cancel">,
    private readonly journal: RealtimeJournal,
    dependencies: Partial<Dependencies> = {},
  ) {
    const record = journal.load();
    this.#state = freeze({
      record,
      current: null,
      stream: null,
      busy: false,
      blocked: false,
      connecting: false,
      committing: false,
      inputDeviceId: null,
      microphone: "idle",
      problem: record.pending ? "unknown" : null,
    });
    this.#dependencies = {
      socket: (url) => new WebSocket(url),
      capture: createMicrophoneCapture,
      ...dependencies,
    };
  }
  getSnapshot = () => this.#state;
  subscribe = (f: () => void) => {
    this.#listeners.add(f);
    return () => {
      this.#listeners.delete(f);
    };
  };
  #publish(change: Partial<RealtimeSnapshot>) {
    if (this.#disposed) return;
    this.#state = freeze({ ...this.#state, ...change });
    if (this.#captureCleanupFailed)
      this.#state = freeze({
        ...this.#state,
        microphone: "failed",
        problem: this.#state.problem === "storage" ? "storage" : "capture_cleanup",
      });
    this.#listeners.forEach((f) => f());
  }
  #persist(record: RealtimeRecord) {
    if (this.#disposed) return false;
    try {
      this.journal.save(record);
      this.#publish({ record, blocked: false });
      return true;
    } catch {
      this.#publish({ record, blocked: true, problem: "storage" });
      return false;
    }
  }
  #idle() {
    return !this.#disposed && !this.#state.busy && !this.#state.blocked;
  }
  retryStorage() {
    if (this.#disposed || this.#state.busy) return;
    if (this.#persist(this.#state.record))
      this.#publish({ problem: this.#state.record.pending ? "unknown" : null });
  }
  dismissRejected() {
    if (
      !this.#idle() ||
      this.#state.problem !== "rejected" ||
      this.#state.record.pending?.kind !== "create"
    )
      return;
    this.#persist({ ...emptyRealtimeRecord(), captions: this.#state.record.captions });
    this.#publish({ problem: null, current: null });
  }
  async create(input: RealtimeRequest) {
    if (
      !this.#idle() ||
      !this.#active ||
      this.#state.record.pending ||
      this.#socket ||
      this.#capturePending ||
      this.#device ||
      this.#captureCleanupFailed ||
      this.#state.microphone !== "idle"
    )
      return;
    if (this.#state.record.session) {
      const current = this.#state.current;
      if (
        !current ||
        !terminal(current) ||
        !["complete", "not_required"].includes(current.cleanup_state)
      )
        return;
    }
    const command: RealtimeCommand = {
      kind: "create",
      key: crypto.randomUUID(),
      body: structuredClone(input),
    };
    if (
      !this.#persist({
        ...emptyRealtimeRecord(),
        captions: this.#state.record.captions,
        request: command.body,
        pending: command,
      })
    )
      return;
    this.#stream = null;
    this.#publish({ stream: null, current: null, problem: null });
    await this.replay();
  }
  async replay() {
    const command = this.#state.record.pending;
    if (!this.#idle() || !command) return;
    // Re-check CAS/quota for EVERY dispatch, including an explicit replay after an unknown reply.
    if (!this.#persist(this.#state.record)) return;
    ++this.#read;
    this.#reading?.abort();
    this.#publish({ busy: true, current: null });
    try {
      const session =
        command.kind === "create"
          ? await this.client.create(command.body, command.key)
          : await this.client.cancel(command.sessionId, command.mode, command.key);
      if (this.#disposed) return;
      checkSession(
        session,
        command.kind === "cancel" ? command.sessionId : undefined,
        this.#state.record.request ?? undefined,
      );
      const record = { ...this.#state.record, session, pending: null };
      if (this.#persist(record))
        this.#publish({
          problem: ["capture", "protocol"].includes(this.#state.problem ?? "")
            ? this.#state.problem
            : null,
        });
    } catch (error: unknown) {
      if (!this.#disposed) {
        const rejected =
          error instanceof RealtimeResponseError &&
          error.status >= 400 &&
          error.status < 500 &&
          ![408, 429].includes(error.status);
        this.#publish({ problem: rejected ? "rejected" : "unknown" });
      }
    } finally {
      this.#publish({ busy: false });
      if (
        !this.#active &&
        !this.#disposed &&
        !this.#state.record.pending &&
        !this.#state.blocked &&
        this.#state.record.session &&
        !terminal(this.#state.record.session) &&
        this.#state.record.session.status !== "draining"
      )
        void this.close("abort");
    }
  }
  async refresh() {
    const id = this.#state.record.session?.id;
    if (this.#disposed || this.#state.busy || !id) return;
    const version = ++this.#read,
      abort = new AbortController();
    this.#reading?.abort();
    this.#reading = abort;
    this.#publish({ current: null });
    try {
      const current = await this.client.current(id, abort.signal);
      checkSession(current, id, this.#state.record.request ?? undefined);
      if (!this.#disposed && version === this.#read && id === this.#state.record.session?.id)
        this.#publish({
          current,
          problem: this.#state.problem === "read_failed" ? null : this.#state.problem,
        });
    } catch {
      if (!this.#disposed && version === this.#read)
        this.#publish({ current: null, problem: this.#state.blocked ? "storage" : "read_failed" });
    }
  }
  async connect() {
    if (
      !this.#idle() ||
      !this.#active ||
      this.#socket ||
      this.#state.record.pending ||
      !this.#state.record.session
    )
      return;
    // No reconnect of a previously consumed stream. A disconnect requires a new REST session.
    if (this.#stream) return;
    const epoch = ++this.#epoch;
    this.#publish({ connecting: true });
    await this.refresh();
    if (this.#disposed || !this.#active || epoch !== this.#epoch) return;
    const current = this.#state.current;
    if (!current || current.status !== "created" || Date.parse(current.expires_at) <= Date.now()) {
      this.#publish({ connecting: false, problem: "read_failed" });
      return;
    }
    if (!this.#persist(this.#state.record)) {
      this.#publish({ connecting: false });
      return;
    }
    try {
      const socket = this.#dependencies.socket(connectionUrl(current));
      this.#socket = socket;
      const stream = new AsrStream({ sessionId: current.id, mode: "nexus", expected: current });
      this.#stream = stream;
      this.#publish({ stream: stream.getSnapshot(), problem: null });
      const valid = () => !this.#disposed && this.#socket === socket && epoch === this.#epoch;
      socket.addEventListener("message", (event) => {
        if (!valid()) return;
        try {
          const text = (event as MessageEvent<unknown>).data;
          identity(
            typeof text === "string" &&
              new TextEncoder().encode(text).byteLength <=
                (stream.getSnapshot().limits?.max_message_bytes ?? 65536),
          );
          stream.receive(JSON.parse(text) as unknown);
          this.#publish({
            stream: stream.getSnapshot(),
            connecting: stream.getSnapshot().phase === "awaiting",
          });
          if (stream.getSnapshot().phase === "active" && this.#handshake) {
            clearTimeout(this.#handshake);
            this.#handshake = null;
          }
          this.#saveFinals();
          if (this.#state.blocked) {
            this.#drop("storage_unavailable");
            return;
          }
          if (stream.getSnapshot().phase === "closed") {
            this.#drop("session_closed");
            void this.refresh();
          }
        } catch {
          this.#publish({ problem: "protocol" });
          this.#drop("protocol_error");
          void this.close("abort");
        }
      });
      const lost = () => {
        if (valid()) {
          this.#drop("connection_lost");
          void this.close("abort");
        }
      };
      socket.addEventListener("close", lost);
      socket.addEventListener("error", lost);
      this.#handshake = setTimeout(() => {
        if (valid()) {
          this.#drop("admission_timeout");
          void this.close("abort");
        }
      }, 30000);
    } catch {
      this.#drop("connection_failed");
      this.#publish({ problem: "protocol" });
      void this.close("abort");
    }
  }
  #saveFinals() {
    const stream = this.#stream?.getSnapshot();
    if (!stream) return;
    const previous = this.#state.record.captions;
    const current = new Map(
      previous.filter((c) => c.sessionId === stream.sessionId).map((c) => [c.itemId, c]),
    );
    for (const row of stream.segments) {
      if (row.final === null || row.itemId === null) continue;
      const old = current.get(row.itemId);
      if (old) {
        identity(old.text === row.final);
        continue;
      }
      current.set(row.itemId, { sessionId: stream.sessionId, itemId: row.itemId, text: row.final });
    }
    const ordered = stream.segments.flatMap((row) => {
      if (row.itemId === null) return [];
      const caption = current.get(row.itemId);
      if (!caption) return [];
      current.delete(row.itemId);
      return [caption];
    });
    const captions = [
      ...previous.filter((c) => c.sessionId !== stream.sessionId),
      ...ordered,
      ...current.values(),
    ];
    if (captions.length !== previous.length || captions.some((c, i) => c !== previous[i]))
      this.#persist({ ...this.#state.record, captions });
  }
  async #send(value: unknown, epoch: number) {
    const run = async () => {
      const socket = this.#socket,
        stream = this.#stream;
      identity(
        !this.#disposed &&
          this.#active &&
          epoch === this.#epoch &&
          !!socket &&
          socket.readyState === 1 &&
          !!stream,
      );
      await stream.command(value, (text) => {
        identity(
          !this.#disposed &&
            this.#active &&
            epoch === this.#epoch &&
            this.#socket === socket &&
            this.#stream === stream &&
            socket.readyState === 1,
        );
        const bytes = new TextEncoder().encode(text).byteLength;
        // Five seconds of 24kHz PCM16, including Base64/envelope overhead. No unbounded send queue.
        identity(socket.bufferedAmount + bytes <= (5 * 24000 * 2 * 4) / 3);
        socket.send(text);
      });
      this.#publish({ stream: stream.getSnapshot() });
    };
    const task = this.#queue.then(run);
    this.#queue = task.catch(() => undefined);
    return task;
  }
  async selectInputDevice(inputDeviceId: string | null) {
    if (
      !this.#idle() ||
      !this.#active ||
      this.#state.record.pending ||
      this.#state.microphone === "stopping" ||
      this.#captureCleanupFailed ||
      inputDeviceId === this.#state.inputDeviceId ||
      inputDeviceId === ""
    )
      return;
    this.#publish({ inputDeviceId });
    if (this.#socket || this.#capturePending || this.#device || this.#state.connecting) {
      this.#drop("input_device_changed");
      await this.close("abort");
    }
  }
  async startCapture() {
    const stream = this.#stream?.getSnapshot();
    if (
      !this.#idle() ||
      !this.#active ||
      this.#state.microphone !== "idle" ||
      this.#capturePending ||
      this.#device ||
      this.#captureCleanupFailed ||
      stream?.phase !== "active" ||
      this.#state.record.session?.status === "draining" ||
      !stream.limits ||
      this.#state.record.pending
    )
      return;
    if (!this.#persist(this.#state.record)) return;
    const epoch = this.#epoch,
      captureGeneration = ++this.#captureGeneration,
      abort = new AbortController();
    this.#captureAbort = abort;
    this.#capturePending = true;
    this.#publish({ microphone: "starting", problem: null });
    const frameSamples = Math.min(2400, Math.floor(stream.limits.max_input_chunk_ms * 24));
    let through = 0;
    try {
      const capture = await this.#dependencies.capture({
        sampleRate: 24000,
        frameSamples,
        maxPendingFrames: Math.min(20, Math.max(2, Math.floor(24000 / frameSamples))),
        signal: abort.signal,
        ...(this.#state.inputDeviceId ? { deviceId: this.#state.inputDeviceId } : {}),
        onFrame: async (frame, signal) => {
          identity(
            !signal.aborted &&
              !abort.signal.aborted &&
              epoch === this.#epoch &&
              captureGeneration === this.#captureGeneration &&
              frame.startSample === through &&
              frame.sampleRate === 24000 &&
              frame.samples.length === frameSamples,
          );
          if (this.#device) identity(frame.token === this.#device.token);
          const deadline = performance.now() + 1000;
          while ((this.#stream?.getSnapshot().credit ?? 0) < frameSamples) {
            identity(
              !signal.aborted &&
                !abort.signal.aborted &&
                epoch === this.#epoch &&
                performance.now() < deadline,
            );
            await new Promise<void>((resolve) => setTimeout(resolve, 20));
          }
          await this.#send(
            {
              type: "input_audio_buffer.append",
              event_id: crypto.randomUUID(),
              audio: encodePcm(frame.samples),
            },
            epoch,
          );
          through += frame.samples.length;
        },
      });
      this.#ownCapture(capture);
      if (
        this.#disposed ||
        abort.signal.aborted ||
        captureGeneration !== this.#captureGeneration ||
        epoch !== this.#epoch
      ) {
        await this.#stopDevice();
        return;
      }
      this.#publish({ microphone: "running" });
    } catch (error) {
      if (error instanceof CaptureError && error.code === "capture_cleanup_failed") {
        this.#captureReleaseFailed();
      } else if (!this.#disposed && captureGeneration === this.#captureGeneration) {
        this.#publish({ microphone: "idle", problem: "capture" });
        this.#drop("capture_failed");
        void this.close("abort");
      }
    } finally {
      this.#capturePending = false;
      if (!this.#device && this.#captureAbort === abort) this.#captureAbort = null;
      if (!this.#device && !this.#captureCleanupFailed) this.#publish({ microphone: "idle" });
    }
  }
  #ownCapture(capture: MicrophoneCapture) {
    this.#device = capture;
    this.#captureStopRequested = false;
    void capture.stopping.then(() => {
      if (this.#device !== capture) return;
      this.#publish({ microphone: "stopping" });
      if (!this.#captureStopRequested) {
        this.#publish({ problem: "capture" });
        this.#drop("capture_interrupted");
        void this.close("abort");
      }
    });
    this.#captureRelease = capture.closed.then(
      () => {
        if (this.#device !== capture) return;
        const interrupted =
          !this.#captureStopRequested &&
          (capture.snapshot.reason || capture.snapshot.state === "failed");
        this.#device = null;
        this.#captureAbort = null;
        this.#captureRelease = null;
        this.#publish({ microphone: "idle" });
        if (interrupted) {
          this.#publish({ problem: "capture" });
          this.#drop("capture_interrupted");
          void this.close("abort");
        }
      },
      () => {
        if (this.#device === capture) this.#captureReleaseFailed();
      },
    );
  }
  #captureReleaseFailed() {
    if (this.#captureCleanupFailed) return;
    this.#captureCleanupFailed = true;
    this.#publish({ microphone: "failed", problem: "capture_cleanup" });
    this.#drop("capture_cleanup_failed");
    const current = this.#state.current;
    if (
      current &&
      terminal(current) &&
      ["complete", "not_required"].includes(current.cleanup_state)
    )
      return;
    void this.close("abort");
  }
  async commit() {
    if (
      !this.#idle() ||
      this.#state.committing ||
      this.#state.stream?.phase !== "active" ||
      !this.#state.stream.uncommitted
    )
      return;
    this.#publish({ committing: true });
    try {
      await this.#send(
        { type: "input_audio_buffer.commit", event_id: crypto.randomUUID() },
        this.#epoch,
      );
    } catch (error: unknown) {
      if (error instanceof AsrProtocolError && error.code === "empty_audio_buffer") return;
      this.#publish({ problem: "protocol" });
      this.#drop("commit_failed");
      void this.close("abort");
    } finally {
      this.#publish({ committing: false });
    }
  }
  #drop(reason: string) {
    ++this.#epoch;
    void this.#stopDevice();
    if (this.#handshake) {
      clearTimeout(this.#handshake);
      this.#handshake = null;
    }
    const socket = this.#socket;
    this.#socket = null;
    if (socket)
      try {
        socket.close();
      } catch {
        /* Cleanup remains unknown until GET. */
      }
    this.#stream?.disconnect(reason);
    this.#publish({
      connecting: false,
      stream: this.#stream?.getSnapshot() ?? null,
    });
  }
  #stopDevice() {
    ++this.#captureGeneration;
    const requested = this.#captureStopRequested;
    this.#captureStopRequested = true;
    this.#captureAbort?.abort();
    const device = this.#device;
    if (!this.#captureCleanupFailed) {
      this.#publish({ microphone: device || this.#capturePending ? "stopping" : "idle" });
      if (device && !requested) {
        try {
          void device.stop().catch(() => this.#captureReleaseFailed());
        } catch {
          this.#captureReleaseFailed();
        }
      }
    }
    return this.#captureRelease ?? Promise.resolve();
  }
  async close(mode: CancelMode) {
    if (mode === "abort") this.#drop("abort_requested");
    else void this.#stopDevice();
    const session = this.#state.record.session;
    const current = this.#state.current;
    if (
      !this.#idle() ||
      !session ||
      this.#state.record.pending ||
      terminal(session) ||
      (current &&
        terminal(current) &&
        ["complete", "not_required"].includes(current.cleanup_state)) ||
      (session.status === "draining" && mode === "drain")
    )
      return;
    const pending: RealtimeCommand = {
      kind: "cancel",
      key: crypto.randomUUID(),
      sessionId: session.id,
      mode,
    };
    if (!this.#persist({ ...this.#state.record, pending })) return;
    if (mode === "drain") {
      this.#stream?.drain();
      this.#publish({ stream: this.#stream?.getSnapshot() ?? null });
    }
    await this.replay();
  }
  setActive(active: boolean) {
    if (this.#disposed) return;
    this.#active = active;
    if (!active) {
      this.#drop("navigation_gap");
      void this.close("abort");
    }
  }
  suspend() {
    if (this.#disposed) return;
    this.#drop("page_hidden_gap");
    void this.close("abort");
  }
  dispose() {
    if (this.#disposed) return;
    this.#reading?.abort();
    ++this.#read;
    this.#drop("host_disposed");
    this.#disposed = true;
    this.#listeners.clear();
  }
}
