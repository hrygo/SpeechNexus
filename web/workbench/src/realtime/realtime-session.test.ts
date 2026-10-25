import { describe, expect, it, vi } from "vitest";
import { RealtimeController, type RealtimeSocket } from "./realtime-session";
import { RealtimeJournal, realtimeJournalKey } from "./realtime-journal";
import { request, session, storage, created, ready, event } from "./realtime-fixtures";
import type { MicrophoneCaptureOptions } from "../media/microphone-capture";
import type { RealtimeClient } from "./realtime-client";

class Socket extends EventTarget implements RealtimeSocket {
  readyState = 1;
  bufferedAmount = 0;
  sent: unknown[] = [];
  send(value: string) {
    this.sent.push(JSON.parse(value));
  }
  close = vi.fn(() => {
    this.readyState = 3;
    this.dispatchEvent(new Event("close"));
  });
  message(value: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
function fixture() {
  const s = storage(),
    socket = new Socket();
  const client = {
    create: vi.fn<RealtimeClient["create"]>().mockResolvedValue(session()),
    current: vi.fn<RealtimeClient["current"]>().mockResolvedValue(session()),
    cancel: vi
      .fn<RealtimeClient["cancel"]>()
      .mockResolvedValue({ ...session(), status: "draining", cleanup_state: "pending" }),
  };
  const done = deferred<void>(),
    stop = vi.fn(() => {
      done.resolve();
      return Promise.resolve();
    });
  let options: MicrophoneCaptureOptions | undefined;
  const capture = vi.fn((o: MicrophoneCaptureOptions) => {
    options = o;
    return Promise.resolve({
      token: "capture-demo",
      sampleRate: 24000,
      closed: done.promise,
      stop,
      snapshot: { state: "running" as const, reason: undefined },
    });
  });
  const c = new RealtimeController(client, new RealtimeJournal(s), {
    socket: () => socket,
    capture,
  });
  return {
    s,
    socket,
    client,
    c,
    capture,
    stop,
    get options() {
      return options;
    },
  };
}
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};
async function connected() {
  const f = fixture();
  await f.c.create(request());
  await f.c.connect();
  f.socket.message(created());
  f.socket.message(ready());
  return f;
}
describe("realtime session original intent and media ownership", () => {
  it("I1 ignores queued or repeated empty commits without aborting accepted work", async () => {
    const f = await connected();
    await f.c.startCapture();
    const frame = (startSample: number) => ({
      token: "capture-demo",
      sampleRate: 24000,
      startSample,
      samples: new Float32Array(2400),
    });
    await f.options!.onFrame(frame(0), new AbortController().signal);
    await Promise.all([f.c.commit(), f.c.commit()]);
    await f.c.commit();
    expect(
      f.socket.sent.filter((e) => (e as { type: string }).type === "input_audio_buffer.commit"),
    ).toHaveLength(1);
    expect(f.client.cancel).not.toHaveBeenCalled();
    expect(f.c.getSnapshot().stream).toMatchObject({ phase: "active", uncommitted: 0 });
    await f.options!.onFrame(frame(2400), new AbortController().signal);
    await f.c.commit();
    expect(
      f.socket.sent.filter((e) => (e as { type: string }).type === "input_audio_buffer.commit"),
    ).toHaveLength(2);
    expect(f.client.cancel).not.toHaveBeenCalled();
    f.c.dispose();
  });
  it("I2 orders final drafts by committed input despite reversed finals, empty text and duplicates", async () => {
    const f = await connected();
    f.socket.message({
      type: "input_audio_buffer.committed",
      event_id: "commit-first",
      item_id: "item-first",
      previous_item_id: null,
    });
    f.socket.message({
      type: "input_audio_buffer.committed",
      event_id: "commit-second",
      item_id: "item-second",
      previous_item_id: "item-first",
    });
    const second = {
      type: "conversation.item.input_audio_transcription.completed",
      event_id: "final-second",
      item_id: "item-second",
      content_index: 0,
      transcript: "第二句",
      usage: { type: "duration", seconds: 0.1 },
    };
    f.socket.message(second);
    f.socket.message({ ...second, event_id: "final-first", item_id: "item-first", transcript: "" });
    f.socket.message(second);
    expect(f.c.getSnapshot().record.captions.map((c) => [c.itemId, c.text])).toEqual([
      ["item-first", ""],
      ["item-second", "第二句"],
    ]);
    expect(new RealtimeJournal(f.s).load().captions).toEqual(f.c.getSnapshot().record.captions);
    f.c.dispose();
  });
  it("I3 journals a distinct abort after accepted drain and replays its original key", async () => {
    const f = await connected();
    await f.c.close("drain");
    const drainKey = f.client.cancel.mock.calls[0]![2];
    f.client.cancel.mockRejectedValueOnce(Error("lost-abort-reply"));
    await f.c.close("abort");
    expect(f.client.cancel.mock.calls.map((c) => c[1])).toEqual(["drain", "abort"]);
    const pending = f.c.getSnapshot().record.pending!;
    expect(pending).toMatchObject({ kind: "cancel", mode: "abort" });
    expect(pending.key).not.toBe(drainKey);
    await f.c.replay();
    expect(f.client.cancel.mock.calls[2]![2]).toBe(pending.key);
    expect(f.c.getSnapshot().record.session?.cleanup_state).toBe("pending");
    f.c.dispose();
  });
  it("I3 never replaces an unknown drain with a new abort intent", async () => {
    const f = await connected();
    f.client.cancel.mockRejectedValueOnce(Error("lost-drain-reply"));
    await f.c.close("drain");
    const pending = f.c.getSnapshot().record.pending;
    await f.c.close("abort");
    expect(f.c.getSnapshot().record.pending).toEqual(pending);
    expect(f.client.cancel).toHaveBeenCalledTimes(1);
    f.c.dispose();
  });
  it("keeps accepted creation in memory after ack quota failure and recovers only on explicit storage retry", async () => {
    const f = fixture(),
      save = f.s.setItem;
    let calls = 0;
    f.s.setItem = (k, v) => {
      if (++calls === 3) throw Error("quota");
      save(k, v);
    };
    await f.c.create(request());
    expect(f.c.getSnapshot()).toMatchObject({
      blocked: true,
      problem: "storage",
      record: { session: { id: "session-demo" }, pending: null },
    });
    expect(new RealtimeJournal(f.s).load().pending?.kind).toBe("create");
    await f.c.connect();
    expect(f.client.current).not.toHaveBeenCalled();
    f.c.retryStorage();
    expect(f.c.getSnapshot().blocked).toBe(false);
    expect(new RealtimeJournal(f.s).load().pending).toBeNull();
    expect(f.client.create).toHaveBeenCalledOnce();
  });
  it("stops input and preserves final in memory on quota failure while navigating", async () => {
    const f = await connected();
    const save = f.s.setItem;
    f.s.setItem = () => {
      throw Error("quota");
    };
    f.socket.message({
      type: "conversation.item.input_audio_transcription.completed",
      event_id: "final-quota",
      item_id: "item-quota",
      content_index: 0,
      transcript: "精确保留 🗣️",
      usage: { type: "duration", seconds: 0.1 },
    });
    expect(f.c.getSnapshot()).toMatchObject({
      blocked: true,
      record: { captions: [{ text: "精确保留 🗣️" }] },
    });
    f.c.setActive(false);
    f.c.setActive(true);
    expect(f.c.getSnapshot().record.captions[0]?.text).toBe("精确保留 🗣️");
    expect(f.client.cancel).not.toHaveBeenCalled();
    f.s.setItem = save;
    f.c.retryStorage();
    expect(new RealtimeJournal(f.s).load().captions[0]?.text).toBe("精确保留 🗣️");
    f.c.dispose();
  });
  it("does not publish a delayed GET after dispose", async () => {
    const f = fixture();
    await f.c.create(request());
    const pending = deferred<ReturnType<typeof session>>();
    f.client.current.mockReturnValueOnce(pending.promise);
    const reading = f.c.refresh();
    f.c.dispose();
    const before = f.c.getSnapshot();
    pending.resolve(session());
    await reading;
    expect(f.c.getSnapshot()).toBe(before);
  });
  it("fails closed without sending to an overfull socket", async () => {
    const f = await connected();
    await f.c.startCapture();
    f.socket.bufferedAmount = 400000;
    await expect(
      f.options!.onFrame(
        {
          token: "capture-demo",
          sampleRate: 24000,
          startSample: 0,
          samples: new Float32Array(2400),
        },
        new AbortController().signal,
      ),
    ).rejects.toThrow();
    expect(f.socket.sent).toHaveLength(0);
    await f.c.close("abort");
    expect(f.c.getSnapshot().stream).toMatchObject({
      sent: 0,
      accepted: 0,
      credit: 24000,
      uncommitted: 0,
    });
    f.c.dispose();
  });
  it("keeps the actual sent watermark when socket.send throws before enqueueing", async () => {
    const f = await connected();
    await f.c.startCapture();
    vi.spyOn(f.socket, "send").mockImplementationOnce(() => {
      throw new Error("synthetic send failure");
    });
    await expect(
      f.options!.onFrame(
        {
          token: "capture-demo",
          sampleRate: 24000,
          startSample: 0,
          samples: new Float32Array(2400),
        },
        new AbortController().signal,
      ),
    ).rejects.toThrow();
    expect(f.socket.sent).toHaveLength(0);
    await f.c.close("abort");
    expect(f.c.getSnapshot().stream).toMatchObject({
      sent: 0,
      accepted: 0,
      credit: 24000,
      uncommitted: 0,
    });
    expect(
      f.c.getSnapshot().stream?.gaps.every((gap) => gap.span === null || gap.span.end === 0),
    ).toBe(true);
    f.c.dispose();
  });
  it("stops the microphone but keeps the socket for drain results and pending cleanup", async () => {
    const f = await connected();
    await f.c.startCapture();
    await f.c.close("drain");
    expect(f.stop).toHaveBeenCalledOnce();
    expect(f.socket.close).not.toHaveBeenCalled();
    expect(f.client.cancel).toHaveBeenCalledWith("session-demo", "drain", expect.any(String));
    await f.c.startCapture();
    expect(f.capture).toHaveBeenCalledTimes(1);
    expect(f.c.getSnapshot().stream?.phase).toBe("draining");
    f.c.dispose();
  });
  it("persists before every create/replay and blocks changed recovery state", async () => {
    const f = fixture();
    f.client.create.mockRejectedValue(Error("lost committed reply"));
    await f.c.create(request());
    const pending = f.c.getSnapshot().record.pending;
    expect(pending?.kind).toBe("create");
    f.s.setItem(realtimeJournalKey, "corrupt");
    await f.c.replay();
    expect(f.client.create).toHaveBeenCalledTimes(1);
    expect(f.c.getSnapshot()).toMatchObject({
      blocked: true,
      problem: "storage",
      record: { pending },
    });
  });
  it("reuses original create/cancel keys after an unknown outcome, never declares cleanup", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(Error("lost"));
    await f.c.create(request());
    const original = f.c.getSnapshot().record.pending!;
    await f.c.replay();
    expect(f.client.create.mock.calls.map((x) => x[1])).toEqual([original.key, original.key]);
    f.client.cancel.mockRejectedValueOnce(Error("lost"));
    await f.c.close("abort");
    const cancel = f.c.getSnapshot().record.pending!;
    await f.c.replay();
    expect(f.client.cancel.mock.calls.map((x) => x[2])).toEqual([cancel.key, cancel.key]);
    expect(f.c.getSnapshot().record.session?.cleanup_state).toBe("pending");
    expect(f.c.getSnapshot().current).toBeNull();
  });
  it("requires admission before capture; sends only bounded frames and stops on navigation", async () => {
    const f = fixture();
    await f.c.create(request());
    await f.c.connect();
    await f.c.startCapture();
    expect(f.capture).not.toHaveBeenCalled();
    f.socket.message(created());
    f.socket.message(ready());
    await f.c.startCapture();
    expect(f.capture).toHaveBeenCalledOnce();
    await f.options!.onFrame(
      { token: "capture-demo", sampleRate: 24000, startSample: 0, samples: new Float32Array(2400) },
      new AbortController().signal,
    );
    expect(f.socket.sent[0]).toMatchObject({ type: "input_audio_buffer.append" });
    expect(f.c.getSnapshot().stream?.sent).toBe(2400);
    f.c.setActive(false);
    await flush();
    expect(f.stop).toHaveBeenCalledOnce();
    expect(f.socket.close).toHaveBeenCalled();
    expect(f.client.cancel).toHaveBeenCalledOnce();
    expect(f.c.getSnapshot().stream?.phase).toBe("disconnected");
    const n = f.socket.sent.length;
    await expect(
      f.options!.onFrame(
        {
          token: "capture-demo",
          sampleRate: 24000,
          startSample: 2400,
          samples: new Float32Array(2400),
        },
        new AbortController().signal,
      ),
    ).rejects.toThrow();
    expect(f.socket.sent).toHaveLength(n);
  });
  it("stops late capture ownership after dispose and ignores late reads", async () => {
    const f = await connected(),
      late = deferred<Awaited<ReturnType<typeof f.capture>>>();
    f.capture.mockReturnValue(late.promise);
    const starting = f.c.startCapture();
    f.c.dispose();
    late.resolve({
      token: "late",
      sampleRate: 24000,
      closed: new Promise(() => undefined),
      stop: f.stop,
      snapshot: { state: "running", reason: undefined },
    });
    await starting;
    expect(f.stop).toHaveBeenCalledOnce();
    const snapshot = f.c.getSnapshot();
    await flush();
    expect(f.c.getSnapshot()).toBe(snapshot);
  });
  it("persists final including empty text; required failure doesn't erase it and bad epoch disconnects", async () => {
    const f = await connected();
    f.socket.message(
      event(
        "nexus.transcription.hypothesis",
        {
          operation_id: "operation-demo",
          segment_id: "segment-demo",
          item_id: "item-demo",
          revision: 1,
          text: "replace me",
          sample_span: { start: 0, end: 2400 },
        },
        2,
      ),
    );
    f.socket.message({
      type: "conversation.item.input_audio_transcription.completed",
      event_id: "final-demo",
      item_id: "item-demo",
      content_index: 0,
      transcript: "",
      usage: { type: "duration", seconds: 0.1 },
    });
    f.socket.message(
      event(
        "nexus.operation.done",
        {
          operation_id: "operation-demo",
          kind: "asr",
          status: "failed",
          cleanup_state: "quarantined",
          reason: "required_auxiliary_failed",
        },
        3,
      ),
    );
    expect(new RealtimeJournal(f.s).load().captions).toEqual([
      { sessionId: "session-demo", itemId: "item-demo", text: "" },
    ]);
    expect(f.c.getSnapshot().stream?.segments[0]?.operation?.status).toBe("failed");
    f.socket.message({
      ...event("nexus.session.gap", { reason: "epoch_change" }, 4),
      stream_epoch: 2,
    });
    await flush();
    expect(f.c.getSnapshot().stream?.phase).toBe("disconnected");
    expect(f.client.cancel).toHaveBeenCalledOnce();
    expect(f.c.getSnapshot().problem).toBe("protocol");
  });
  it("does not connect or request a device when restored from storage", async () => {
    const f = await connected(),
      socket = vi.fn(() => new Socket()),
      capture = vi.fn(f.capture);
    new RealtimeController(f.client, new RealtimeJournal(f.s), { socket, capture });
    expect(socket).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
  });
});
