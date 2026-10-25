import { describe, expect, it, vi } from "vitest";
import { parse, WorkbenchClient, WorkbenchResponseError } from "../manuscripts/workbench-client";
import { sample } from "../meetings/meeting-fixtures";
import { DeletionJournal, deletionJournalKey, emptyDeletionRecord } from "./deletion-journal";
import { DeletionSession } from "./deletion-session";

function fixture() {
  const data = new Map<string, string>();
  const storage = {
    getItem: vi.fn((k: string) => data.get(k) ?? null),
    setItem: vi.fn((k: string, v: string) => {
      data.set(k, v);
    }),
  };
  const p = parse("DeletionPlan", sample("workbenchCreateDeletionPlan"));
  p.expires_at = "2099-01-01T00:00:00Z";
  const d = parse("Deletion", sample("workbenchCreateDeletion"));
  const project = parse("Project", sample("getProject"));
  const client = new WorkbenchClient(vi.fn());
  vi.spyOn(client, "status").mockResolvedValue({
    ...parse("WorkbenchStatus", sample("getWorkbenchStatus")),
    workbench_enabled: true,
  });
  vi.spyOn(client, "projects").mockResolvedValue({
    object: "nexus.list",
    data: [project],
    has_more: false,
    next_cursor: null,
  });
  vi.spyOn(client, "project").mockResolvedValue(project);
  vi.spyOn(client, "transcripts").mockResolvedValue(
    parse("TranscriptList", sample("listTranscripts")),
  );
  const preview = vi.spyOn(client, "previewDeletion").mockResolvedValue(p);
  const accept = vi.spyOn(client, "acceptDeletion").mockResolvedValue(d);
  const read = vi.spyOn(client, "deletion").mockResolvedValue(d);
  const make = () => new DeletionSession(client, new DeletionJournal(storage));
  return { data, storage, p, d, project, client, make, preview, accept, read };
}
async function selected(f: ReturnType<typeof fixture>) {
  const s = f.make();
  await s.start();
  await s.selectProject(f.project.id);
  return s;
}

