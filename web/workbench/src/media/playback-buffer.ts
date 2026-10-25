/** Internal sample-clock primitives. Public Realtime DTOs are mapped outside this module. */
export interface PlaybackIdentity {
  readonly epoch: number;
  readonly generation: number;
}

export interface PlaybackOptions {
  readonly identity: PlaybackIdentity;
  readonly capacitySamples: number;
  readonly startSample?: number;
}

export type PushResult = "accepted" | "backpressure" | "gap" | "stale" | "stopped";

// Application memory policy, independent of any negotiated server credit.
export const MAX_BUFFER_SAMPLES = 1_048_576;
export const MAX_CHUNK_SAMPLES = 8192;

export function isSampleIndex(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function isIdentity(value: unknown): value is PlaybackIdentity {
  if (typeof value !== "object" || value === null) return false;
  const token = value as Record<string, unknown>;
  return (
    isSampleIndex(token.epoch) &&
    token.epoch > 0 &&
    isSampleIndex(token.generation) &&
    token.generation > 0
  );
}

export function sameIdentity(left: PlaybackIdentity, right: PlaybackIdentity) {
  return left.epoch === right.epoch && left.generation === right.generation;
}

export function validateOptions(options: PlaybackOptions) {
  if (!isIdentity(options.identity) || !isSampleIndex(options.startSample ?? 0)) {
    throw new RangeError("invalid playback identity or sample cursor");
  }
  if (
    !Number.isSafeInteger(options.capacitySamples) ||
    options.capacitySamples < 1 ||
    options.capacitySamples > MAX_BUFFER_SAMPLES
  ) {
    throw new RangeError("playback capacity outside local memory policy");
  }
}

export function validateSamples(startSample: number, samples: Float32Array) {
  if (
    !isSampleIndex(startSample) ||
    !isSampleIndex(startSample + samples.length) ||
    samples.length === 0 ||
    samples.length > MAX_CHUNK_SAMPLES
  ) {
    throw new RangeError("invalid PCM sample span");
  }
  for (const sample of samples) {
    if (!Number.isFinite(sample) || Math.abs(sample) > 1)
      throw new RangeError("invalid normalized PCM");
  }
}

export class PlaybackBuffer {
  readonly #identity: PlaybackIdentity;
  readonly #samples: Float32Array;
  #readPosition = 0;
  #size = 0;
  #acceptedThrough: number;
  #renderedThrough: number;
  #stopped = false;

  constructor(options: PlaybackOptions) {
    validateOptions(options);
    this.#identity = { ...options.identity };
    this.#samples = new Float32Array(options.capacitySamples);
    this.#acceptedThrough = this.#renderedThrough = options.startSample ?? 0;
  }

  get snapshot() {
    return {
      bufferedSamples: this.#size,
      acceptedThrough: this.#acceptedThrough,
      renderedThrough: this.#renderedThrough,
      stopped: this.#stopped,
    };
  }

  get stopped() {
    return this.#stopped;
  }

  get bufferedSamples() {
    return this.#size;
  }

  get renderedThrough() {
    return this.#renderedThrough;
  }

  push(identity: PlaybackIdentity, startSample: number, samples: Float32Array): PushResult {
    if (this.#stopped) return "stopped";
    if (!sameIdentity(identity, this.#identity)) return "stale";
    validateSamples(startSample, samples);
    if (startSample !== this.#acceptedThrough) return "gap";
    if (samples.length > this.#samples.length - this.#size) return "backpressure";
    const writePosition = (this.#readPosition + this.#size) % this.#samples.length;
    const first = Math.min(samples.length, this.#samples.length - writePosition);
    this.#samples.set(samples.subarray(0, first), writePosition);
    this.#samples.set(samples.subarray(first), 0);
    this.#size += samples.length;
    this.#acceptedThrough += samples.length;
    return "accepted";
  }

  /** Count real samples rendered; silence on underrun never advances their watermark. */
  renderInto(output: Float32Array) {
    output.fill(0);
    const count = Math.min(output.length, this.#size);
    for (let index = 0; index < count; index += 1) {
      output[index] = this.#samples[this.#readPosition]!;
      this.#samples[this.#readPosition] = 0;
      this.#readPosition = (this.#readPosition + 1) % this.#samples.length;
    }
    this.#size -= count;
    this.#renderedThrough += count;
    return count;
  }

  stop() {
    this.#stopped = true;
    this.#samples.fill(0);
    this.#size = 0;
  }
}
