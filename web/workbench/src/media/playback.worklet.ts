import {
  isIdentity,
  isSampleIndex,
  PlaybackBuffer,
  sameIdentity,
  type PlaybackIdentity,
  type PlaybackOptions,
} from "./playback-buffer";
import { PLAYBACK_PROCESSOR_NAME } from "./playback-channel";

// TypeScript's DOM library covers the node but omits the rendering-thread globals.
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  constructor();
}
declare const sampleRate: number;
declare function registerProcessor(
  name: string,
  processor: new (options: AudioWorkletNodeOptions) => AudioWorkletProcessor,
): void;

class PcmPlaybackProcessor extends AudioWorkletProcessor {
  readonly #buffer: PlaybackBuffer;
  readonly #identity: PlaybackIdentity;
  #lastReported: number;

  constructor(options: AudioWorkletNodeOptions) {
    super();
    const config = options.processorOptions as PlaybackOptions;
    this.#buffer = new PlaybackBuffer(config);
    this.#identity = { ...config.identity };
    this.#lastReported = config.startSample ?? 0;
    this.port.onmessage = (event: MessageEvent<unknown>) => this.#receive(event.data);
  }

  #receive(message: unknown) {
    if (this.#buffer.stopped) return;
    if (typeof message !== "object" || message === null) return this.#reject();
    const value = message as Record<string, unknown>;
    if (!isIdentity(value.identity)) return this.#reject();
    if (!sameIdentity(value.identity, this.#identity)) return;
    if (value.kind === "stop") return this.#buffer.stop();
    if (
      value.kind !== "pcm" ||
      !isSampleIndex(value.pushId) ||
      value.pushId === 0 ||
      !isSampleIndex(value.startSample) ||
      !(value.samples instanceof Float32Array)
    )
      return this.#reject();
    try {
      const result = this.#buffer.push(value.identity, value.startSample, value.samples);
      if (result !== "accepted") return this.#reject();
      this.port.postMessage({ kind: "received", identity: this.#identity, pushId: value.pushId });
    } catch {
      this.#reject();
    }
  }

  #reject() {
    this.#buffer.stop();
    this.port.postMessage({
      kind: "rejected",
      identity: this.#identity,
      reason: "invalid_pcm_message",
    });
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]) {
    const output = outputs[0]?.[0];
    if (!output) return false;
    this.#buffer.renderInto(output);
    // Read primitive counters here; do not allocate a snapshot on each render quantum.
    const renderedThrough = this.#buffer.renderedThrough;
    if (
      renderedThrough > this.#lastReported &&
      (this.#buffer.bufferedSamples === 0 ||
        renderedThrough - this.#lastReported >= sampleRate / 10)
    ) {
      this.#lastReported = renderedThrough;
      this.port.postMessage({
        kind: "consumed",
        identity: this.#identity,
        throughSample: renderedThrough,
      });
    }
    return !this.#buffer.stopped;
  }
}

registerProcessor(PLAYBACK_PROCESSOR_NAME, PcmPlaybackProcessor);
