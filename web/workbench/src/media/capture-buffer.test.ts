import { describe, expect, it, vi } from "vitest";

import { CaptureBuffer, type CaptureFrame } from "./capture-buffer";

function fixture(frameSamples = 4, maxPendingFrames = 2) {
  const frames: CaptureFrame[] = [];
  const failed = vi.fn();
  const buffer = new CaptureBuffer(
    { token: "capture-a", frameSamples, maxPendingFrames },
    (frame) => frames.push(frame),
    failed,
  );
  return { buffer, frames, failed };
}

describe("bounded capture producer", () => {
  it("forms exact continuous frames across variable render quanta without retaining input", () => {
    const { buffer, frames } = fixture();
    const input = new Float32Array([0.25, -0.5, 0.75]);
    buffer.push(input);
    input.fill(0);
    buffer.push(new Float32Array([1, 0.5, -0.25, 0.75, 0]));
    expect(frames.map((f) => [f.token, f.startSample, Array.from(f.samples)])).toEqual([
      ["capture-a", 0, [0.25, -0.5, 0.75, 1]],
      ["capture-a", 4, [0.5, -0.25, 0.75, 0]],
    ]);
  });

  it("fails explicitly on overrun without sending a partial frame or growing a queue", () => {
    const { buffer, frames, failed } = fixture();
    buffer.push(new Float32Array(8));
    buffer.push(new Float32Array(1));
    expect(buffer.stopped).toBe(true);
    expect(frames).toHaveLength(2);
    expect(failed).toHaveBeenCalledExactlyOnceWith("capture_overrun");
    buffer.push(new Float32Array(8));
    buffer.acknowledge(8);
    expect(frames).toHaveLength(2);
  });

  it("restores only cumulatively acknowledged credit and accepts duplicate old ACKs", () => {
    const { buffer, frames, failed } = fixture();
    buffer.push(new Float32Array(8));
    buffer.acknowledge(4);
    buffer.acknowledge(4);
    buffer.push(new Float32Array(4));
    buffer.acknowledge(12);
    buffer.acknowledge(8);
    buffer.push(new Float32Array(8));
    expect(frames.map((frame) => frame.startSample)).toEqual([0, 4, 8, 12, 16]);
    expect(failed).not.toHaveBeenCalled();
  });

  it.each([NaN, Infinity, -1, 3, 12])("fails on forged ACK %s", (through) => {
    const { buffer, failed } = fixture();
    buffer.push(new Float32Array(8));
    buffer.acknowledge(through);
    expect(buffer.stopped).toBe(true);
    expect(failed).toHaveBeenCalledExactlyOnceWith("capture_protocol_error");
  });

  it("clears partial PCM on stop and rejects non-finite input", () => {
    const { buffer, frames, failed } = fixture();
    buffer.push(new Float32Array([0.5, NaN]));
    expect(failed).toHaveBeenCalledExactlyOnceWith("capture_invalid_pcm");
    buffer.stop();
    buffer.push(new Float32Array(4));
    expect(frames).toHaveLength(0);
  });

  it.each([
    { frameSamples: 0 },
    { frameSamples: 8193 },
    { maxPendingFrames: 0 },
    { maxPendingFrames: 65 },
    { token: "" },
  ])("rejects invalid local limits %j", (invalid) => {
    expect(
      () =>
        new CaptureBuffer(
          { token: "a", frameSamples: 4, maxPendingFrames: 2, ...invalid },
          vi.fn(),
          vi.fn(),
        ),
    ).toThrow(RangeError);
  });
});
