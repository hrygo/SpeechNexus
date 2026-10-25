import { afterEach, expect, it, vi } from "vitest";
import { openTtsPlayback } from "./tts-playback";

afterEach(() => vi.unstubAllGlobals());
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
