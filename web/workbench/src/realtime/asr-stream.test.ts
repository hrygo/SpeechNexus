import { describe, expect, it, vi } from "vitest";
import traces from "../../../../tests/fixtures/contracts/transcription-sequences.json";
import { AsrStream } from "./asr-stream";
import { encodePcm } from "./pcm";

import { created, ready, event } from "./realtime-fixtures";

describe("shared standard ASR semantic traces", () => {
  it.each(traces.examples)("$id", async (trace) => {
    const stream = new AsrStream({ sessionId: "session-demo", mode: "transcription" });
    let valid = true;
    try {
      for (const step of trace.steps) {
        if ("transport" in step) {
          stream.disconnect("connection_lost");
          continue;
        }
        const value = structuredClone(step.value) as Record<string, unknown>;
        if ("synthetic_pcm_bytes" in step)
          value.audio = btoa(String.fromCharCode(...new Uint8Array(step.synthetic_pcm_bytes)));
        if (step.direction === "client") await stream.command(value);
        else stream.receive(value);
      }
    } catch {
      valid = false;
    }
    expect(valid).toBe(trace.semantic_valid);
  });
});
describe("nexus ASR independent text, execution and input facts", () => {
  it.each(["input_audio_buffer.commit", "input_audio_buffer.clear", "nexus.session.close"])(
    "a rejected %s preserves buffered input, phase and the original command identity",
    async (type) => {
      const stream = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
      stream.receive(created());
      stream.receive(ready());
      await stream.command({
        type: "input_audio_buffer.append",
        event_id: "audio",
        audio: encodePcm(new Float32Array(2400)),
      });
      const before = stream.getSnapshot();
      const command = {
        type,
        event_id: "rejected-command",
        ...(type === "nexus.session.close" ? { mode: "drain" } : {}),
      };
      await expect(
        stream.command(command, () => {
          throw Error("send rejected");
        }),
      ).rejects.toThrow("send rejected");
      expect(stream.getSnapshot()).toBe(before);
      const retry = await stream.command(command, () => {});
      expect(retry.duplicate).toBe(false);
      expect(stream.getSnapshot()).toMatchObject(
        type === "nexus.session.close"
          ? { phase: "draining", uncommitted: 2400 }
          : { phase: "active", uncommitted: 0 },
      );
    },
  );
  it("commits no samples, credit or command identity when the transport rejects a command", async () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive(ready());
    const before = s.getSnapshot();
    const append = {
      type: "input_audio_buffer.append",
      event_id: "send-failure",
      audio: encodePcm(new Float32Array(2400)),
    };
    const send = vi.fn(() => {
      throw new Error("synthetic transport rejection");
    });
    await expect(s.command(append, send)).rejects.toThrow("synthetic transport rejection");
    expect(s.getSnapshot()).toBe(before);
    const accepted: string[] = [];
    await s.command(append, (wire) => {
      expect(s.getSnapshot()).toBe(before);
      accepted.push(wire);
    });
    expect(accepted.map((wire) => JSON.parse(wire) as unknown)).toEqual([append]);
    expect(s.getSnapshot()).toMatchObject({
      sent: 2400,
      credit: 21600,
      accepted: 0,
      uncommitted: 2400,
    });
    const after = s.getSnapshot();
    await s.command(append, (wire) => accepted.push(wire));
    expect(s.getSnapshot()).toBe(after);
    expect(accepted).toHaveLength(2);
  });
  it("I2 resolves delayed committed predecessors and rejects a cyclic input order", () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive(ready());
    const committed = (event_id: string, item_id: string, previous_item_id: string | null) => ({
      type: "input_audio_buffer.committed",
      event_id,
      item_id,
      previous_item_id,
    });
    s.receive(committed("ack-third", "item-third", "item-second"));
    s.receive(committed("ack-second", "item-second", "item-first"));
    s.receive(committed("ack-first", "item-first", null));
    expect(s.getSnapshot().segments.map((r) => r.itemId)).toEqual([
      "item-first",
      "item-second",
      "item-third",
    ]);
    const bad = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    bad.receive(created());
    bad.receive(ready());
    bad.receive(committed("ack-first", "item-first", "item-second"));
    expect(() => bad.receive(committed("ack-second", "item-second", "item-first"))).toThrow();
  });
  it("reconciles an item arriving between anonymous hypothesis and segment closure", () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive(ready());
    s.receive(
      event(
        "nexus.transcription.hypothesis",
        {
          operation_id: "operation-demo",
          segment_id: "segment",
          revision: 1,
          text: "preview",
          sample_span: { start: 0, end: 2400 },
        },
        2,
      ),
    );
    s.receive({
      type: "input_audio_buffer.committed",
      event_id: "commit-ack",
      item_id: "item",
      previous_item_id: null,
    });
    s.receive({
      type: "conversation.item.input_audio_transcription.completed",
      event_id: "final",
      item_id: "item",
      content_index: 0,
      transcript: "final",
      usage: { type: "duration", seconds: 0.1 },
    });
    s.receive(
      event(
        "nexus.operation.done",
        {
          operation_id: "operation-demo",
          kind: "asr",
          status: "cancelled",
          cleanup_state: "complete",
        },
        3,
      ),
    );
    s.receive(
      event(
        "nexus.transcription.segment_closed",
        {
          operation_id: "operation-demo",
          segment_id: "segment",
          item_id: "item",
          content_index: 0,
          reason: "client_commit",
          sample_span: { start: 0, end: 2400 },
        },
        4,
      ),
    );
    expect(s.getSnapshot().segments).toHaveLength(1);
    expect(s.getSnapshot().segments[0]).toMatchObject({
      final: "final",
      preview: "",
      operation: { status: "cancelled" },
    });
  });
  it("deducts later in-flight audio from an acknowledgement's advertised credit", async () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive(ready());
    for (const id of ["a", "b"])
      await s.command({
        type: "input_audio_buffer.append",
        event_id: id,
        audio: encodePcm(new Float32Array(2400)),
      });
    s.receive(
      event("nexus.input.ack", { accepted_through_sample: 2400, credit_samples: 24000 }, 2),
    );
    expect(s.getSnapshot().credit).toBe(21600);
  });
  it("rejects content before admission and never attaches unverified auxiliary versions", () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    const hypothesis = event(
      "nexus.transcription.hypothesis",
      {
        operation_id: "operation-demo",
        segment_id: "segment-demo",
        item_id: "item-demo",
        revision: 1,
        text: "预览",
        sample_span: { start: 0, end: 2400 },
      },
      3,
    );
    expect(() => s.receive(hypothesis)).toThrow();
    expect(s.getSnapshot().segments).toHaveLength(0);
    s.receive(ready());
    s.receive(hypothesis);
    const meta = {
      operation_id: "operation-demo",
      segment_id: "segment-demo",
      text_revision: 3,
      metadata_revision: 5,
      alignment: { status: "not_requested" },
      diarization: { status: "not_requested" },
    };
    s.receive(event("nexus.transcription.metadata", meta, 4));
    expect(s.getSnapshot().segments[0]!.auxiliary?.verified).toBe(false);
    s.receive(
      event("nexus.transcription.metadata", { ...meta, text_revision: 2, metadata_revision: 6 }, 5),
    );
    expect(s.getSnapshot().segments[0]!.auxiliary?.textRevision).toBe(3);
  });
  it("waits for ready, acknowledges only sent input and preserves final across overall failure", async () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    expect(s.getSnapshot().phase).toBe("awaiting");
    await expect(
      s.command({
        type: "input_audio_buffer.append",
        event_id: "a",
        audio: encodePcm(new Float32Array(2400)),
      }),
    ).rejects.toThrow();
    s.receive(ready());
    await s.command({
      type: "input_audio_buffer.append",
      event_id: "a",
      audio: encodePcm(new Float32Array(2400)),
    });
    expect(s.getSnapshot().accepted).toBe(0);
    s.receive(
      event("nexus.input.ack", { accepted_through_sample: 2400, credit_samples: 24000 }, 2),
    );
    s.receive(
      event(
        "nexus.transcription.hypothesis",
        {
          operation_id: "operation-demo",
          segment_id: "segment-demo",
          item_id: "item-demo",
          revision: 1,
          text: "错误预览",
          sample_span: { start: 0, end: 2400 },
        },
        3,
      ),
    );
    s.receive(
      event(
        "nexus.transcription.hypothesis",
        {
          operation_id: "operation-demo",
          segment_id: "segment-demo",
          item_id: "item-demo",
          revision: 2,
          text: "替换预览",
          sample_span: { start: 0, end: 2400 },
        },
        4,
      ),
    );
    expect(s.getSnapshot().segments[0]!.preview).toBe("替换预览");
    s.receive(
      event(
        "nexus.transcription.segment_closed",
        {
          operation_id: "operation-demo",
          segment_id: "segment-demo",
          item_id: "item-demo",
          reason: "client_commit",
          sample_span: { start: 0, end: 2400 },
        },
        5,
      ),
    );
    s.receive({
      type: "conversation.item.input_audio_transcription.completed",
      event_id: "final-demo",
      item_id: "item-demo",
      content_index: 0,
      transcript: "",
      usage: { type: "duration", seconds: 0.1 },
    });
    expect(s.getSnapshot().segments[0]!.final).toBe("");
    expect(s.getSnapshot().segments[0]!.operation).toBeNull();
    s.receive(
      event(
        "nexus.operation.done",
        {
          operation_id: "operation-demo",
          kind: "asr",
          status: "failed",
          cleanup_state: "quarantined",
          reason: "required_auxiliary_failed",
        },
        6,
      ),
    );
    expect(s.getSnapshot().segments[0]!.final).toBe("");
    expect(s.getSnapshot().segments[0]!.operation?.status).toBe("failed");
    s.disconnect("connection_lost");
    expect(s.getSnapshot().phase).toBe("disconnected");
    expect(s.getSnapshot().gaps).toHaveLength(1);
  });
  it("rejects stale/foreign epochs and invalid acknowledgement without changing facts", () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive(ready());
    const before = s.getSnapshot();
    expect(() =>
      s.receive(event("nexus.input.ack", { accepted_through_sample: 1, credit_samples: 24000 }, 2)),
    ).toThrow();
    expect(s.getSnapshot()).toBe(before);
    expect(() =>
      s.receive({ ...event("nexus.session.gap", { reason: "epoch_change" }, 2), stream_epoch: 2 }),
    ).toThrow();
    expect(() =>
      s.receive({ ...event("nexus.session.gap", { reason: "reconnect" }, 2), session_id: "other" }),
    ).toThrow();
    const gap = event(
      "nexus.session.gap",
      { reason: "input_overflow", sample_span: { start: 0, end: 2400 } },
      2,
    );
    s.receive(gap);
    s.receive(gap);
    expect(s.getSnapshot().gaps).toHaveLength(1);
    expect(() => s.receive({ ...gap, reason: "client_slow" })).toThrow();
  });
  it("enforces credit and safe PCM/message bounds", async () => {
    const s = new AsrStream({ sessionId: "session-demo", mode: "nexus" });
    s.receive(created());
    s.receive({ ...ready(), input_credit_samples: 100 });
    await expect(
      s.command({
        type: "input_audio_buffer.append",
        event_id: "a",
        audio: encodePcm(new Float32Array(2400)),
      }),
    ).rejects.toThrow();
    expect(() => encodePcm(new Float32Array([NaN]))).toThrow();
    expect(() => encodePcm(new Float32Array(6001))).toThrow();
    const bytes = Uint8Array.from(atob(encodePcm(new Float32Array([-1, 0, 1]))), (c) =>
      c.charCodeAt(0),
    );
    expect([...bytes]).toEqual([0, 128, 0, 0, 255, 127]);
  });
});
