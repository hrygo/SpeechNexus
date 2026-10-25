import {
  isIdentity,
  isSampleIndex,
  sameIdentity,
  validateOptions,
  validateSamples,
  type PlaybackIdentity,
  type PlaybackOptions,
  type PushResult,
} from "./playback-buffer";

export const PLAYBACK_PROCESSOR_NAME = "speechnexus-pcm-playback";

/** Private MessagePort commands; these are not public speech or execution DTOs. */
export type ProcessorCommand =
  | {
      kind: "pcm";
      identity: PlaybackIdentity;
      pushId: number;
      startSample: number;
      samples: Float32Array<ArrayBuffer>;
    }
  | { kind: "stop"; identity: PlaybackIdentity };

export interface PlaybackTransport {
  send(message: ProcessorCommand, transfer: Transferable[]): void;
  /** Must disconnect the audio graph and close its port; does not stop server compute. */
  close(): void;
}

export class PlaybackChannel {
  readonly #transport: PlaybackTransport;
  readonly #identity: PlaybackIdentity;
  readonly #capacity: number;
  readonly #maxMessages: number;
  readonly #pending = new Set<number>();
  #nextPushId = 1;
  #sentThrough: number;
  #renderedThrough: number;
  #state: "running" | "stopped" | "failed" = "running";

  constructor(
    transport: PlaybackTransport,
    options: PlaybackOptions & { maxInFlightMessages?: number },
  ) {
    validateOptions(options);
    this.#transport = transport;
    this.#identity = { ...options.identity };
    this.#capacity = options.capacitySamples;
    this.#maxMessages = options.maxInFlightMessages ?? 64;
    if (
      !Number.isSafeInteger(this.#maxMessages) ||
      this.#maxMessages < 1 ||
      this.#maxMessages > 64
    ) {
      throw new RangeError("playback message budget outside local memory policy");
    }
    this.#sentThrough = this.#renderedThrough = options.startSample ?? 0;
  }

  get snapshot() {
    return {
      state: this.#state,
      sentThrough: this.#sentThrough,
      renderedThrough: this.#renderedThrough,
      availableSamples: this.#capacity - (this.#sentThrough - this.#renderedThrough),
      inFlightMessages: this.#pending.size,
    };
  }

  push(identity: PlaybackIdentity, startSample: number, samples: Float32Array): PushResult {
    if (this.#state !== "running") return "stopped";
    if (!sameIdentity(identity, this.#identity)) return "stale";
    validateSamples(startSample, samples);
    if (startSample !== this.#sentThrough) return "gap";
    if (samples.length > this.snapshot.availableSamples || this.#pending.size >= this.#maxMessages)
      return "backpressure";
    if (!Number.isSafeInteger(this.#nextPushId + 1)) {
      this.fail();
      throw new RangeError("playback message counter exhausted");
    }
    const copied = new Float32Array(samples);
    const pushId = this.#nextPushId++;
    this.#pending.add(pushId);
    this.#sentThrough += copied.length;
    try {
      this.#transport.send(
        { kind: "pcm", identity: this.#identity, pushId, startSample, samples: copied },
        [copied.buffer],
      );
    } catch {
      this.fail();
      throw new Error("playback transport failed");
    }
    return "accepted";
  }

  receive(message: unknown) {
    if (this.#state !== "running" || typeof message !== "object" || message === null) return;
    const value = message as Record<string, unknown>;
    if (!isIdentity(value.identity) || !sameIdentity(value.identity, this.#identity)) return;
    if (value.kind === "received" && isSampleIndex(value.pushId))
      this.#pending.delete(value.pushId);
    if (value.kind === "rejected") this.fail();
    if (value.kind === "consumed") {
      if (!isSampleIndex(value.throughSample) || value.throughSample > this.#sentThrough) {
        this.fail();
      } else if (value.throughSample > this.#renderedThrough) {
        this.#renderedThrough = value.throughSample;
      }
    }
  }

  /** Local playback failure only; server execution cleanup remains a separate fact. */
  fail() {
    this.#close("failed");
  }

  stop() {
    this.#close("stopped");
  }

  #close(state: "failed" | "stopped") {
    if (this.#state !== "running") return;
    this.#state = state;
    this.#pending.clear();
    try {
      this.#transport.send({ kind: "stop", identity: this.#identity }, []);
    } catch {
      // Closing the local graph must still happen when the port has already failed.
    } finally {
      this.#transport.close();
    }
  }
}
