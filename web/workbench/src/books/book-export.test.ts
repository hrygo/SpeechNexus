import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchClient, WorkbenchResponseError, parse } from "../manuscripts/workbench-client";
import { BookExportSession, isTerminal } from "./book-export";

const book = parse("Book", {
  object: "nexus.book",
  id: "book_demo",
  project_id: "project_demo",
  title: "合成有声书",
  status: "draft",
  casting_revision: "3",
  created_at: "2026-10-24T12:00:00Z",
  updated_at: "2026-10-24T12:00:00Z",
});

function job(overrides: Record<string, unknown> = {}) {
  return parse("ExportJob", {
    object: "nexus.export_job",
    id: "export_demo",
    book_id: book.id,
    status: "queued",
    format: "zip",
    manifest_revision: book.casting_revision,
    progress_completed: 0,
    progress_total: 2,
    artifact: null,
    error: null,
    created_at: "2026-10-24T12:00:00Z",
    updated_at: "2026-10-24T12:00:00Z",
    ...overrides,
  });
}

function refusal(status: number, code: string, retryable: boolean) {
  return new WorkbenchResponseError(status, {
    error: {
      code,
      type: "invalid_request_error",
      message: "private-synthetic",
      param: null,
      request_id: "request-demo",
      retryable,
    },
  });
}

