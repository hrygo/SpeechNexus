import { createWorkletPlayback } from "../media/worklet-playback";
import { ttsCheck } from "./tts-stream";

export type TtsPlayback = {
  push: (start: number, samples: Float32Array) => void;
  stop: () => Promise<void>;
  snapshot: () => {
    state: "running" | "closing" | "stopped" | "failed";
    received: number;
    rendered: number;
  };
};
export class PlaybackCleanupError extends Error {
  constructor() {
    super("Local playback cleanup could not be confirmed");
  }
}
/** The render watermark proves Worklet consumption, not physical loudspeaker latency. */
export async function openTtsPlayback(
  epoch: number,
  generation: number,
  signal: AbortSignal,
): Promise<TtsPlayback> {
  signal.throwIfAborted();
  const context = new AudioContext({ sampleRate: 24000 }),
    identity = { epoch, generation };
  let resourceState: "running" | "closing" | "stopped" | "failed" = "running";
  let closing: Promise<void> | undefined;
  let channel: Awaited<ReturnType<typeof createWorkletPlayback>> | undefined;
  const close = () => {
    if (closing) return closing;
    resourceState = "closing";
    signal.removeEventListener("abort", onAbort);
    let graphFailed = false;
    try {
      channel?.stop();
    } catch {
      graphFailed = true;
    }
    let contextClosing: Promise<void>;
    try {
      contextClosing = context.close();
    } catch {
      contextClosing = Promise.reject(new PlaybackCleanupError());
    }
    closing = contextClosing
      .then(() => {
        if (graphFailed) throw new PlaybackCleanupError();
        resourceState = "stopped";
      })
      .catch(() => {
        resourceState = "failed";
        throw new PlaybackCleanupError();
      });
    // Abort listeners cannot await; callers still receive the same observable result.
    void closing.catch(() => {});
    return closing;
  };
  const onAbort = () => {
    void close();
  };
  signal.addEventListener("abort", onAbort, { once: true });
  try {
    // Called before the first await to preserve the explicit user gesture.
    const resume = context.resume();
    await new Promise<void>((resolve, reject) => {
      const aborted = () =>
        reject(
          signal.reason instanceof Error ? signal.reason : new Error("Playback start aborted"),
        );
      signal.addEventListener("abort", aborted, { once: true });
      void resume.then(resolve, reject).finally(() => signal.removeEventListener("abort", aborted));
      if (signal.aborted) aborted();
    });
    signal.throwIfAborted();
    ttsCheck(context.state === "running");
    const playbackChannel = (channel = await createWorkletPlayback(context, {
      identity,
      capacitySamples: 120000,
      pcmSampleRate: 24000,
      signal,
    }));
    return {
      push(start, samples) {
        ttsCheck(
          resourceState === "running" &&
            context.state === "running" &&
            samples.length <= playbackChannel.snapshot.availableSamples,
        );
        for (let i = 0; i < samples.length; i += 8192)
          ttsCheck(
            playbackChannel.push(identity, start + i, samples.subarray(i, i + 8192)) === "accepted",
          );
      },
      snapshot() {
        const s = playbackChannel.snapshot;
        return {
          state:
            resourceState !== "running"
              ? resourceState
              : context.state !== "running"
                ? "failed"
                : s.state,
          received: s.sentThrough,
          rendered: s.renderedThrough,
        };
      },
      stop: close,
    };
  } catch (error) {
    await close();
    throw error;
  }
}
