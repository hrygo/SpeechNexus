import { createWorkletPlayback } from "../media/worklet-playback";
import { ttsCheck } from "./tts-stream";

export type TtsPlayback = {
  push: (start: number, samples: Float32Array) => void;
  stop: () => void;
  snapshot: () => { state: "running" | "stopped" | "failed"; received: number; rendered: number };
};
/** The render watermark proves Worklet consumption, not physical loudspeaker latency. */
export async function openTtsPlayback(
  epoch: number,
  generation: number,
  signal: AbortSignal,
): Promise<TtsPlayback> {
  signal.throwIfAborted();
  const context = new AudioContext({ sampleRate: 24000 }),
    identity = { epoch, generation };
  let closed = false;
  const close = () => {
    if (!closed) {
      closed = true;
      signal.removeEventListener("abort", close);
      void context.close().catch(() => {});
    }
  };
  signal.addEventListener("abort", close, { once: true });
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
    const channel = await createWorkletPlayback(context, {
      identity,
      capacitySamples: 120000,
      pcmSampleRate: 24000,
      signal,
    });
    return {
      push(start, samples) {
        ttsCheck(
          !closed &&
            context.state === "running" &&
            samples.length <= channel.snapshot.availableSamples,
        );
        for (let i = 0; i < samples.length; i += 8192)
          ttsCheck(channel.push(identity, start + i, samples.subarray(i, i + 8192)) === "accepted");
      },
      snapshot() {
        const s = channel.snapshot;
        return {
          state: closed ? "stopped" : context.state !== "running" ? "failed" : s.state,
          received: s.sentThrough,
          rendered: s.renderedThrough,
        };
      },
      stop() {
        channel.stop();
        close();
      },
    };
  } catch (error) {
    close();
    throw error;
  }
}
