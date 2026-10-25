import { afterEach, describe, expect, it, vi } from "vitest";

import { createWorkletPlayback } from "./worklet-playback";

class FakeNode {
  static instances: FakeNode[] = [];
  readonly port = { postMessage: vi.fn(), close: vi.fn(), onmessage: null as unknown };
  readonly connect = vi.fn();
  readonly disconnect = vi.fn();
  onprocessorerror: (() => void) | null = null;

  constructor() {
    FakeNode.instances.push(this);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeNode.instances = [];
});

function fixture() {
  vi.stubGlobal("AudioWorkletNode", FakeNode);
  const addModule = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
  const context = {
    sampleRate: 24000,
    destination: {},
    audioWorklet: { addModule },
  } as unknown as BaseAudioContext;
  const options = {
    identity: { epoch: 1, generation: 1 },
    capacitySamples: 512,
    pcmSampleRate: 24000,
  };
  return { addModule, context, options };
}

describe("worklet lifecycle", () => {
  it("rejects mismatched clocks and pre-aborted requests without allocating nodes", async () => {
    const { addModule, context, options } = fixture();
    await expect(
      createWorkletPlayback(context, { ...options, pcmSampleRate: 48000 }),
    ).rejects.toThrow("sample clocks differ");
    const abort = new AbortController();
    abort.abort();
    await expect(
      createWorkletPlayback(context, { ...options, signal: abort.signal }),
    ).rejects.toThrow();
    expect(addModule).not.toHaveBeenCalled();
    expect(FakeNode.instances).toHaveLength(0);
  });

  it("aborts while loading without leaving a late node connected", async () => {
    const { addModule, context, options } = fixture();
    let finish!: () => void;
    addModule.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    const abort = new AbortController();
    const loading = createWorkletPlayback(context, { ...options, signal: abort.signal });
    abort.abort();
    finish();
    await expect(loading).rejects.toThrow();
    expect(FakeNode.instances).toHaveLength(0);
  });

  it("settles an aborted start promptly even if module loading stays pending", async () => {
    const { addModule, context, options } = fixture();
    let finish!: () => void;
    addModule.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    const abort = new AbortController();
    const loading = createWorkletPlayback(context, { ...options, signal: abort.signal });
    const outcome = loading.then(
      () => "resolved",
      () => "aborted",
    );
    abort.abort();
    const result = await Promise.race([
      outcome,
      new Promise<string>((resolve) => setTimeout(() => resolve("still pending"), 25)),
    ]);
    finish();
    await outcome;
    expect(result).toBe("aborted");
    expect(FakeNode.instances).toHaveLength(0);
  });

  it("stop and abort disconnect the graph, close its port and remove handlers once", async () => {
    const { context, options } = fixture();
    const abort = new AbortController();
    const playback = await createWorkletPlayback(context, { ...options, signal: abort.signal });
    const node = FakeNode.instances[0]!;
    abort.abort();
    playback.stop();
    expect(node.disconnect).toHaveBeenCalledTimes(1);
    expect(node.port.close).toHaveBeenCalledTimes(1);
    expect(node.port.onmessage).toBeNull();
    expect(node.onprocessorerror).toBeNull();
  });

  it("reuses the module while replacing the previous playback generation", async () => {
    const { addModule, context, options } = fixture();
    const first = await createWorkletPlayback(context, options);
    const second = await createWorkletPlayback(context, {
      ...options,
      identity: { epoch: 2, generation: 2 },
    });
    expect(first.snapshot.state).toBe("stopped");
    expect(FakeNode.instances[0]?.disconnect).toHaveBeenCalledTimes(1);
    expect(second.snapshot.state).toBe("running");
    expect(addModule).toHaveBeenCalledTimes(1);
    second.stop();
  });

  it("keeps a pending module request cached after an aborted start", async () => {
    const { addModule, context, options } = fixture();
    let finish!: () => void;
    addModule.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    const abort = new AbortController();
    const first = createWorkletPlayback(context, { ...options, signal: abort.signal });
    abort.abort();
    await expect(first).rejects.toThrow();
    const second = createWorkletPlayback(context, {
      ...options,
      identity: { epoch: 1, generation: 2 },
    });
    expect(addModule).toHaveBeenCalledTimes(1);
    finish();
    (await second).stop();
  });

  it("allows a new generation to retry after a module load failure", async () => {
    const { addModule, context, options } = fixture();
    addModule.mockRejectedValueOnce(new Error("load failed"));
    await expect(createWorkletPlayback(context, options)).rejects.toThrow("load failed");
    const next = await createWorkletPlayback(context, {
      ...options,
      identity: { epoch: 1, generation: 2 },
    });
    expect(addModule).toHaveBeenCalledTimes(2);
    next.stop();
  });

  it("supersedes an in-progress start so only the newest generation can connect", async () => {
    const { addModule, context, options } = fixture();
    let finish!: () => void;
    addModule.mockReturnValue(
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
    );
    const first = createWorkletPlayback(context, options);
    const second = createWorkletPlayback(context, {
      ...options,
      identity: { epoch: 2, generation: 2 },
    });
    const rejected = expect(first).rejects.toThrow("superseded");
    finish();
    await rejected;
    const current = await second;
    expect(FakeNode.instances).toHaveLength(1);
    current.stop();
  });

  it("rejects an old generation instead of reconnecting cancelled audio", async () => {
    const { context, options } = fixture();
    const playback = await createWorkletPlayback(context, options);
    playback.stop();
    await expect(createWorkletPlayback(context, options)).rejects.toThrow(
      "generation must advance",
    );
    expect(FakeNode.instances).toHaveLength(1);
  });

  it("releases resources if the processor fails", async () => {
    const { context, options } = fixture();
    const playback = await createWorkletPlayback(context, options);
    FakeNode.instances[0]?.onprocessorerror?.();
    expect(playback.snapshot.state).toBe("failed");
    expect(FakeNode.instances[0]?.port.close).toHaveBeenCalledTimes(1);
  });
});
