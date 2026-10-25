import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

const identity = { epoch: 1, generation: 1 };

class FakeProcessor {
  readonly port = {
    onmessage: null as ((event: MessageEvent<unknown>) => void) | null,
    postMessage: vi.fn<(message: unknown) => void>(),
  };
}

interface Processor extends FakeProcessor {
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
}

let ProcessorClass: new (options: AudioWorkletNodeOptions) => Processor;

beforeAll(async () => {
  vi.stubGlobal("AudioWorkletProcessor", FakeProcessor);
  vi.stubGlobal("sampleRate", 24000);
  vi.stubGlobal("registerProcessor", (_name: string, processor: typeof ProcessorClass) => {
    ProcessorClass = processor;
  });
  // Exercise the actual rendering-thread module with only its native host globals replaced.
  await import("./playback.worklet");
});

afterAll(() => vi.unstubAllGlobals());

function fixture(startSample = 0, capacitySamples = 8192) {
  const processor = new ProcessorClass({
    processorOptions: { identity, capacitySamples, startSample },
  });
  const send = (data: unknown) => processor.port.onmessage?.({ data } as MessageEvent<unknown>);
  const render = (length = 128) => {
    const output = new Float32Array(length).fill(1);
    const running = processor.process([], [[output]]);
    return { output: Array.from(output), running };
  };
  const pcm = (samples: Float32Array, start = startSample) => ({
    kind: "pcm",
    identity,
    pushId: 1,
    startSample: start,
    samples,
  });
  return { processor, send, render, pcm };
}

describe("playback processor", () => {
  it("renders exact PCM and reports consumed source samples without counting underrun silence", () => {
    const { processor, send, render, pcm } = fixture(100);
    send(pcm(new Float32Array([0.25, -0.5])));
    expect(render(4)).toEqual({ output: [0.25, -0.5, 0, 0], running: true });
    expect(processor.port.postMessage.mock.calls.map(([message]) => message)).toEqual([
      { kind: "received", identity, pushId: 1 },
      { kind: "consumed", identity, throughSample: 102 },
    ]);
    expect(render(4).output).toEqual([0, 0, 0, 0]);
    expect(processor.port.postMessage).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["null", null],
    ["missing identity", { identity: undefined }],
    ["unknown kind", { kind: "unknown", identity }],
    ["non-float PCM", { samples: [0.5] }],
    ["empty PCM", { samples: new Float32Array(0) }],
    ["non-finite PCM", { samples: new Float32Array([NaN]) }],
    ["oversized PCM", { samples: new Float32Array(8193) }],
    ["gap", { startSample: 10 }],
    ["invalid push id", { pushId: 0 }],
  ])("flushes and stops on %s", (_name, invalid) => {
    const { processor, send, render, pcm } = fixture();
    send(pcm(new Float32Array([0.5])));
    // Seed a valid queue first so rejection also proves queued audio is erased.
    const message = invalid === null ? null : { ...pcm(new Float32Array([0.5]), 1), ...invalid };
    send(message);
    expect(render(4)).toEqual({ output: [0, 0, 0, 0], running: false });
    expect(processor.port.postMessage).toHaveBeenLastCalledWith({
      kind: "rejected",
      identity,
      reason: "invalid_pcm_message",
    });
    send(pcm(new Float32Array([0.5])));
    expect(render(4).output).toEqual([0, 0, 0, 0]);
  });

  it("ignores stale generation messages and stops only for its current identity", () => {
    const { send, render, pcm } = fixture();
    send({ ...pcm(new Float32Array([0.5])), identity: { epoch: 1, generation: 2 } });
    send({ kind: "stop", identity: { epoch: 2, generation: 2 } });
    expect(render(4)).toEqual({ output: [0, 0, 0, 0], running: true });
    send(pcm(new Float32Array([0.25])));
    send({ kind: "stop", identity });
    expect(render(4)).toEqual({ output: [0, 0, 0, 0], running: false });
  });

  it("rejects capacity overflow instead of rendering a partially admitted block", () => {
    const { processor, send, render, pcm } = fixture(0, 2);
    send(pcm(new Float32Array([0.25, 0.5])));
    send({ ...pcm(new Float32Array([0.75]), 2), pushId: 2 });
    expect(render(4)).toEqual({ output: [0, 0, 0, 0], running: false });
    expect(processor.port.postMessage).toHaveBeenLastCalledWith({
      kind: "rejected",
      identity,
      reason: "invalid_pcm_message",
    });
  });
});
