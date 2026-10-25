import { validateOptions, type PlaybackOptions } from "./playback-buffer";
import { PlaybackChannel, PLAYBACK_PROCESSOR_NAME } from "./playback-channel";
import workletUrl from "./playback.worklet.ts?worker&url";

interface ContextState {
  loading: Promise<void> | undefined;
  generation: number;
  playback: PlaybackChannel | undefined;
}

const contexts = new WeakMap<BaseAudioContext, ContextState>();

function awaitModule(loading: Promise<void>, signal?: AbortSignal): Promise<void> {
  if (!signal) return loading;
  return new Promise<void>((resolve, reject) => {
    const abort = () => {
      signal.removeEventListener("abort", abort);
      const reason: unknown = signal.reason;
      reject(
        reason instanceof Error ? reason : new DOMException("playback start aborted", "AbortError"),
      );
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
    void loading.then(
      () => {
        signal.removeEventListener("abort", abort);
        resolve();
      },
      (error: unknown) => {
        signal.removeEventListener("abort", abort);
        reject(error instanceof Error ? error : new Error("audio worklet module load failed"));
      },
    );
  });
}

/**
 * A caller-owned audio context and one disposable node per playback generation.
 * Its clock must match the decoded PCM clock. No implicit resampling or device access.
 */
export async function createWorkletPlayback(
  context: BaseAudioContext,
  provided: PlaybackOptions & { pcmSampleRate: number; signal?: AbortSignal },
) {
  const options = { ...provided, identity: { ...provided.identity } };
  validateOptions(options);
  if (context.sampleRate !== options.pcmSampleRate)
    throw new RangeError("PCM and render sample clocks differ");
  const { signal } = options;
  signal?.throwIfAborted();
  let state = contexts.get(context);
  if (!state) {
    state = { loading: undefined, generation: 0, playback: undefined };
    contexts.set(context, state);
  }
  if (options.identity.generation <= state.generation) {
    throw new RangeError("playback generation must advance");
  }
  state.generation = options.identity.generation;
  state.playback?.stop();
  let loading = state.loading;
  if (!loading) {
    loading = context.audioWorklet.addModule(workletUrl);
    state.loading = loading;
    const request = loading;
    void request.catch(() => {
      if (state.loading === request) state.loading = undefined;
    });
  }
  await awaitModule(loading, signal);
  signal?.throwIfAborted();
  if (state.generation !== options.identity.generation) {
    throw new DOMException("playback start superseded", "AbortError");
  }
  const node = new AudioWorkletNode(context, PLAYBACK_PROCESSOR_NAME, {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    processorOptions: {
      identity: options.identity,
      capacitySamples: options.capacitySamples,
      startSample: options.startSample ?? 0,
    },
  });
  const stop = () => playback.stop();
  const playback = new PlaybackChannel(
    {
      send: (message, transfer) => node.port.postMessage(message, transfer),
      close: () => {
        signal?.removeEventListener("abort", stop);
        node.onprocessorerror = null;
        node.port.onmessage = null;
        node.port.close();
        node.disconnect();
        if (state.playback === playback) state.playback = undefined;
      },
    },
    options,
  );
  node.port.onmessage = (event: MessageEvent<unknown>) => playback.receive(event.data);
  node.onprocessorerror = () => playback.fail();
  signal?.addEventListener("abort", stop, { once: true });
  state.playback = playback;
  try {
    node.connect(context.destination);
  } catch (error) {
    playback.stop();
    throw error;
  }
  return playback;
}
