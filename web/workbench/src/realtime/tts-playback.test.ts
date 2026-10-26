import { afterEach, expect, it, vi } from "vitest";
import { openTtsPlayback, PlaybackCleanupError } from "./tts-playback";

afterEach(() => vi.unstubAllGlobals());

function playbackFixture() {
  let finishClose!: () => void;
  let failClose!: (error: Error) => void;
  const closing = new Promise<void>((resolve, reject) => {
    finishClose = resolve;
    failClose = reject;
  });
  const context = {
    sampleRate: 24000,
    state: "running" as AudioContextState,
    destination: {},
    resume: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
    close: vi.fn<() => Promise<void>>(),
    audioWorklet: {
      addModule: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
    },
  };
  context.close.mockImplementation(() =>
    closing.then(() => {
      context.state = "closed";
    }),
  );
  const node = {
    port: {
      postMessage: vi.fn<(message: { kind: string }, transfer: Transferable[]) => void>(),
      close: vi.fn(),
      onmessage: null as unknown,
    },
    connect: vi.fn(),
    disconnect: vi.fn(),
    onprocessorerror: null as (() => void) | null,
  };
  vi.stubGlobal(
    "AudioContext",
    class {
      constructor() {
        return context;
      }
    },
  );
  vi.stubGlobal(
    "AudioWorkletNode",
    class {
      constructor() {
        return node;
      }
    },
  );
  return { context, node, closing, finishClose, failClose };
}

it.each(["resolve", "reject"] as const)(
  "aborted initialization remains owned until close %s",
  async (outcome) => {
    const { context, finishClose, failClose } = playbackFixture();
    context.resume.mockReturnValue(new Promise(() => {}));
    const abort = new AbortController();
    const opening = openTtsPlayback(1, 1, abort.signal);
    const result = opening.then(
      () => "opened",
      (error: unknown) => error,
    );
    const settled = vi.fn();
    void result.then(settled);
    abort.abort();
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).not.toHaveBeenCalled();
    expect(context.audioWorklet.addModule).not.toHaveBeenCalled();
    if (outcome === "resolve") {
      finishClose();
      expect(await result).toBe(abort.signal.reason);
    } else {
      failClose(Error("synthetic cleanup failure"));
      expect(await result).toBeInstanceOf(PlaybackCleanupError);
    }
    expect(context.close).toHaveBeenCalledTimes(1);
  },
);

it.each(["stop", "abort"] as const)(
  "%s waits for context closure before reporting stopped and closes resources once",
  async (action) => {
    const { context, node, finishClose } = playbackFixture();
    const abort = new AbortController();
    const playback = await openTtsPlayback(1, 1, abort.signal);
    playback.push(0, new Float32Array([0.1, 0.2]));
    expect(playback.snapshot()).toEqual({ state: "running", received: 2, rendered: 0 });
    expect(context.audioWorklet.addModule).toHaveBeenCalledTimes(1);
    expect(node.connect).toHaveBeenCalledWith(context.destination);

    if (action === "stop") void playback.stop();
    else abort.abort();
    expect.soft(playback.snapshot().state).not.toBe("stopped");
    expect(() => playback.push(2, new Float32Array([0.3]))).toThrow();
    void playback.stop();
    abort.abort();
    expect(context.close).toHaveBeenCalledTimes(1);
    expect(node.port.close).toHaveBeenCalledTimes(1);
    expect(node.disconnect).toHaveBeenCalledTimes(1);
    expect(
      node.port.postMessage.mock.calls.filter(([message]) => message.kind === "stop"),
    ).toHaveLength(1);

    finishClose();
    await vi.waitFor(() => expect(playback.snapshot().state).toBe("stopped"));
    expect(playback.snapshot()).toEqual({ state: "stopped", received: 2, rendered: 0 });
    expect(() => playback.push(2, new Float32Array([0.3]))).toThrow();
    void playback.stop();
    expect(context.close).toHaveBeenCalledTimes(1);
  },
);

it.each(["stop", "abort"] as const)(
  "%s exposes a rejected context closure as failed instead of stopped",
  async (action) => {
    const { context, node, closing, failClose } = playbackFixture();
    const abort = new AbortController();
    const playback = await openTtsPlayback(1, 1, abort.signal);
    if (action === "stop") void playback.stop();
    else abort.abort();

    failClose(new Error("synthetic context close failure"));
    await expect(closing).rejects.toThrow("synthetic context close failure");
    await vi.waitFor(() => expect(playback.snapshot().state).toBe("failed"));
    expect(() => playback.push(0, new Float32Array([0.1]))).toThrow();
    void playback.stop();
    abort.abort();
    expect(playback.snapshot().state).toBe("failed");
    expect(context.close).toHaveBeenCalledTimes(1);
    expect(node.port.close).toHaveBeenCalledTimes(1);
    expect(node.disconnect).toHaveBeenCalledTimes(1);
  },
);

it("closes an owned context while resume is pending and abort prevents a late graph", async () => {
  let resolve!: () => void;
  const context = {
    sampleRate: 24000,
    state: "suspended",
    resume: vi.fn(
      () =>
        new Promise<void>((r) => {
          resolve = r;
        }),
    ),
    close: vi.fn(() => Promise.resolve()),
    audioWorklet: { addModule: vi.fn() },
  };
  vi.stubGlobal(
    "AudioContext",
    class {
      constructor() {
        return context;
      }
    },
  );
  const abort = new AbortController(),
    open = openTtsPlayback(1, 1, abort.signal);
  abort.abort();
  resolve();
  await expect(open).rejects.toThrow();
  expect(context.close).toHaveBeenCalledTimes(1);
  expect(context.audioWorklet.addModule).not.toHaveBeenCalled();
});
