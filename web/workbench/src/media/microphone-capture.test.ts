import { afterEach, describe, expect, it, vi } from "vitest";

import type { CaptureOptions } from "./capture-buffer";
import { createMicrophoneCapture, type MicrophoneFrame } from "./microphone-capture";

class Track extends EventTarget {
  readyState = "live";
  readonly stop = vi.fn(() => {
    this.readyState = "ended";
  });
}
class Context extends EventTarget {
  static instances: Context[] = [];
  static loading: Promise<void> = Promise.resolve();
  static resuming: Promise<void> = Promise.resolve();
  sampleRate: number;
  state = "running";
  destination = {};
  audioWorklet = { addModule: vi.fn(() => Context.loading) };
  source = { connect: vi.fn(), disconnect: vi.fn() };
  createMediaStreamSource = vi.fn(() => this.source);
  resume = vi.fn(() => Context.resuming);
  close = vi.fn(() => {
    this.state = "closed";
    return Promise.resolve();
  });
  constructor(options: AudioContextOptions) {
    super();
    this.sampleRate = options.sampleRate!;
    Context.instances.push(this);
  }
}
class Node extends EventTarget {
  static instances: Node[] = [];
  port = {
    onmessage: null as ((event: MessageEvent<unknown>) => void) | null,
    postMessage:
      vi.fn<(message: { kind: string; token: string; throughSample?: number }) => void>(),
    close: vi.fn(),
  };
  connect = vi.fn();
  disconnect = vi.fn();
  onprocessorerror: (() => void) | null = null;
  constructor(
    readonly context: unknown,
    readonly name: string,
    readonly options: AudioWorkletNodeOptions,
  ) {
    super();
    Node.instances.push(this);
  }
  send(data: unknown) {
    this.port.onmessage?.({ data } as MessageEvent<unknown>);
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function fixture() {
  vi.stubGlobal("AudioContext", Context);
  vi.stubGlobal("AudioWorkletNode", Node);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const track = new Track();
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  const getUserMedia = vi.fn<() => Promise<unknown>>().mockResolvedValue(stream);
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  const options = {
    sampleRate: 24000,
    frameSamples: 4,
    maxPendingFrames: 2,
    onFrame: vi.fn<(frame: MicrophoneFrame, signal: AbortSignal) => void | Promise<void>>(),
  };
  return { track, stream, getUserMedia, options };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  Context.instances = [];
  Context.loading = Promise.resolve();
  Context.resuming = Promise.resolve();
  Node.instances = [];
});

function pcm(
  node: Node,
  startSample = 0,
  token = (node.options.processorOptions as CaptureOptions).token,
) {
  return { kind: "pcm", token, startSample, samples: new Float32Array(4) };
}

describe("owned microphone lifecycle", () => {
  it("owns tracks/context, requests audio only and stops every resource once", async () => {
    const { track, getUserMedia, options } = fixture();
    const session = await createMicrophoneCapture(options);
    const node = Node.instances[0]!;
    expect(getUserMedia).toHaveBeenCalledWith(expect.objectContaining({ video: false }));
    expect(node.options).toMatchObject({
      channelCount: 1,
      channelCountMode: "explicit",
      outputChannelCount: [1],
    });
    await session.stop();
    await session.stop();
    expect(session.snapshot.state).toBe("stopped");
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(Context.instances[0]?.close).toHaveBeenCalledTimes(1);
    expect(node.disconnect).toHaveBeenCalledTimes(1);
    expect(node.port.close).toHaveBeenCalledTimes(1);
    expect(node.port.onmessage).toBeNull();
  });

  it("returns permission denial without retaining an AudioContext or exposing browser details", async () => {
    const { getUserMedia, options } = fixture();
    getUserMedia.mockRejectedValue(new DOMException("private device details", "NotAllowedError"));
    await expect(createMicrophoneCapture(options)).rejects.toMatchObject({
      code: "capture_permission_denied",
      message: "capture_permission_denied",
    });
    expect(Context.instances[0]?.close).toHaveBeenCalledTimes(1);
    expect(Node.instances).toHaveLength(0);
  });

  it("cancels a pending permission prompt immediately and stops a late granted stream", async () => {
    const { track, stream, getUserMedia, options } = fixture();
    const permission = deferred<unknown>();
    getUserMedia.mockReturnValue(permission.promise);
    const abort = new AbortController();
    const starting = createMicrophoneCapture({ ...options, signal: abort.signal });
    abort.abort();
    await expect(starting).rejects.toMatchObject({ code: "capture_cancelled" });
    permission.resolve(stream);
    await vi.waitFor(() => expect(track.stop).toHaveBeenCalledTimes(1));
    expect(Node.instances).toHaveLength(0);
  });

  it.each(["module", "resume"])(
    "aborts while %s stays pending without a late connection",
    async (phase) => {
      const { track, options } = fixture();
      const pending = deferred<void>();
      if (phase === "module") Context.loading = pending.promise;
      else Context.resuming = pending.promise;
      const abort = new AbortController();
      const starting = createMicrophoneCapture({ ...options, signal: abort.signal });
      await vi.waitFor(() => expect(Context.instances).toHaveLength(1));
      abort.abort();
      await expect(starting).rejects.toMatchObject({ code: "capture_cancelled" });
      pending.resolve();
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(Node.instances).toHaveLength(0);
    },
  );

  it("bounds the complete startup deadline and frees a late stream", async () => {
    vi.useFakeTimers();
    const { track, stream, getUserMedia, options } = fixture();
    const permission = deferred<unknown>();
    getUserMedia.mockReturnValue(permission.promise);
    const outcome = createMicrophoneCapture({ ...options, startupTimeoutMs: 25 }).catch(
      (error: unknown) => error,
    );
    await vi.advanceTimersByTimeAsync(25);
    expect(await outcome).toMatchObject({ code: "capture_start_timeout" });
    permission.resolve(stream);
    await Promise.resolve();
    await Promise.resolve();
    expect(track.stop).toHaveBeenCalledTimes(1);
  });

  it("rejects a track that ends while the worklet is still loading", async () => {
    const { track, options } = fixture();
    const module = deferred<void>();
    Context.loading = module.promise;
    const outcome = createMicrophoneCapture(options).catch((error: unknown) => error);
    await Promise.resolve();
    await Promise.resolve();
    track.readyState = "ended";
    track.dispatchEvent(new Event("ended"));
    expect(await outcome).toMatchObject({ code: "capture_device_ended" });
    module.resolve();
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(Context.instances[0]?.state).toBe("closed");
    expect(Node.instances).toHaveLength(0);
  });

  it("reports context cleanup failure while still stopping every track and disconnecting", async () => {
    const { track, options } = fixture();
    const session = await createMicrophoneCapture(options);
    Context.instances[0]!.close.mockRejectedValueOnce(new Error("synthetic close failure"));
    await expect(session.stop()).rejects.toMatchObject({ code: "capture_cleanup_failed" });
    expect(session.snapshot).toEqual({ state: "failed", reason: "capture_cleanup_failed" });
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(Node.instances[0]?.disconnect).toHaveBeenCalledTimes(1);
  });

  it("serializes consumer completion and ACKs only accepted frames", async () => {
    const { options } = fixture();
    const accepted = deferred<void>();
    options.onFrame.mockReturnValueOnce(accepted.promise);
    const session = await createMicrophoneCapture(options);
    const node = Node.instances[0]!;
    node.send(pcm(node));
    node.send(pcm(node, 4));
    await vi.waitFor(() => expect(options.onFrame).toHaveBeenCalledTimes(1));
    expect(node.port.postMessage).not.toHaveBeenCalled();
    accepted.resolve();
    await vi.waitFor(() => expect(node.port.postMessage).toHaveBeenCalledTimes(2));
    expect(node.port.postMessage).toHaveBeenLastCalledWith({
      kind: "ack",
      token: session.token,
      throughSample: 8,
    });
    await session.stop();
  });

  it("keeps the original ACK watermark when the consumer transfers the PCM buffer", async () => {
    const { options } = fixture();
    options.onFrame.mockImplementation((frame) => {
      structuredClone(frame.samples, { transfer: [frame.samples.buffer] });
    });
    const session = await createMicrophoneCapture(options);
    const node = Node.instances[0]!;
    node.send(pcm(node));
    await vi.waitFor(() => expect(node.port.postMessage).toHaveBeenCalled());
    expect(node.port.postMessage).toHaveBeenCalledWith({
      kind: "ack",
      token: session.token,
      throughSample: 4,
    });
    await session.stop();
  });

  it("rejects mismatched clocks and pre-cancelled requests before allocating a node", async () => {
    const { options } = fixture();
    const abort = new AbortController();
    abort.abort();
    await expect(
      createMicrophoneCapture({ ...options, signal: abort.signal }),
    ).rejects.toMatchObject({ code: "capture_cancelled" });
    expect(Context.instances).toHaveLength(0);
    vi.stubGlobal(
      "AudioContext",
      class extends Context {
        constructor(settings: AudioContextOptions) {
          super(settings);
          this.sampleRate = 48000;
        }
      },
    );
    await expect(createMicrophoneCapture(options)).rejects.toMatchObject({
      code: "capture_clock_mismatch",
    });
    expect(Context.instances[0]?.close).toHaveBeenCalledTimes(1);
    expect(Node.instances).toHaveLength(0);
  });

  it("releases hardware even if a consumer never returns, with no late ACK or callback", async () => {
    const { track, options } = fixture();
    const accepted = deferred<void>();
    options.onFrame.mockReturnValueOnce(accepted.promise);
    const session = await createMicrophoneCapture(options);
    const node = Node.instances[0]!;
    node.send(pcm(node));
    node.send(pcm(node, 4));
    await vi.waitFor(() => expect(options.onFrame).toHaveBeenCalledTimes(1));
    const signal = options.onFrame.mock.calls[0]![1];
    await session.stop();
    expect(signal.aborted).toBe(true);
    expect(track.readyState).toBe("ended");
    accepted.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(options.onFrame).toHaveBeenCalledTimes(1);
    expect(node.port.postMessage.mock.calls.filter(([value]) => value.kind === "ack")).toHaveLength(
      0,
    );
  });

  it.each([
    "pagehide",
    "hidden",
    "ended",
    "mute",
    "processor",
    "suspended",
    "consumer",
    "protocol",
    "overrun",
  ])("releases all owned resources on %s", async (reason) => {
    const { track, options } = fixture();
    const session = await createMicrophoneCapture(options);
    const node = Node.instances[0]!;
    if (reason === "pagehide") window.dispatchEvent(new Event("pagehide"));
    if (reason === "hidden") {
      vi.spyOn(document, "hidden", "get").mockReturnValue(true);
      document.dispatchEvent(new Event("visibilitychange"));
    }
    if (reason === "ended" || reason === "mute") track.dispatchEvent(new Event(reason));
    if (reason === "processor") node.onprocessorerror?.();
    if (reason === "suspended") {
      Context.instances[0]!.state = "suspended";
      Context.instances[0]!.dispatchEvent(new Event("statechange"));
    }
    if (reason === "consumer") {
      options.onFrame.mockRejectedValueOnce(new Error("private text"));
      node.send(pcm(node));
    }
    if (reason === "protocol") node.send(pcm(node, 5));
    if (reason === "overrun")
      node.send({ kind: "failed", token: session.token, reason: "capture_overrun" });
    await session.closed;
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(Context.instances[0]?.state).toBe("closed");
    expect(node.port.onmessage).toBeNull();
    expect(session.snapshot.state).not.toBe("running");
  });

  it("ignores a previous token after restart", async () => {
    const { options, getUserMedia } = fixture();
    const first = await createMicrophoneCapture(options);
    await first.stop();
    const nextTrack = new Track();
    getUserMedia.mockResolvedValue({
      getTracks: () => [nextTrack],
      getAudioTracks: () => [nextTrack],
    });
    const second = await createMicrophoneCapture(options);
    expect(second.token).not.toBe(first.token);
    Node.instances[1]!.send(pcm(Node.instances[1]!, 0, first.token));
    expect(options.onFrame).not.toHaveBeenCalled();
    await second.stop();
  });
});
