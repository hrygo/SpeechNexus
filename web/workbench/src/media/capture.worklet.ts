import { CAPTURE_PROCESSOR_NAME, CaptureBuffer, type CaptureOptions } from "./capture-buffer";

declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  constructor();
}
declare function registerProcessor(
  name: string,
  processor: new (options: AudioWorkletNodeOptions) => AudioWorkletProcessor,
): void;

class PcmCaptureProcessor extends AudioWorkletProcessor {
  readonly #buffer: CaptureBuffer;

  constructor(options: AudioWorkletNodeOptions) {
    super();
    const config = options.processorOptions as CaptureOptions;
    const token = config.token;
    this.#buffer = new CaptureBuffer(
      config,
      (frame) => this.port.postMessage({ kind: "pcm", ...frame }, [frame.samples.buffer]),
      (reason) => this.port.postMessage({ kind: "failed", token, reason }),
    );
    this.port.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (typeof data !== "object" || data === null)
        return this.#buffer.reject("capture_protocol_error");
      const value = data as Record<string, unknown>;
      if (typeof value.token !== "string") return this.#buffer.reject("capture_protocol_error");
      if (value.token !== token) return;
      if (value.kind === "stop") return this.#buffer.stop();
      if (value.kind !== "ack" || typeof value.throughSample !== "number")
        return this.#buffer.reject("capture_protocol_error");
      this.#buffer.acknowledge(value.throughSample);
    };
  }

  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    // A silent output keeps this graph pulled without monitoring the microphone.
    for (const channel of outputs[0] ?? []) channel.fill(0);
    const input = inputs[0]?.[0];
    if (input) this.#buffer.push(input);
    return !this.#buffer.stopped;
  }
}

registerProcessor(CAPTURE_PROCESSOR_NAME, PcmCaptureProcessor);