describe("explicit deletion intent lifecycle", () => {
  it("retains unknown preview across refresh, then accepted ack when current GET fails", async () => {
    const f = fixture(),
      s = await selected(f);
    f.preview.mockRejectedValueOnce(Error("lost committed response"));
    await s.preview("ordinary");
    const original = s.getSnapshot().record.pending;
    expect(original?.kind).toBe("preview");
    expect(f.storage.setItem).toHaveBeenCalled();
    s.dispose();
    const resumed = f.make();
    await resumed.start();
    expect(f.preview).toHaveBeenCalledTimes(1);
    await resumed.replay();
    expect(f.preview.mock.calls[1]).toEqual([original!.body, original!.key]);
    f.read.mockRejectedValueOnce(Error("offline"));
    await resumed.confirm([]);
    expect(f.accept).not.toHaveBeenCalled();
    await resumed.confirm([f.p.target]);
    expect(resumed.getSnapshot().record.accepted).toEqual(f.d);
    expect(resumed.getSnapshot().current).toBeNull();
    expect(resumed.getSnapshot().problem).toBe("read_failed");
    await resumed.refresh();
    expect(resumed.getSnapshot().current).toEqual(f.d);
  });
  it("blocks network on quota, preserves memory across storage retry and replays exact accept key", async () => {
    const f = fixture(),
      s = await selected(f);
    f.storage.setItem.mockImplementationOnce(() => {
      throw Error("quota");
    });
    await s.preview("ordinary");
    expect(f.preview).not.toHaveBeenCalled();
    expect(s.getSnapshot().blocked).toBe(true);
    const pending = s.getSnapshot().record.pending;
    s.retryStorage();
    await s.replay();
    expect(f.preview.mock.calls[0]).toEqual([pending!.body, pending!.key]);
    f.accept.mockRejectedValueOnce(Error("lost ack"));
    await s.confirm([f.p.target]);
    const intent = s.getSnapshot().record.pending;
    s.reset();
    expect(s.getSnapshot().record.pending).toEqual(intent);
    s.dispose();
    const resumed = f.make();
    await resumed.start();
    await resumed.replay();
    expect(f.accept.mock.calls[1]).toEqual([intent!.body, intent!.key]);
  });
  it("requires a fresh explicit preview after expired plan or a definite 409 rejection", async () => {
    const f = fixture(),
      s = await selected(f);
    await s.preview("ordinary");
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2100-01-01"));
    await s.confirm([f.p.target]);
    expect(f.accept).not.toHaveBeenCalled();
    expect(s.getSnapshot().problem).toBe("expired");
    vi.useRealTimers();
    f.accept.mockRejectedValueOnce(
      new WorkbenchResponseError(409, {
        error: {
          code: "deletion_plan_changed",
          type: "invalid_request_error",
          message: "private",
          param: null,
          request_id: "request-demo",
          retryable: false,
        },
      }),
    );
    await s.confirm([f.p.target]);
    expect(s.getSnapshot().problem).toBe("rejected");
    s.dismissRejected();
    expect(s.getSnapshot().record.pending).toBeNull();
    expect(s.getSnapshot().record.plan).toBeNull();
  });
  it("does not lose accepted evidence if saving the acknowledgement fails", async () => {
    const f = fixture(),
      s = await selected(f);
    await s.preview("ordinary");
    const original = f.storage.setItem.getMockImplementation()!;
    f.storage.setItem.mockImplementation((k, v) => {
      if ((JSON.parse(v) as { accepted: unknown }).accepted) throw Error("quota on ack");
      original(k, v);
    });
    await s.confirm([f.p.target]);
    expect(s.getSnapshot().record.accepted).toEqual(f.d);
    expect(s.getSnapshot().blocked).toBe(true);
    expect(
      (JSON.parse(f.data.get(deletionJournalKey)!) as { pending: { kind: string } }).pending.kind,
    ).toBe("accept");
    expect(f.read).not.toHaveBeenCalled();
    f.storage.setItem.mockImplementation(original);
    s.retryStorage();
    await s.refresh();
    expect(s.getSnapshot().current).toEqual(f.d);
    expect(f.accept).toHaveBeenCalledTimes(1);
  });
  it.each(["preview", "accept"] as const)(
    "revalidates %s recovery before every replay",
    async (kind) => {
      for (const fault of ["corrupt", "concurrent", "read", "quota"] as const) {
        const f = fixture(),
          s = await selected(f);
        const command = kind === "preview" ? f.preview : f.accept;
        if (kind === "accept") await s.preview("ordinary");
        command.mockRejectedValueOnce(Error("lost original response"));
        if (kind === "preview") await s.preview("ordinary");
        else await s.confirm([f.p.target]);
        const intent = s.getSnapshot().record.pending,
          raw = f.data.get(deletionJournalKey)!;
        const calls = command.mock.calls.length;
        if (fault === "corrupt") f.data.set(deletionJournalKey, "{corrupt");
        if (fault === "concurrent")
          f.data.set(deletionJournalKey, JSON.stringify(emptyDeletionRecord()));
        if (fault === "read")
          f.storage.getItem.mockImplementationOnce(() => {
            throw Error("read failed");
          });
        if (fault === "quota")
          f.storage.setItem.mockImplementationOnce(() => {
            throw Error("quota");
          });
        const external = f.data.get(deletionJournalKey);
        await s.replay();
        expect(command.mock.calls.length, kind + ":" + fault).toBe(calls);
        expect(s.getSnapshot().blocked).toBe(true);
        expect(s.getSnapshot().problem).toBe("storage");
        expect(s.getSnapshot().record.pending).toEqual(intent);
        expect(f.data.get(deletionJournalKey)).toBe(external);
        f.data.set(deletionJournalKey, raw);
        s.retryStorage();
        expect(command.mock.calls.length).toBe(calls);
        await s.replay();
        expect(command.mock.calls.length).toBe(calls + 1);
        expect(command.mock.calls.at(-1)).toEqual([intent!.body, intent!.key]);
        s.dispose();
      }
    },
  );
  it("rejects a successful current result with unrelated retained copy claims", async () => {
    const f = fixture(),
      s = await selected(f);
    await s.preview("ordinary");
    await s.confirm([f.p.target]);
    f.read.mockResolvedValueOnce({
      ...f.d,
      status: "succeeded",
      cleanup_state: "complete",
      error: null,
      result: {
        deleted: [f.p.target],
        retained_independent_copies: [
          { ...f.p.target, resource: { id: "foreign-copy", revision: "1" } },
        ],
        ledger_id: "ledger-demo",
        completed_at: "2026-10-24T01:00:00Z",
      },
    });
    await s.refresh();
    expect(s.getSnapshot().current).toBeNull();
    expect(s.getSnapshot().problem).toBe("read_failed");
  });
  it("rejects foreign current records and ignores disposal of an inflight read", async () => {
    const f = fixture(),
      s = await selected(f);
    await s.preview("ordinary");
    await s.confirm([f.p.target]);
    f.read.mockResolvedValueOnce({ ...f.d, plan_id: "foreign" });
    await s.refresh();
    expect(s.getSnapshot().current).toBeNull();
    expect(s.getSnapshot().record.accepted).toEqual(f.d);
    let resolve!: (v: typeof f.d) => void;
    f.read.mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const read = s.refresh();
    const snapshot = s.getSnapshot();
    s.dispose();
    resolve(f.d);
    await read;
    expect(s.getSnapshot()).toBe(snapshot);
  });
});
