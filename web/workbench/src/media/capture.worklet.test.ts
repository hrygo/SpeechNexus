import { afterAll, beforeAll, expect, it, vi } from "vitest";

class Host {
  readonly port = {
    onmessage: null as ((event: MessageEvent<unknown>) => void) | null,
    postMessage: vi.fn<(value: unknown, transfer?: Transferable[]) => void>(),
  };
}
interface Processor extends Host {
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
}
let ProcessorClass: new (options: AudioWorkletNodeOptions) => Processor;
beforeAll(async () => {
  vi.stubGlobal("AudioWorkletProcessor", Host);
  vi.stubGlobal("registerProcessor", (_name: string, value: typeof ProcessorClass) => {
    ProcessorClass = value;
  });
  await import("./capture.worklet");
});
afterAll(() => vi.unstubAllGlobals());

function fixture() {
  const processor = new ProcessorClass({
    processorOptions: { token: "a", frameSamples: 4, maxPendingFrames: 1 },
  });
  const send = (data: unknown) => processor.port.onmessage?.({ data } as MessageEvent<unknown>);
  const output = new Float32Array(4).fill(1);
  const render = (samples = new Float32Array(4)) => processor.process([[samples]], [[output]]);
  return { processor, send, output, render };
}

it("transfers exact PCM while producing silence and ignores old tokens", () => {
  const { processor, send, output, render } = fixture();
  render(new Float32Array([0.25, -0.5, 0.75, -1]));
  expect(Array.from(output)).toEqual([0, 0, 0, 0]);
  const frame = {
    kind: "pcm",
    token: "a",
    startSample: 0,
    samples: new Float32Array([0.25, -0.5, 0.75, -1]),
  };
  expect(processor.port.postMessage).toHaveBeenCalledWith(frame, [frame.samples.buffer]);
  send({ kind: "stop", token: "old" });
  send({ kind: "ack", token: "a", throughSample: 4 });
  expect(render()).toBe(true);
  send({ kind: "stop", token: "a" });
  expect(render()).toBe(false);
});

it("sends one overrun failure instead of an unbounded MessagePort queue", () => {
  const { processor, render } = fixture();
  render();
  expect(render()).toBe(false);
  expect(render()).toBe(false);
  expect(processor.port.postMessage).toHaveBeenCalledTimes(2);
  expect(processor.port.postMessage).toHaveBeenLastCalledWith({
    kind: "failed",
    token: "a",
    reason: "capture_overrun",
  });
});

it.each([null, { token: "a", kind: "unknown" }, { token: "a", kind: "ack", throughSample: "4" }])(
  "rejects malformed current control %j",
  (message) => {
    const { processor, send, render } = fixture();
    send(message);
    expect(render()).toBe(false);
    expect(processor.port.postMessage).toHaveBeenLastCalledWith({
      kind: "failed",
      token: "a",
      reason: "capture_protocol_error",
    });
  },
);
