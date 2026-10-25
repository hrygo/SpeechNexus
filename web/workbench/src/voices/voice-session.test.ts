import { describe, expect, it, vi } from "vitest";
import { VoiceSession, voiceJournalKey } from "./voice-session";
import { clone, design, job, reference, storage } from "./voice-fixtures";
import { parse, SpeechResponseError } from "../file-workflow/speech-file-client";
function failure(code = "unsupported_parameter", recovery = false) {
  return new SpeechResponseError(
    recovery ? 409 : 422,
    parse("ErrorResponse", {
      error: {
        code,
        type: "invalid_request_error",
        message: "PRIVATE",
        param: "model",
        request_id: "req-test",
        retryable: false,
        ...(recovery
          ? {
              nexus: {
                operation_id: "op-original",
                ...(code === "response_not_replayable"
                  ? {
                      receipt: {
                        receipt_id: "receipt-original",
                        requested: {},
                        effective: {},
                        observed: {},
                        missing: [],
                        unknown: [],
                      },
                    }
                  : {}),
              },
            }
          : {}),
      },
    }),
  );
}
const operation = () => ({
  id: "op-original",
  object: "nexus.operation",
  kind: "voice_preview",
  status: "running",
  cleanup_state: "pending",
  execution_revision: "1",
  job_id: "job-demo",
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
});
function fixture(s = storage()) {
  const client = {
    create: vi.fn().mockResolvedValue(job()),
    getJob: vi.fn().mockResolvedValue(job()),
    getAsset: vi.fn().mockResolvedValue(reference()),
    cancel: vi.fn().mockRejectedValue(new Error("lost")),
    getOperation: vi.fn().mockResolvedValue(operation()),
  };
  const c = new VoiceSession(client, s);
  return { s, client, c };
}
describe("voice durable creation", () => {
  it("blocks correction until a definite rejection's journal removal is saved", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(failure());
    const remove = vi.spyOn(f.s, "removeItem").mockImplementation(() => {
      throw Error("quota");
    });
    await f.c.create(design);
    expect(f.c.snapshot().blocked).toBe(true);
    await f.c.create(clone);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    remove.mockRestore();
    f.c.retryStorage();
    expect(f.c.snapshot().record).toBe(null);
    await f.c.create(design);
    expect(f.client.create).toHaveBeenCalledTimes(2);
  });
  it("restores legacy version1 pending conservatively without automatic I/O", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(Error("lost"));
    await f.c.create(design);
    const legacy = JSON.parse(f.s.getItem(voiceJournalKey)!) as Record<string, unknown>;
    legacy.version = 1;
    delete legacy.operationId;
    f.s.setItem(voiceJournalKey, JSON.stringify(legacy));
    const c = new VoiceSession(f.client, f.s);
    expect(c.snapshot().blocked).toBe(false);
    expect(c.snapshot().record?.operationId).toBe(null);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    f.client.create.mockRejectedValueOnce(failure());
    await c.replay();
    expect(c.snapshot().record?.pending).toBe("create");
    expect((JSON.parse(f.s.getItem(voiceJournalKey)!) as Record<string, unknown>).version).toBe(2);
  });
  it("keeps recovery pending when operation has no linked job", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(failure("operation_in_progress", true));
    await f.c.create(design);
    f.client.getOperation.mockResolvedValue({ ...operation(), job_id: null });
    await f.c.refresh();
    expect(f.client.getJob).not.toHaveBeenCalled();
    expect(f.c.snapshot().record?.pending).toBe("create");
    expect(f.c.canNext()).toBe(false);
  });
  it("allows correction only after the first definite unsupported parameter rejection", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(failure());
    await f.c.create(design);
    expect(f.c.snapshot().record).toBe(null);
    expect(f.c.snapshot().error).toBe("unsupported_parameter");
    await f.c.create({ ...design, model: "corrected-model" });
    expect(f.client.create).toHaveBeenCalledTimes(2);
    expect(f.client.create.mock.calls[1]![1]).not.toBe(f.client.create.mock.calls[0]![1]);
  });
  it.each(["lost", "reload", "receipt-storage"])(
    "preserves the original after %s then a definite rejection",
    async (mode) => {
      const f = fixture();
      if (mode === "receipt-storage") {
        const set = f.s.setItem;
        let n = 0;
        vi.spyOn(f.s, "setItem").mockImplementation((k, v) => {
          if (++n === 3) throw Error("quota");
          set(k, v);
        });
      } else f.client.create.mockRejectedValueOnce(Error("lost"));
      await f.c.create(design);
      const original = f.c.snapshot().record;
      f.c.retryStorage();
      const c = mode === "reload" ? new VoiceSession(f.client, f.s) : f.c;
      f.client.create.mockRejectedValue(failure());
      await c.replay();
      expect(c.snapshot().record?.createKey).toBe(original?.createKey);
      expect(c.snapshot().record?.pending).toBe("create");
      await c.create(clone);
      expect(f.client.create).toHaveBeenCalledTimes(2);
    },
  );
  it.each(["operation_in_progress", "response_not_replayable"])(
    "persists %s and explicitly reads the original operation after reload",
    async (code) => {
      const f = fixture();
      f.client.create.mockRejectedValueOnce(failure(code, true));
      await f.c.create(design);
      expect(
        (JSON.parse(f.s.getItem(voiceJournalKey)!) as Record<string, unknown>).operationId,
      ).toBe("op-original");
      const c = new VoiceSession(f.client, f.s);
      await c.replay();
      expect(f.client.create).toHaveBeenCalledTimes(1);
      expect(f.client.getOperation).not.toHaveBeenCalled();
      await c.refresh();
      expect(f.client.getOperation.mock.calls[0]![0]).toBe("op-original");
      expect(c.snapshot().record?.pending).toBe(null);
      expect(c.snapshot().record?.jobId).toBe("job-demo");
      expect(c.snapshot().current).toBe(true);
    },
  );
  it.each([
    "operation-id",
    "operation-schema",
    "job-kind",
    "job-workspace",
    "job-id",
    "read-failure",
  ])("rejects recovery mismatch %s", async (mode) => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(failure("operation_in_progress", true));
    await f.c.create(design);
    if (mode === "operation-id")
      f.client.getOperation.mockResolvedValue({ ...operation(), id: "foreign" });
    if (mode === "operation-schema")
      f.client.getOperation.mockResolvedValue({ ...operation(), kind: "invented" });
    if (mode === "job-kind") f.client.getJob.mockResolvedValue(job("voice_clone"));
    if (mode === "job-workspace")
      f.client.getJob.mockResolvedValue({ ...job(), workspace_id: "foreign" });
    if (mode === "job-id") f.client.getJob.mockResolvedValue({ ...job(), id: "foreign" });
    if (mode === "read-failure") f.client.getOperation.mockRejectedValue(Error("lost"));
    await f.c.refresh();
    expect(f.c.snapshot().record?.pending).toBe("create");
    expect(f.c.snapshot().current).toBe(false);
    expect(f.c.canNext()).toBe(false);
  });
  it("retains operation recovery across storage failure and preserves pending cancellation after GET", async () => {
    const f = fixture();
    await f.c.create(design);
    await f.c.refresh();
    f.client.cancel.mockRejectedValueOnce(failure("operation_in_progress", true));
    // The first two saves are cancellation barriers; the third stores recovery identity.
    const original = f.s.setItem;
    let n = 0;
    vi.spyOn(f.s, "setItem").mockImplementation((k, v) => {
      if (++n === 3) throw Error("quota");
      original(k, v);
    });
    await f.c.cancel();
    expect(f.c.snapshot().blocked).toBe(true);
    f.c.retryStorage();
    expect((JSON.parse(f.s.getItem(voiceJournalKey)!) as Record<string, unknown>).operationId).toBe(
      "op-original",
    );
    f.client.getJob.mockResolvedValue({ ...job(), status: "cancelled", cleanup_state: "complete" });
    await f.c.refresh();
    expect(f.c.snapshot().record?.pending).toBe("cancel");
    expect(f.c.canNext()).toBe(false);
  });
  it("creates exact design once, leaves current GET separate and freezes input", async () => {
    const f = fixture();
    await f.c.create(design);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    expect(f.client.create.mock.calls[0]![0]).toEqual(design);
    expect(f.c.snapshot().current).toBe(false);
    expect(f.c.snapshot().record?.pending).toBe(null);
    expect(() => {
      f.c.snapshot().record!.command.input = {} as never;
    }).toThrow();
  });
  it("restores unknown create without automatic requests and replays original key/body", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(new Error("lost"));
    await f.c.create(design);
    const old = f.client.create.mock.calls[0]!;
    expect(f.c.snapshot().record?.pending).toBe("create");
    const r = new VoiceSession(f.client, f.s);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    await r.create(clone);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    await r.replay();
    expect(f.client.create.mock.calls[1]!.slice(0, 2)).toEqual(old.slice(0, 2));
  });
  it("checks clone reference before first POST, but original replay ignores later expiry", async () => {
    const f = fixture();
    f.client.create.mockRejectedValueOnce(new Error("lost"));
    await f.c.create(clone);
    expect(f.client.getAsset).toHaveBeenCalledTimes(1);
    f.client.create.mockResolvedValue(job("voice_clone"));
    f.client.getAsset.mockRejectedValue(new Error("expired"));
    await f.c.replay();
    expect(f.client.getAsset).toHaveBeenCalledTimes(1);
    expect(f.c.snapshot().record?.jobId).toBe("job-demo");
  });
  it("does not dispatch clone with wrong material", async () => {
    const f = fixture();
    f.client.getAsset.mockResolvedValue({ ...reference(), purpose: "asr_input" });
    await f.c.create(clone);
    expect(f.client.create).not.toHaveBeenCalled();
    expect(f.c.snapshot().record).toBe(null);
  });
  it("storage failure blocks every write until explicit retry", async () => {
    const f = fixture();
    const save = vi.spyOn(f.s, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    await f.c.create(design);
    expect(f.client.create).not.toHaveBeenCalled();
    expect(f.c.snapshot().blocked).toBe(true);
    save.mockRestore();
    f.c.retryStorage();
    await f.c.create(design);
    expect(f.client.create).toHaveBeenCalledTimes(1);
  });
  it("foreign storage overwrite blocks replay and next", async () => {
    const f = fixture();
    f.client.create.mockRejectedValue(new Error("lost"));
    await f.c.create(design);
    f.s.setItem(voiceJournalKey, "foreign");
    await f.c.replay();
    expect(f.client.create).toHaveBeenCalledTimes(1);
    expect(f.c.snapshot().blocked).toBe(true);
    f.c.next();
    expect(f.s.getItem(voiceJournalKey)).toBe("foreign");
  });
  it.each([{ kind: "asr" }, { workspace_id: "foreign" }])(
    "rejects wrong create response %#",
    async (change) => {
      const f = fixture();
      f.client.create.mockResolvedValue({ ...job(), ...change });
      await f.c.create(design);
      expect(f.c.snapshot().record?.pending).toBe("create");
      expect(f.c.snapshot().job).toBe(null);
    },
  );
  it("requires a successful current read and preserves original cancellation key", async () => {
    const f = fixture();
    await f.c.create(design);
    await f.c.cancel();
    expect(f.client.cancel).not.toHaveBeenCalled();
    await f.c.refresh();
    await f.c.cancel();
    const original = f.client.cancel.mock.calls[0]!;
    expect(f.c.snapshot().record?.pending).toBe("cancel");
    const r = new VoiceSession(f.client, f.s);
    await r.replay();
    expect(f.client.cancel.mock.calls[1]!.slice(0, 2)).toEqual(original.slice(0, 2));
  });
  it("failed GET cannot authorize next or cancel", async () => {
    const f = fixture();
    await f.c.create(design);
    await f.c.refresh();
    f.client.getJob.mockRejectedValue(new Error("lost"));
    await f.c.refresh();
    expect(f.c.snapshot().current).toBe(false);
    await f.c.cancel();
    f.c.next();
    expect(f.client.cancel).not.toHaveBeenCalled();
    expect(f.c.snapshot().record).not.toBe(null);
  });
  it("invalid terminal cleanup cannot authorize next, verified complete terminal can", async () => {
    const f = fixture();
    await f.c.create(design);
    f.client.getJob.mockResolvedValue({ ...job(), status: "cancelled", cleanup_state: "unknown" });
    await f.c.refresh();
    f.c.next();
    expect(f.c.snapshot().record).not.toBe(null);
    f.client.getJob.mockResolvedValue({ ...job(), status: "cancelled", cleanup_state: "complete" });
    await f.c.refresh();
    f.c.next();
    expect(f.c.snapshot().record).toBe(null);
    expect(f.s.getItem(voiceJournalKey)).toBe(null);
  });
  it("corrupt recovery never silently replaces an intent", async () => {
    const s = storage();
    s.setItem(voiceJournalKey, "broken");
    const f = fixture(s);
    await f.c.create(design);
    expect(f.client.create).not.toHaveBeenCalled();
    expect(f.c.snapshot().blocked).toBe(true);
  });
  it("suspending ignores late create/read results and does not auto dispatch on return", async () => {
    const f = fixture();
    let resolve!: (x: ReturnType<typeof job>) => void;
    f.client.create.mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const pending = f.c.create(design);
    f.c.setActive(false);
    resolve(job());
    await pending;
    expect(f.c.snapshot().record?.pending).toBe("create");
    f.c.setActive(true);
    expect(f.client.create).toHaveBeenCalledTimes(1);
  });
});
