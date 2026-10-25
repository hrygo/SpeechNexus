import {
  CAPTURE_PROCESSOR_NAME,
  validateCaptureOptions,
  type CaptureBufferFailure,
  type CaptureFrame,
} from "./capture-buffer";
import workletUrl from "./capture.worklet.ts?worker&url";

type CaptureReason =
  | CaptureBufferFailure
  | "capture_cancelled"
  | "capture_hidden"
  | "capture_page_closed"
  | "capture_start_timeout"
  | "capture_permission_denied"
  | "capture_unavailable"
  | "capture_device_unavailable"
  | "capture_device_ended"
  | "capture_muted"
  | "capture_suspended"
  | "capture_clock_mismatch"
  | "capture_start_failed"
  | "capture_processor_error"
  | "capture_consumer_failed"
  | "capture_cleanup_failed";

export class CaptureError extends Error {
  constructor(readonly code: CaptureReason) {
    super(code);
    this.name = "CaptureError";
  }
}

function startError(error: unknown): CaptureError {
  if (error instanceof CaptureError) return error;
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError") return new CaptureError("capture_permission_denied");
    if (error.name === "NotFoundError" || error.name === "NotReadableError")
      return new CaptureError("capture_device_unavailable");
  }
  return new CaptureError("capture_start_failed");
}

export interface MicrophoneFrame extends CaptureFrame {
  readonly sampleRate: number;
}
export interface MicrophoneCaptureOptions {
  readonly sampleRate: number;
  readonly frameSamples: number;
  readonly maxPendingFrames: number;
  readonly onFrame: (frame: MicrophoneFrame, signal: AbortSignal) => void | Promise<void>;
  readonly signal?: AbortSignal;
  readonly deviceId?: string;
  readonly startupTimeoutMs?: number;
}
export interface MicrophoneCapture {
  readonly token: string;
  readonly sampleRate: number;
  readonly snapshot: {
    readonly state: "running" | "stopped" | "failed";
    readonly reason: CaptureReason | undefined;
  };
  readonly closed: Promise<void>;
  stop(): Promise<void>;
}

