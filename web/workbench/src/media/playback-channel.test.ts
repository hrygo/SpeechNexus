import { describe, expect, it } from "vitest";

import { PlaybackChannel, type PlaybackTransport, type ProcessorCommand } from "./playback-channel";
import type { PlaybackIdentity } from "./playback-buffer";

const identity: PlaybackIdentity = { epoch: 2, generation: 3 };

class FakeTransport implements PlaybackTransport {
  messages: ProcessorCommand[] = [];
  closed = 0;
  failSend = false;

  send(message: ProcessorCommand) {
    if (this.failSend) throw new Error("synthetic transport failure");
    this.messages.push(message);
  }

  close() {
    this.closed += 1;
  }
}

function channel(capacitySamples = 4, maxInFlightMessages = 2) {
  const transport = new FakeTransport();
  const playback = new PlaybackChannel(transport, {
    identity,
    capacitySamples,
    maxInFlightMessages,
  });
  return { transport, playback };
}

describe("playback channel", () => {
  it("bounds PCM in flight and restores credit from monotonic consumed samples only", () => {
    const { playback } = channel();
    playback.push(identity, 0, new Float32Array([0.2, 0.3, 0.4, 0.5]));
    expect(playback.push(identity, 4, new Float32Array([0.1]))).toBe("backpressure");
    playback.receive({ kind: "received", identity, pushId: 1 });
    expect(playback.snapshot.availableSamples).toBe(0);
    playback.receive({ kind: "consumed", identity, throughSample: 2 });
    expect(playback.snapshot.availableSamples).toBe(2);
    playback.receive({ kind: "consumed", identity, throughSample: 2 });
    playback.receive({ kind: "consumed", identity, throughSample: 1 });
    expect(playback.snapshot.availableSamples).toBe(2);
    expect(playback.push(identity, 4, new Float32Array([0.1, 0.2]))).toBe("accepted");
    expect(playback.snapshot.availableSamples).toBe(0);
  });

  it("also bounds message count when chunks are very small", () => {
    const { playback } = channel(100, 2);
    playback.push(identity, 0, new Float32Array([0.1]));
    playback.push(identity, 1, new Float32Array([0.2]));
    expect(playback.push(identity, 2, new Float32Array([0.3]))).toBe("backpressure");
    playback.receive({ kind: "received", identity, pushId: 1 });
    playback.receive({ kind: "received", identity, pushId: 1 });
    expect(playback.push(identity, 2, new Float32Array([0.3]))).toBe("accepted");
    expect(playback.snapshot.inFlightMessages).toBe(2);
  });

  it("ignores stale messages and rejects noncontiguous audio without advancing the cursor", () => {
    const { playback, transport } = channel();
    const old = { epoch: 1, generation: 2 };
    expect(playback.push(old, 0, new Float32Array([0.2]))).toBe("stale");
    expect(playback.push(identity, 1, new Float32Array([0.2]))).toBe("gap");
    playback.receive({ kind: "consumed", identity: old, throughSample: 100 });
    expect(transport.messages).toHaveLength(0);
    expect(playback.snapshot.availableSamples).toBe(4);
  });

  it("fails closed on impossible consumption or processor rejection", () => {
    const first = channel();
    first.playback.receive({ kind: "consumed", identity, throughSample: 1 });
    expect(first.playback.snapshot.state).toBe("failed");
    expect(first.transport.closed).toBe(1);
    const second = channel();
    second.playback.receive({ kind: "rejected", identity, reason: "gap" });
    expect(second.playback.snapshot.state).toBe("failed");
    expect(second.transport.closed).toBe(1);
  });

  it("stop is idempotent, releases transport, and never accepts late audio", () => {
    const { playback, transport } = channel();
    playback.push(identity, 0, new Float32Array([0.1]));
    playback.stop();
    playback.stop();
    expect(transport.closed).toBe(1);
    expect(playback.snapshot.inFlightMessages).toBe(0);
    expect(playback.push(identity, 1, new Float32Array([0.2]))).toBe("stopped");
    playback.receive({ kind: "consumed", identity, throughSample: 1 });
    expect(playback.snapshot.renderedThrough).toBe(0);
  });

  it("closes transport when sending a frame throws", () => {
    const { playback, transport } = channel();
    transport.failSend = true;
    expect(() => playback.push(identity, 0, new Float32Array([0.1]))).toThrow(
      "playback transport failed",
    );
    expect(playback.snapshot.state).toBe("failed");
    expect(transport.closed).toBe(1);
  });

  it("copies samples before transfer and keeps identity independent of caller mutation", () => {
    const token = { ...identity };
    const { transport } = channel();
    const playback = new PlaybackChannel(transport, { identity: token, capacitySamples: 4 });
    token.generation = 99;
    const source = new Float32Array([0.25]);
    playback.push(identity, 0, source);
    source.fill(1);
    const message = transport.messages[0];
    expect(message?.kind).toBe("pcm");
    if (message?.kind === "pcm") expect([...message.samples]).toEqual([0.25]);
  });
});