function setup() {
  const client = new WorkbenchClient(vi.fn());
  const create = vi.spyOn(client, "createBookExport").mockResolvedValue(job());
  const read = vi.spyOn(client, "bookExport").mockResolvedValue(job());
  const cancel = vi.spyOn(client, "cancelBookExport").mockResolvedValue(job());
  const session = new BookExportSession(client, book, { pollMs: 10 });
  return { client, create, read, cancel, session };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("book export session", () => {
  it("starts the exact pinned intent and asks for the requested revision only", async () => {
    const { create, session } = setup();
    await session.start("zip", "key-demo");
    expect(create).toHaveBeenCalledWith(
      "book_demo",
      { expected_casting_revision: "3", format: "zip" },
      "key-demo",
    );
    expect(session.getSnapshot().job?.status).toBe("queued");
  });

  it("polls a queued export through progress to a frozen terminal state", async () => {
    vi.useFakeTimers();
    const { read, session } = setup();
    read
      .mockResolvedValueOnce(job({ status: "running", progress_completed: 1 }))
      .mockResolvedValueOnce(
        job({
          status: "succeeded",
          progress_completed: 2,
          artifact: {
            object: "nexus.export_artifact",
            id: "artifact_demo",
            export_id: "export_demo",
            manifest_revision: "3",
            media_type: "application/zip",
            bytes: 2048,
            sha256: "sha256:" + "ab".repeat(32),
            created_at: "2026-10-24T12:00:05Z",
          },
        }),
      );

    await session.start("zip", "key-demo");
    await vi.advanceTimersByTimeAsync(10);
    expect(session.getSnapshot().job?.progress_completed).toBe(1);
    await vi.advanceTimersByTimeAsync(10);
    const done = session.getSnapshot();
    expect(done.job?.status).toBe("succeeded");
    expect(done.job?.artifact?.sha256).toBe("sha256:" + "ab".repeat(32));
    expect(done.watching).toBe(false);

    // A terminal export is never polled again.
    await vi.advanceTimersByTimeAsync(100);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("refuses to move progress backwards or to rewrite a finished export", async () => {
    vi.useFakeTimers();
    const { read, session } = setup();
    read
      .mockResolvedValueOnce(job({ status: "running", progress_completed: 2 }))
      .mockResolvedValueOnce(job({ status: "running", progress_completed: 1 }));
    await session.start("zip", "key-demo");
    await vi.advanceTimersByTimeAsync(10);
    expect(session.getSnapshot().job?.progress_completed).toBe(2);
    await vi.advanceTimersByTimeAsync(10);
    const state = session.getSnapshot();
    expect(state.problem).toBe("unknown");
    expect(state.job?.progress_completed).toBe(2);
    expect(state.watching).toBe(false);
  });

  it("keeps the accepted cancellation and stops watching once settled", async () => {
    const { create, cancel, session } = setup();
    create.mockResolvedValue(job({ format: "mp3" }));
    await session.start("mp3", "key-demo");
    cancel.mockResolvedValueOnce(job({ format: "mp3", status: "cancelling" }));
    await session.cancel("cancel-key");
    expect(cancel).toHaveBeenCalledWith("export_demo", "cancel-key");
    expect(session.getSnapshot().job?.status).toBe("cancelling");
  });

  it("refuses a cancellation answer that describes a different export", async () => {
    const { cancel, session } = setup();
    await session.start("zip", "key-demo");
    cancel.mockResolvedValueOnce(job({ format: "mp3", status: "cancelling" }));
    await session.cancel("cancel-key");
    const state = session.getSnapshot();
    expect(state.problem).toBe("unknown");
    // The mismatched answer is discarded instead of replacing what we know.
    expect(state.job?.format).toBe("zip");
    expect(state.job?.status).toBe("queued");
  });

  it("maps refusals to a problem without inventing a job", async () => {
    const { create, session } = setup();
    create.mockRejectedValueOnce(
      new WorkbenchResponseError(409, {
        error: {
          code: "revision_conflict",
          type: "invalid_request_error",
          message: "private-synthetic",
          param: null,
          request_id: "request-demo",
          retryable: false,
        },
      }),
    );
    await session.start("zip", "key-demo");
    const state = session.getSnapshot();
    expect(state.problem).toBe("conflict");
    expect(state.job).toBeNull();
    expect(state.busy).toBe(false);
  });

  it("writes the category the contract states and reads it back after a reload", async () => {
    // #803: the export route publishes no per-code operator vocabulary, so the category
    // comes from the envelope's own retry permission instead of a second code table.
    const { client, create, session } = setup();
    create.mockRejectedValueOnce(refusal(422, "invalid_change", false));
    await session.start("zip", "key-demo");
    expect(session.getSnapshot()).toMatchObject({ refused: "terminal", busy: false });
    // A reload sees the persisted category, not a guess from the status, and it no longer
    // reports "结果尚未确认，请按原键重放" for a request the server already answered: the
    // aside words the refusal, and the alert stays out of it (#803).
    const restored = new BookExportSession(client, book, { pollMs: 10 });
    expect(restored.getSnapshot()).toMatchObject({ refused: "terminal", problem: null });
    expect(restored.getSnapshot().pending).toMatchObject({ key: "key-demo" });
  });

  it("keeps a refusal the contract calls retryable, whatever its status", async () => {
    // The field decides, not the status family: a retryable 4xx would keep the key, and a
    // status-only reading would spend it. The workbench publishes no such disposition for
    // this route today, which is exactly why the panel must not hard-code "4xx = spent".
    const { create, session } = setup();
    create.mockRejectedValueOnce(refusal(409, "revision_conflict", true));
    await session.start("zip", "key-demo");
    expect(session.getSnapshot().refused).toBe("retryable");
  });

  it("does not re-send a key whose refusal the record withholds", async () => {
    // Twice: the record is a settlement, and the path is guarded in the session, not only
    // by the panel's `disabled` attribute (#803, the two layers of #794).
    const { create, session } = setup();
    create.mockRejectedValueOnce(refusal(422, "invalid_change", false));
    await session.start("zip", "key-demo");
    expect(session.getSnapshot().refused).toBe("terminal");
    await session.replay();
    expect(create).toHaveBeenCalledTimes(1);
  });

  it("restores a record written before #803 as unclassified and withholds the replay", async () => {
    const { client, create } = setup();
    sessionStorage.setItem(
      "speechnexus.workbench.exports.book_demo.pending.v1",
      JSON.stringify({
        version: 1,
        command: {
          kind: "create",
          bookId: book.id,
          body: { expected_casting_revision: book.casting_revision, format: "zip" },
          key: "legacy-key",
        },
        uncertain: false,
        rejected: true,
      }),
    );
    const restored = new BookExportSession(client, book, { pollMs: 10 });
    expect(restored.getSnapshot()).toMatchObject({ refused: "unclassified", problem: null });
    await restored.replay();
    expect(create).not.toHaveBeenCalled();
  });

  it("stops updating after disposal and reports terminal states", async () => {
    vi.useFakeTimers();
    const { read, session } = setup();
    await session.start("zip", "key-demo");
    session.dispose();
    await vi.advanceTimersByTimeAsync(100);
    expect(read).not.toHaveBeenCalled();
    expect(isTerminal("succeeded")).toBe(true);
    expect(isTerminal("running")).toBe(false);
  });
});

beforeEach(() => sessionStorage.clear());
