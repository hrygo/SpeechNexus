import { describe, expect, it } from "vitest";
import { TtsStream } from "./tts-stream";
import {
  ttsCommand,
  ttsEvent,
  ttsPcm,
  ttsReady,
  ttsRequest,
  ttsSession,
  ttsStarted,
} from "./tts-fixtures";
import { RealtimeClient } from "./realtime-client";

async function active(credit = 100) {
  const s = new TtsStream(ttsSession());
  await s.receive(ttsReady());
  s.command(ttsCommand("start"));
  await s.receive(ttsStarted(credit));
  return s;
}
describe("TTS contract consumer", () => {
  it("I1 sends cancellation before started and never reopens cancelled input on a late acknowledgement", async () => {
    const s = new TtsStream(ttsSession()),
      sent: string[] = [];
    await s.receive(ttsReady());
    s.command(ttsCommand("start"), (wire) => sent.push(wire));
    s.command(ttsCommand("cancel"), (wire) => sent.push(wire));
    expect(sent.map((w) => (JSON.parse(w) as { type: string }).type)).toEqual([
      "nexus.tts.start",
      "nexus.tts.cancel",
    ]);
    await s.receive(ttsStarted());
    expect(s.getSnapshot().utterance?.phase).toBe("cancelling");
    expect(() => s.command(ttsCommand("append_text", { text_seq: 1, text: "late" }))).toThrow();
    expect(
      await s.receive(
        ttsEvent(
          "nexus.tts.audio.delta",
          { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
          3,
        ),
      ),
    ).toBeNull();
    await s.receive(
      ttsEvent(
        "nexus.operation.done",
        { kind: "tts", status: "cancelled", cleanup_state: "complete" },
        4,
      ),
    );
    expect(s.getSnapshot().utterance?.done?.status).toBe("cancelled");
  });
  it.each([true, false])(
    "I3 drains already accepted text across ACK timing (ackBefore=%s)",
    async (ackBefore) => {
      const s = await active();
      s.command(ttsCommand("append_text", { text_seq: 1, text: "existing" }));
      const ack = ttsEvent(
        "nexus.tts.text_ack",
        { accepted_through_text_seq: 1, text_credit: 100 },
        3,
      );
      if (ackBefore) await s.receive(ack);
      s.drain();
      if (!ackBefore) await s.receive(ack);
      expect(() => s.command(ttsCommand("start", { utterance_id: "new" }))).toThrow();
      expect(() => s.command(ttsCommand("append_text", { text_seq: 2, text: "new" }))).toThrow();
      s.command(ttsCommand("commit_text", { through_text_seq: 1 }));
      s.command(ttsCommand("finish_text"));
      expect(s.getSnapshot()).toMatchObject({
        phase: "draining",
        utterance: { phase: "finishing", committed: 1, text: "existing" },
      });
    },
  );
  it("deduplicates semantically identical reordered JSON without replaying audio", async () => {
    const s = await active();
    const original = ttsEvent(
      "nexus.tts.text_ack",
      { accepted_through_text_seq: 1, text_credit: 80 },
      3,
    );
    s.command(ttsCommand("append_text", { text_seq: 1, text: "a" }));
    await s.receive(original);
    const reordered = Object.fromEntries(Object.entries(original).reverse());
    expect(await s.receive(reordered)).toBeNull();
  });
  it("requires resolved default voice before acknowledging a TTS session", async () => {
    const input = ttsRequest(),
      session = ttsSession();
    delete input.voice;
    delete session.effective_configuration.voice;
    const client = new RealtimeClient(() =>
      Promise.resolve(
        new Response(JSON.stringify(session), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(client.create(input, "key")).rejects.toThrow();
  });
  it("records succeeded separately from received audio and rejects contradictory terminal outcome", async () => {
    const s = await active();
    s.command(ttsCommand("append_text", { text_seq: 1, text: "a" }));
    await s.receive(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 100 }, 3),
    );
    s.command(ttsCommand("finish_text"));
    const done = ttsEvent(
      "nexus.operation.done",
      {
        kind: "tts",
        status: "succeeded",
        cleanup_state: "complete",
        receipt: {
          receipt_id: "receipt-1",
          requested: {},
          effective: {},
          observed: {},
          missing: [],
          unknown: [],
        },
      },
      4,
    );
    await s.receive(done);
    expect(s.getSnapshot().utterance).toMatchObject({ received: 0, done: { status: "succeeded" } });
    expect(await s.receive(done)).toBeNull();
    await expect(
      s.receive(
        ttsEvent(
          "nexus.operation.done",
          { kind: "tts", status: "cancelled", cleanup_state: "complete" },
          5,
        ),
      ),
    ).rejects.toThrow();
  });
  it("uses frozen REST TTS references and rejects another voice/model", async () => {
    const make = (value: unknown) =>
      new RealtimeClient(() =>
        Promise.resolve(
          new Response(JSON.stringify(value), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      );
    expect(await make(ttsSession()).create(ttsRequest(), "original")).toEqual(ttsSession());
    await expect(
      make({
        ...ttsSession(),
        effective_configuration: {
          ...ttsSession().effective_configuration,
          voice: { id: "other", revision: "1" },
        },
      }).create(ttsRequest(), "original"),
    ).rejects.toThrow();
  });
  it("waits for ready, started and ACK; freezes prefixes and finish closes text input", async () => {
    const s = new TtsStream(ttsSession());
    expect(() => s.command(ttsCommand("start"))).toThrow();
    await s.receive(ttsReady());
    s.command(ttsCommand("start"));
    expect(() => s.command(ttsCommand("append_text", { text_seq: 1, text: "甲" }))).toThrow();
    await s.receive(ttsStarted(3));
    s.command(ttsCommand("append_text", { text_seq: 1, text: "甲" }));
    expect(s.getSnapshot().utterance?.credit).toBe(0);
    expect(() => s.command(ttsCommand("append_text", { text_seq: 2, text: "乙" }))).toThrow();
    await s.receive(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 30 }, 3),
    );
    s.command(ttsCommand("commit_text", { through_text_seq: 1 }));
    s.command(ttsCommand("finish_text"));
    expect(s.getSnapshot().utterance).toMatchObject({
      text: "甲",
      committed: 1,
      phase: "finishing",
    });
    expect(() => s.command(ttsCommand("append_text", { text_seq: 2, text: "乙" }))).toThrow();
  });
  it("does not publish a command that failed to send; duplicate original command is idempotent", async () => {
    const s = await active(),
      cmd = ttsCommand("append_text", { text_seq: 1, text: "hello" });
    expect(() =>
      s.command(cmd, () => {
        throw Error("socket full");
      }),
    ).toThrow();
    expect(s.getSnapshot().utterance?.sent).toBe(0);
    s.command(cmd);
    const same = s.getSnapshot();
    s.command(cmd);
    expect(s.getSnapshot()).toBe(same);
    expect(() => s.command({ ...cmd, text: "changed" })).toThrow();
  });
  it("decodes precise little-endian PCM without retaining audio in snapshots; duplicate does not replay", async () => {
    const s = await active();
    s.command(ttsCommand("append_text", { text_seq: 1, text: "a" }));
    await s.receive(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 99 }, 3),
    );
    s.command(ttsCommand("commit_text", { through_text_seq: 1 }));
    const audio = ttsEvent(
      "nexus.tts.audio.delta",
      { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
      4,
    );
    const pcm = await s.receive(audio);
    expect([...pcm!.samples]).toEqual([0, 0.5, -1]);
    expect(s.getSnapshot().utterance?.received).toBe(3);
    expect(JSON.stringify(s.getSnapshot())).not.toContain(ttsPcm());
    expect(await s.receive(audio)).toBeNull();
    await expect(s.receive({ ...audio, audio: ttsPcm([1, 2, 3]) })).rejects.toThrow();
  });
  it("rejects epoch, seq, audio gap, invalid base64, size mismatch and wrong operation", async () => {
    for (const fields of [
      { stream_epoch: 2 },
      { seq: 1 },
      { start_sample: 1 },
      { chunk_seq: 2 },
      { audio: "%%%" },
      { sample_count: 4 },
      { operation_id: "foreign" },
    ]) {
      const s = await active();
      s.command(ttsCommand("append_text", { text_seq: 1, text: "a" }));
      await s.receive(
        ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 1 }, 3),
      );
      s.command(ttsCommand("commit_text", { through_text_seq: 1 }));
      await expect(
        s.receive(
          ttsEvent(
            "nexus.tts.audio.delta",
            { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3, ...fields },
            4,
          ),
        ),
      ).rejects.toThrow();
      expect(s.getSnapshot().utterance?.received).toBe(0);
    }
  });
  it("cancels locally and discards late audio, waits for terminal release before another start", async () => {
    const s = await active();
    s.command(ttsCommand("cancel"));
    expect(
      await s.receive(
        ttsEvent(
          "nexus.tts.audio.delta",
          { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
          3,
        ),
      ),
    ).toBeNull();
    expect(() => s.command(ttsCommand("start", { utterance_id: "next" }))).toThrow();
    await s.receive(
      ttsEvent(
        "nexus.operation.done",
        { kind: "tts", status: "cancelled", cleanup_state: "complete" },
        4,
      ),
    );
    s.command(ttsCommand("start", { utterance_id: "next" }));
    expect(s.getSnapshot().utterance?.id).toBe("next");
  });
  it("checks ACK monotonicity and does not accept a foreign ready configuration", async () => {
    const s = await active();
    await expect(
      s.receive(
        ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 1 }, 3),
      ),
    ).rejects.toThrow();
    const fresh = new TtsStream(ttsSession()),
      bad = ttsReady();
    const cfg = bad.effective_configuration as Record<string, unknown>;
    await expect(
      fresh.receive({
        ...bad,
        effective_configuration: { ...cfg, models: { tts: { id: "other", revision: "1" } } },
      }),
    ).rejects.toThrow();
  });
  it("caps wire message bytes before decode", async () => {
    const s = await active();
    await expect(
      s.receive(
        ttsEvent(
          "nexus.tts.audio.delta",
          { audio: "A".repeat(65536), chunk_seq: 1, start_sample: 0, sample_count: 24576 },
          3,
        ),
      ),
    ).rejects.toThrow();
    expect(s.getSnapshot().utterance?.received).toBe(0);
  });
});
