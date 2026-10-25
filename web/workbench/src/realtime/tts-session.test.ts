import { describe, expect, it, vi } from "vitest";
import { TtsController } from "./tts-session";
import { TtsJournal, ttsJournalKey } from "./tts-journal";
import { storage } from "./realtime-fixtures";
import { ttsEvent, ttsPcm, ttsReady, ttsRequest, ttsSession, ttsStarted } from "./tts-fixtures";
import type { RealtimeClient } from "./realtime-client";
import type { RealtimeSocket } from "./realtime-session";
import { PlaybackCleanupError } from "./tts-playback";

class Socket extends EventTarget implements RealtimeSocket {
  readyState = 1;
  bufferedAmount = 0;
  sent: Record<string, unknown>[] = [];
  send(s: string) {
    this.sent.push(JSON.parse(s) as Record<string, unknown>);
  }
  close = vi.fn(() => {
    this.readyState = 3;
    this.dispatchEvent(new Event("close"));
  });
  message(value: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}
function fixture(closing = Promise.resolve()) {
  const s = storage(),
    socket = new Socket(),
    client = {
      create: vi.fn<RealtimeClient["create"]>().mockResolvedValue(ttsSession()),
      current: vi.fn<RealtimeClient["current"]>().mockResolvedValue(ttsSession()),
      cancel: vi
        .fn<RealtimeClient["cancel"]>()
        .mockResolvedValue({ ...ttsSession(), status: "draining", cleanup_state: "pending" }),
    };
  let rendered = 0,
    received = 0;
  let state: "running" | "closing" | "stopped" | "failed" = "running";
  let stopped: Promise<void> | undefined;
  const stop = vi.fn(() => {
      if (!stopped) {
        state = "closing";
        stopped = closing.then(
          () => {
            state = "stopped";
          },
          (error: unknown) => {
            state = "failed";
            throw error;
          },
        );
        void stopped.catch(() => {});
      }
      return stopped;
    }),
    push = vi.fn((_start: number, samples: Float32Array) => {
      received += samples.length;
    });
  const playback = {
    push,
    stop,
    snapshot: () => ({ state, received, rendered }),
  };
  const open = vi.fn(() => Promise.resolve(playback));
  const c = new TtsController(client, new TtsJournal(s), { socket: () => socket, playback: open });
  return {
    s,
    socket,
    client,
    stop,
    push,
    open,
    c,
    setRendered: (n: number) => {
      rendered = n;
    },
  };
}
async function connected(closing?: Promise<void>) {
  const f = fixture(closing);
  await f.c.create(ttsRequest());
  await f.c.connect();
  f.socket.message(ttsReady());
  await f.c.settled();
  expect(f.c.getSnapshot().stream?.phase).toBe("active");
  return f;
}
describe("TTS controller intent and playback ownership", () => {
  it("retains a cleanup failure during initialization and cannot start another context", async () => {
    const f = await connected();
    f.open.mockRejectedValueOnce(new PlaybackCleanupError());
    await f.c.start("utterance-1");
    expect(f.c.getSnapshot()).toMatchObject({
      playback: { state: "failed" },
      problem: "playback_cleanup",
      starting: false,
    });
    await f.c.refresh();
    await f.c.start("utterance-1");
    expect(f.open).toHaveBeenCalledTimes(1);
    expect(f.socket.sent).toHaveLength(0);
    expect(f.c.getSnapshot().problem).toBe("playback_cleanup");
    f.c.dispose();
  });
  it.each(["resolve", "reject"] as const)(
    "retains local cleanup facts after remote completion (%s)",
    async (outcome) => {
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const closing = new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      });
      const f = await connected(closing);
      await f.c.start("utterance-1");
      f.socket.message(ttsStarted());
      await f.c.settled();
      f.c.cancel();
      f.socket.message(
        ttsEvent(
          "nexus.operation.done",
          {
            kind: "tts",
            status: "cancelled",
            cleanup_state: "complete",
          },
          3,
        ),
      );
      await f.c.settled();
      expect.soft(f.c.getSnapshot().playback?.state).toBe("closing");
      await f.c.start("utterance-2");
      expect.soft(f.open).toHaveBeenCalledTimes(1);
      if (outcome === "resolve") {
        resolve();
        await vi.waitFor(() => expect(f.c.getSnapshot().playback?.state).toBe("stopped"));
        await f.c.start("utterance-2");
        expect(f.open).toHaveBeenCalledTimes(2);
      } else {
        reject(Error("synthetic close failure"));
        await vi.waitFor(() => expect(f.c.getSnapshot().playback?.state).toBe("failed"));
        await f.c.refresh();
        await f.c.start("utterance-2");
        expect(f.open).toHaveBeenCalledTimes(1);
        expect(f.c.getSnapshot().problem).toBe("playback_cleanup");
      }
      f.c.dispose();
    },
  );
  it("I1 immediately stops and sends cancel while start acknowledgement is delayed", async () => {
    const f = await connected();
    await f.c.start("utterance-1");
    f.c.cancel();
    expect(f.socket.sent.map((e) => e.type)).toEqual(["nexus.tts.start", "nexus.tts.cancel"]);
    expect(f.stop).toHaveBeenCalled();
    f.socket.message(ttsStarted());
    await f.c.settled();
    expect(f.c.getSnapshot().stream?.utterance?.phase).toBe("cancelling");
    f.socket.message(
      ttsEvent(
        "nexus.tts.audio.delta",
        { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
        3,
      ),
    );
    await f.c.settled();
    expect(f.push).not.toHaveBeenCalled();
    f.c.dispose();
  });
  it("I3 publishes draining and allows finishing the accepted prefix after REST drain", async () => {
    const f = await connected();
    await f.c.start("utterance-1");
    f.socket.message(ttsStarted());
    await f.c.settled();
    f.c.setText("accepted before drain");
    f.c.append();
    f.socket.message(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 100 }, 3),
    );
    await f.c.settled();
    await f.c.close("drain");
    expect(f.c.getSnapshot().stream?.phase).toBe("draining");
    f.c.commit();
    f.c.finish();
    expect(f.socket.sent.at(-1)?.type).toBe("nexus.tts.finish_text");
    expect(f.c.getSnapshot().problem).toBeNull();
    f.c.dispose();
  });
  it("replays unknown creation with the same key/body and dispatch CAS, refresh never auto-connects", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(Error("lost reply"));
    await f.c.create(ttsRequest());
    const pending = f.c.getSnapshot().record.pending;
    expect(pending?.kind).toBe("create");
    await f.c.replay();
    expect(f.client.create.mock.calls[0]).toEqual(f.client.create.mock.calls[1]);
    await f.c.refresh();
    expect(f.open).not.toHaveBeenCalled();
    expect(f.c.getSnapshot().stream).toBeNull();
    f.c.dispose();
  });
  it("external storage rewrite blocks original replay and preserves pending", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(Error("unknown"));
    await f.c.create(ttsRequest());
    f.s.setItem(ttsJournalKey, "external");
    await f.c.replay();
    expect(f.client.create).toHaveBeenCalledTimes(1);
    expect(f.c.getSnapshot().blocked).toBe(true);
    f.c.dispose();
  });
  it("opens playback on explicit start, sends exact text after ACK, renders independently and navigation aborts", async () => {
    const f = await connected();
    expect(f.c.getSnapshot().current).toBeNull();
    await f.c.start("utterance-1");
    f.socket.message(ttsStarted());
    await f.c.settled();
    f.c.setText("甲😀");
    f.c.append();
    expect(f.socket.sent.at(-1)).toMatchObject({ text: "甲😀", text_seq: 1 });
    f.socket.message(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 80 }, 3),
    );
    await f.c.settled();
    f.c.commit();
    f.socket.message(
      ttsEvent(
        "nexus.tts.audio.delta",
        { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
        4,
      ),
    );
    await f.c.settled();
    expect(f.push).toHaveBeenCalledTimes(1);
    expect(f.c.getSnapshot().playback).toMatchObject({ received: 3, rendered: 0 });
    f.setRendered(3);
    await new Promise((r) => setTimeout(r, 70));
    expect(f.c.getSnapshot().playback?.rendered).toBe(3);
    await f.c.setActive(false);
    expect(f.stop).toHaveBeenCalled();
    expect(f.socket.close).toHaveBeenCalled();
    expect(f.client.cancel).toHaveBeenCalledWith(ttsSession().id, "abort", expect.any(String));
    expect(f.c.getSnapshot().record.text).toBe("甲😀");
    f.c.dispose();
  });
  it("local cancel stops before sending and late audio never reaches playback", async () => {
    const f = await connected();
    await f.c.start("utterance-1");
    f.socket.message(ttsStarted());
    await f.c.settled();
    f.c.cancel();
    expect(f.stop).toHaveBeenCalled();
    expect(f.socket.sent.at(-1)?.type).toBe("nexus.tts.cancel");
    f.socket.message(
      ttsEvent(
        "nexus.tts.audio.delta",
        { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
        3,
      ),
    );
    await f.c.settled();
    expect(f.push).not.toHaveBeenCalled();
    f.c.dispose();
  });
  it("late playback initialization after navigation is stopped and sends no start", async () => {
    const f = await connected();
    let resolve!: (v: Awaited<ReturnType<typeof f.open>>) => void;
    f.open.mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const starting = f.c.start("utterance-1");
    await f.c.setActive(false);
    resolve({
      push: f.push,
      stop: f.stop,
      snapshot: () => ({ state: "running", received: 0, rendered: 0 }),
    });
    await starting;
    expect(f.socket.sent).toHaveLength(0);
    expect(f.stop).toHaveBeenCalled();
    f.c.dispose();
  });
  it("playback overflow fails closed and sends abort while retaining text", async () => {
    const f = await connected();
    await f.c.start("utterance-1");
    f.socket.message(ttsStarted());
    await f.c.settled();
    f.c.setText("keep");
    f.c.append();
    f.socket.message(
      ttsEvent("nexus.tts.text_ack", { accepted_through_text_seq: 1, text_credit: 10 }, 3),
    );
    await f.c.settled();
    f.c.commit();
    f.push.mockImplementationOnce(() => {
      throw Error("full");
    });
    f.socket.message(
      ttsEvent(
        "nexus.tts.audio.delta",
        { audio: ttsPcm(), chunk_seq: 1, start_sample: 0, sample_count: 3 },
        4,
      ),
    );
    await f.c.settled();
    await vi.waitFor(() => expect(f.client.cancel).toHaveBeenCalled());
    expect(f.c.getSnapshot().problem).toBe("protocol");
    expect(f.stop).toHaveBeenCalled();
    expect(f.client.cancel).toHaveBeenCalled();
    expect(f.c.getSnapshot().record.text).toBe("keep");
    f.c.dispose();
  });
});