/** Owns its stream and context. Invoke from a user gesture; hidden pages stop capture. */
export async function createMicrophoneCapture(
  provided: MicrophoneCaptureOptions,
): Promise<MicrophoneCapture> {
  const options = { ...provided };
  const token = crypto.randomUUID();
  const config = {
    token,
    frameSamples: options.frameSamples,
    maxPendingFrames: options.maxPendingFrames,
  };
  validateCaptureOptions(config);
  const timeout = options.startupTimeoutMs ?? 30000;
  if (
    !Number.isSafeInteger(options.sampleRate) ||
    options.sampleRate < 8000 ||
    options.sampleRate > 192000 ||
    !Number.isSafeInteger(timeout) ||
    timeout < 1 ||
    timeout > 120000 ||
    typeof options.onFrame !== "function"
  ) {
    throw new RangeError("invalid local capture options");
  }
  if (options.signal?.aborted) throw new CaptureError("capture_cancelled");
  if (document.hidden) throw new CaptureError("capture_hidden");
  if (!navigator.mediaDevices?.getUserMedia) throw new CaptureError("capture_unavailable");

  let context: AudioContext | undefined;
  let stream: MediaStream | undefined;
  let source: MediaStreamAudioSourceNode | undefined;
  let node: AudioWorkletNode | undefined;
  let phase: "starting" | "running" | "stopped" | "failed" = "starting";
  let reason: CaptureReason | undefined;
  let cleanupFailed = false;
  const listeners: (() => void)[] = [];
  const lifetime = new AbortController();
  let resolveClosed!: () => void;
  let rejectClosed!: (error: CaptureError) => void;
  const closed = new Promise<void>((resolve, reject) => {
    resolveClosed = resolve;
    rejectClosed = reject;
  });
  // Event-driven termination may precede a caller awaiting this promise.
  void closed.catch(() => undefined);

  const safely = (action: () => void) => {
    try {
      action();
    } catch {
      cleanupFailed = true;
    }
  };
  const stopTracks = (value: MediaStream) => {
    for (const track of value.getTracks()) safely(() => track.stop());
  };
  const finish = (failed: boolean, cause?: CaptureReason): Promise<void> => {
    if (phase === "stopped" || phase === "failed") return closed;
    phase = failed ? "failed" : "stopped";
    reason = cause;
    clearTimeout(timer);
    for (const remove of listeners.splice(0)) remove();
    lifetime.abort(new CaptureError(cause ?? "capture_cancelled"));
    if (node) {
      node.port.onmessage = null;
      node.onprocessorerror = null;
      safely(() => node!.port.close());
      safely(() => node!.disconnect());
    }
    safely(() => source?.disconnect());
    if (stream) stopTracks(stream);
    const release = async () => {
      try {
        if (context && context.state !== "closed") await context.close();
      } catch {
        cleanupFailed = true;
      }
      if (cleanupFailed) {
        phase = "failed";
        reason = "capture_cleanup_failed";
        rejectClosed(new CaptureError(reason));
      } else resolveClosed();
    };
    void release();
    return closed;
  };
  const listen = (target: EventTarget, event: string, callback: () => void) => {
    target.addEventListener(event, callback);
    listeners.push(() => target.removeEventListener(event, callback));
  };
  const wait = <T>(promise: Promise<T>): Promise<T> => {
    const stage = new Promise<T>((resolve, reject) => {
      const abort = () => {
        lifetime.signal.removeEventListener("abort", abort);
        reject(new CaptureError(reason ?? "capture_cancelled"));
      };
      lifetime.signal.addEventListener("abort", abort, { once: true });
      if (lifetime.signal.aborted) abort();
      void promise.then(
        (value) => {
          lifetime.signal.removeEventListener("abort", abort);
          if (!lifetime.signal.aborted) resolve(value);
        },
        (error: unknown) => {
          lifetime.signal.removeEventListener("abort", abort);
          reject(startError(error));
        },
      );
    });
    void stage.catch(() => undefined);
    return stage;
  };

  listen(window, "pagehide", () => {
    void finish(false, "capture_page_closed");
  });
  listen(document, "visibilitychange", () => {
    if (document.hidden) void finish(false, "capture_hidden");
  });
  if (options.signal)
    listen(options.signal, "abort", () => {
      void finish(false, "capture_cancelled");
    });
  const timer = setTimeout(() => {
    void finish(true, "capture_start_timeout");
  }, timeout);

  try {
    context = new AudioContext({ sampleRate: options.sampleRate, latencyHint: "interactive" });
    if (context.sampleRate !== options.sampleRate) throw new CaptureError("capture_clock_mismatch");
    // Start resume during the user gesture, before waiting for permission or modules.
    const resuming = wait(context.resume());
    const loading = wait(context.audioWorklet.addModule(workletUrl));
    const acquiring = wait(
      navigator.mediaDevices
        .getUserMedia({
          audio: {
            channelCount: { ideal: 1 },
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
            ...(options.deviceId ? { deviceId: { exact: options.deviceId } } : {}),
          },
          video: false,
        })
        .then((value) => {
          // Adopt the stream in the native fulfillment callback itself. An extra
          // promise hop can let cancellation run after resolution but before ownership.
          if (lifetime.signal.aborted) {
            stopTracks(value);
            return;
          }
          stream = value;
          const tracks = value.getAudioTracks();
          if (tracks.length !== 1 || tracks[0]!.readyState !== "live")
            throw new CaptureError("capture_device_ended");
          listen(tracks[0]!, "ended", () => {
            void finish(true, "capture_device_ended");
          });
          listen(tracks[0]!, "mute", () => {
            void finish(true, "capture_muted");
          });
        }),
    );
    await Promise.all([acquiring, loading, resuming]);
    if (lifetime.signal.aborted) throw new CaptureError(reason ?? "capture_cancelled");
    if (context.state !== "running") throw new CaptureError("capture_suspended");
    listen(context, "statechange", () => {
      if (context?.state !== "running") void finish(true, "capture_suspended");
    });
    source = context.createMediaStreamSource(stream!);
    node = new AudioWorkletNode(context, CAPTURE_PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      channelCountMode: "explicit",
      channelInterpretation: "speakers",
      outputChannelCount: [1],
      processorOptions: config,
    });
    let through = 0;
    let pending = 0;
    let consumers = Promise.resolve();
    node.onprocessorerror = () => {
      void finish(true, "capture_processor_error");
    };
    node.port.onmessage = ({ data }: MessageEvent<unknown>) => {
      if (phase !== "running") return;
      if (typeof data !== "object" || data === null) {
        void finish(true, "capture_protocol_error");
        return;
      }
      const message = data as Record<string, unknown>;
      if (typeof message.token === "string" && message.token !== token) return;
      if (message.token !== token) {
        void finish(true, "capture_protocol_error");
        return;
      }
      if (
        message.kind === "failed" &&
        (message.reason === "capture_overrun" ||
          message.reason === "capture_invalid_pcm" ||
          message.reason === "capture_protocol_error")
      ) {
        void finish(true, message.reason);
        return;
      }
      if (
        message.kind !== "pcm" ||
        message.startSample !== through ||
        !(message.samples instanceof Float32Array) ||
        message.samples.length !== options.frameSamples ||
        !message.samples.every(Number.isFinite) ||
        !Number.isSafeInteger(through + options.frameSamples) ||
        pending >= options.maxPendingFrames
      ) {
        void finish(true, "capture_protocol_error");
        return;
      }
      const samples = message.samples;
      const frame: MicrophoneFrame = {
        token,
        startSample: through,
        samples,
        sampleRate: options.sampleRate,
      };
      through += samples.length;
      const acceptedThrough = through;
      pending += 1;
      consumers = consumers
        .then(async () => {
          if (phase !== "running") return;
          await options.onFrame(frame, lifetime.signal);
          if (phase !== "running") return;
          pending -= 1;
          node!.port.postMessage({ kind: "ack", token, throughSample: acceptedThrough });
        })
        .catch(() => {
          void finish(true, "capture_consumer_failed");
        });
    };
    clearTimeout(timer);
    phase = "running";
    source.connect(node);
    node.connect(context.destination);
    return {
      token,
      sampleRate: options.sampleRate,
      closed,
      get snapshot() {
        return { state: phase === "starting" ? "failed" : phase, reason };
      },
      stop: () => finish(false),
    };
  } catch (error: unknown) {
    const code = startError(error).code;
    void finish(true, code);
    throw new CaptureError(reason ?? code);
  }
}
