import { describe, expect, it } from "vitest";

import { PlaybackBuffer, type PlaybackIdentity } from "./playback-buffer";

const identity: PlaybackIdentity = { epoch: 1, generation: 1 };

describe("bounded playback buffer", () => {
  it("preserves order across wraparound and zeroes underruns", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 4, identity });
    expect(buffer.push(identity, 0, new Float32Array([0.1, 0.2, 0.3]))).toBe("accepted");
    const first = new Float32Array(2);
    expect(buffer.renderInto(first)).toBe(2);
    expect([...first]).toEqual([...new Float32Array([0.1, 0.2])]);
    expect(buffer.push(identity, 3, new Float32Array([0.4, 0.5, 0.6]))).toBe("accepted");
    const rest = new Float32Array(6).fill(1);
    expect(buffer.renderInto(rest)).toBe(4);
    expect([...rest]).toEqual([...new Float32Array([0.3, 0.4, 0.5, 0.6, 0, 0])]);
    expect(buffer.snapshot).toMatchObject({
      bufferedSamples: 0,
      acceptedThrough: 6,
      renderedThrough: 6,
    });
  });

  it("rejects overflow atomically without discarding previously accepted samples", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 3, identity });
    buffer.push(identity, 0, new Float32Array([0.1, 0.2]));
    expect(buffer.push(identity, 2, new Float32Array([0.3, 0.4]))).toBe("backpressure");
    expect(buffer.snapshot.acceptedThrough).toBe(2);
    expect(buffer.push(identity, 2, new Float32Array([0.3]))).toBe("accepted");
    const output = new Float32Array(3);
    buffer.renderInto(output);
    expect([...output]).toEqual([...new Float32Array([0.1, 0.2, 0.3])]);
  });

  it("reports sample gaps and overlaps without synthesizing replacement audio", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 8, identity, startSample: 100 });
    expect(buffer.push(identity, 101, new Float32Array([0.2]))).toBe("gap");
    expect(buffer.push(identity, 99, new Float32Array([0.2]))).toBe("gap");
    expect(buffer.snapshot.acceptedThrough).toBe(100);
    expect(buffer.push(identity, 100, new Float32Array([0.3]))).toBe("accepted");
    expect(buffer.snapshot.acceptedThrough).toBe(101);
  });

  it("drops stale epochs and generations, and stop clears all queued content", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 4, identity });
    expect(buffer.push({ epoch: 2, generation: 1 }, 0, new Float32Array([0.2]))).toBe("stale");
    expect(buffer.push({ epoch: 1, generation: 2 }, 0, new Float32Array([0.2]))).toBe("stale");
    const source = new Float32Array([0.2, 0.3]);
    buffer.push(identity, 0, source);
    source.fill(1);
    buffer.stop();
    const output = new Float32Array(4).fill(1);
    expect(buffer.renderInto(output)).toBe(0);
    expect([...output]).toEqual([0, 0, 0, 0]);
    expect(buffer.push(identity, 2, source)).toBe("stopped");
  });

  it("copies accepted samples instead of retaining a mutable caller buffer", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 2, identity });
    const source = new Float32Array([0.25, -0.25]);
    buffer.push(identity, 0, source);
    source.fill(1);
    const output = new Float32Array(2);
    buffer.renderInto(output);
    expect([...output]).toEqual([0.25, -0.25]);
  });

  it.each([0, -1, 1.5, Infinity, Number.MAX_SAFE_INTEGER])(
    "rejects invalid capacity %s",
    (capacitySamples) => {
      expect(() => new PlaybackBuffer({ capacitySamples, identity })).toThrow(RangeError);
    },
  );

  it("rejects invalid PCM and unsafe cursors before changing watermarks", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 4, identity });
    for (const source of [
      new Float32Array(),
      new Float32Array([NaN]),
      new Float32Array([Infinity]),
      new Float32Array([1.1]),
    ]) {
      expect(() => buffer.push(identity, 0, source)).toThrow(RangeError);
    }
    expect(() => buffer.push(identity, Number.MAX_SAFE_INTEGER + 1, new Float32Array([0]))).toThrow(
      RangeError,
    );
    expect(buffer.snapshot.acceptedThrough).toBe(0);
  });

  it("bounds individual messages as well as the total sample buffer", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 16384, identity });
    expect(() => buffer.push(identity, 0, new Float32Array(8193))).toThrow(RangeError);
    expect(buffer.snapshot.acceptedThrough).toBe(0);
  });

  it("keeps exact integer watermarks through repeated wraparound and underruns", () => {
    const buffer = new PlaybackBuffer({ capacitySamples: 256, identity });
    const samples = new Float32Array(128).fill(0.25);
    const output = new Float32Array(128);
    for (let quantum = 0; quantum < 1000; quantum += 1) {
      expect(buffer.push(identity, quantum * 128, samples)).toBe("accepted");
      expect(buffer.renderInto(output)).toBe(128);
      expect(output.every((sample) => sample === 0.25)).toBe(true);
      expect(buffer.renderInto(output)).toBe(0);
      expect(output.every((sample) => sample === 0)).toBe(true);
    }
    expect(buffer.snapshot).toMatchObject({
      acceptedThrough: 128000,
      renderedThrough: 128000,
      bufferedSamples: 0,
    });
  });
});
