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
