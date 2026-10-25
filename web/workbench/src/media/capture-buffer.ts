/** Local rendering-thread transport. These are not public Realtime DTOs or credits. */
export interface CaptureOptions {
  readonly token: string;
  readonly frameSamples: number;
  readonly maxPendingFrames: number;
}

export interface CaptureFrame {
  readonly token: string;
  readonly startSample: number;
  readonly samples: Float32Array;
}

export type CaptureBufferFailure =
  "capture_overrun" | "capture_protocol_error" | "capture_invalid_pcm";

export const CAPTURE_PROCESSOR_NAME = "speechnexus-pcm-capture";

export function validateCaptureOptions(options: CaptureOptions): void {
  if (
    typeof options.token !== "string" ||
    !options.token.length ||
    options.token.length > 128 ||
    !Number.isSafeInteger(options.frameSamples) ||
    options.frameSamples < 1 ||
    options.frameSamples > 8192 ||
    !Number.isSafeInteger(options.maxPendingFrames) ||
    options.maxPendingFrames < 1 ||
    options.maxPendingFrames > 64
  )
    throw new RangeError("invalid local capture limits");
}

export class CaptureBuffer {
  readonly #options: CaptureOptions;
  readonly #emit: (frame: CaptureFrame) => void;
  readonly #fail: (reason: CaptureBufferFailure) => void;
  #frame: Float32Array;
  #used = 0;
  #sent = 0;
  #acknowledged = 0;
  #stopped = false;

  constructor(
    options: CaptureOptions,
    emit: (frame: CaptureFrame) => void,
    fail: (reason: CaptureBufferFailure) => void,
  ) {
    validateCaptureOptions(options);
    this.#options = { ...options };
    this.#frame = new Float32Array(options.frameSamples);
    this.#emit = emit;
    this.#fail = fail;
  }

  get stopped(): boolean {
    return this.#stopped;
  }

  stop(): void {
    this.#stopped = true;
    this.#frame.fill(0);
    this.#used = 0;
  }

  reject(reason: CaptureBufferFailure): void {
    if (this.#stopped) return;
    this.stop();
    this.#fail(reason);
  }

  acknowledge(throughSample: number): void {
    if (this.#stopped) return;
    if (
      !Number.isSafeInteger(throughSample) ||
      throughSample < 0 ||
      throughSample > this.#sent ||
      throughSample % this.#options.frameSamples !== 0
    ) {
      return this.reject("capture_protocol_error");
    }
    this.#acknowledged = Math.max(this.#acknowledged, throughSample);
  }

  push(input: Float32Array): void {
    if (this.#stopped) return;
    const { frameSamples, maxPendingFrames, token } = this.#options;
    for (const sample of input) {
      if (this.#sent - this.#acknowledged >= frameSamples * maxPendingFrames) {
        return this.reject("capture_overrun");
      }
      if (!Number.isFinite(sample) || !Number.isSafeInteger(this.#sent + frameSamples)) {
        return this.reject("capture_invalid_pcm");
      }
      this.#frame[this.#used++] = sample;
      if (this.#used === frameSamples) {
        const frame = { token, startSample: this.#sent, samples: this.#frame };
        this.#frame = new Float32Array(frameSamples);
        this.#used = 0;
        this.#sent += frameSamples;
        this.#emit(frame);
      }
    }
  }
}
