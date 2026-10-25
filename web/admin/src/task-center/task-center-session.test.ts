// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse, type FileContracts } from "../../../../packages/contracts/src";
import { TaskCenterSession } from "./task-center-session";
import type { TaskApi } from "./job-client";

const job = (id = "job-demo") =>
  parse("Job", {
    ...structuredClone(fixtures.examples.find((e) => e.id === "queued-job")!.value),
    id,
  });
const cancellation = () =>
  parse(
    "JobCancellation",
    structuredClone(fixtures.examples.find((e) => e.id === "cancel-job")!.value),
  );
const page = (data = [job()]) =>
  parse("JobList", { object: "nexus.list", data, has_more: false, next_cursor: null });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
function fixture() {
  const listMock = vi.fn(() => Promise.resolve(page()));
  const cancelMock = vi.fn(() => Promise.resolve(cancellation()));
  const api: TaskApi = {
    list: listMock,
    getJob: (id) => Promise.resolve(job(id)),
    cancel: cancelMock,
  };
  return { api, session: new TaskCenterSession(api), listMock, cancelMock };
}

describe("Admin task snapshots and cancellation", () => {
  it("retains the prior list as stale when a refresh fails", async () => {
    const { api, session } = fixture();
    await session.load();
    api.list = () => Promise.reject(new Error("synthetic-sensitive-value"));
    await session.load();
    expect(session.getSnapshot().page?.data[0]?.id).toBe("job-demo");
    expect(session.getSnapshot().listPhase).toBe("error");
    expect(session.getSnapshot().notice).not.toContain("synthetic-sensitive-value");
  });

  it("aborts old list reads and ignores their late response", async () => {
    const { api, session } = fixture();
    const first = deferred<FileContracts["JobList"]>();
    let oldSignal: AbortSignal | undefined;
    api.list = vi.fn((_cursor: string | undefined, signal: AbortSignal | undefined) => {
      oldSignal ??= signal;
      return first.promise;
    });
    const old = session.load();
    api.list = () => Promise.resolve(page([job("job-new")]));
    await session.load("next-page");
    first.resolve(page());
    await old;
    expect(oldSignal?.aborted).toBe(true);
    expect(session.getSnapshot().page?.data[0]?.id).toBe("job-new");
  });

  it("does not show old detail under a new selected identity", async () => {
    const { api, session } = fixture();
    const first = deferred<FileContracts["Job"]>();
    let oldSignal: AbortSignal | undefined;
    api.getJob = (_id, signal) => {
      oldSignal = signal;
      return first.promise;
    };
    const old = session.select("job-old");
    api.getJob = (id) => Promise.resolve(job(id));
    await session.select("job-new");
    first.resolve(job("job-old"));
    await old;
    expect(oldSignal?.aborted).toBe(true);
    expect(session.getSnapshot().detail?.id).toBe("job-new");
  });

  it("keeps acceptance separate from status and resource release", async () => {
    const { session } = fixture();
    await session.select("job-demo");
    await session.cancelSelected();
    expect(session.getSnapshot().cancellation?.response?.accepted).toBe(true);
    expect(session.getSnapshot().detail?.status).toBe("queued");
    expect(session.getSnapshot().detail?.cleanup_state).toBe("pending");
  });

  it("replays the original cancel key and target even after another job is selected", async () => {
    const { api, session } = fixture();
    const calls: { id: string; key: string }[] = [];
    api.cancel = (id, key) => {
      calls.push({ id, key });
      if (calls.length === 1) return Promise.reject(new Error("lost response"));
      return Promise.resolve(cancellation());
    };
    await session.select("job-demo");
    await session.cancelSelected();
    expect(session.getSnapshot().cancellation?.phase).toBe("unknown");
    await session.select("job-other");
    await session.cancelSelected();
    expect(calls).toHaveLength(1);
    await session.replayCancellation();
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual(calls[0]);
    expect(session.getSnapshot().detail?.id).toBe("job-other");
  });

  it("does not duplicate an in-flight cancel or overwrite a newly selected detail", async () => {
    const { api, session } = fixture();
    const response = deferred<FileContracts["JobCancellation"]>();
    const pendingCancel = vi.fn(() => response.promise);
    api.cancel = pendingCancel;
    await session.select("job-demo");
    const pending = session.cancelSelected();
    await session.cancelSelected();
    await session.select("job-other");
    response.resolve(cancellation());
    await pending;
    expect(pendingCancel).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().cancellation?.jobId).toBe("job-demo");
    expect(session.getSnapshot().detail?.id).toBe("job-other");
  });

  it("does not issue cancellation for terminal or unread detail", async () => {
    const { api, session, cancelMock } = fixture();
    await session.cancelSelected();
    api.getJob = () =>
      Promise.resolve(
        parse("Job", {
          ...job(),
          status: "failed",
          error: parse(
            "JobEvent",
            structuredClone(fixtures.examples.find((e) => e.id === "job.failed")!.value),
          ).data.error,
          cleanup_state: "quarantined",
        }),
      );
    await session.select("job-demo");
    await session.cancelSelected();
    expect(cancelMock).not.toHaveBeenCalled();
    expect(session.getSnapshot().detail?.cleanup_state).toBe("quarantined");
  });

  it("aborts owned reads on dispose and ignores late completion", async () => {
    const { api, session, listMock } = fixture();
    const response = deferred<FileContracts["Job"]>();
    let signal: AbortSignal | undefined;
    api.getJob = (_id, s) => {
      signal = s;
      return response.promise;
    };
    const changed = vi.fn();
    session.subscribe(changed);
    const pending = session.select("job-demo");
    session.dispose();
    const count = changed.mock.calls.length;
    response.resolve(job());
    await pending;
    expect(signal?.aborted).toBe(true);
    expect(changed).toHaveBeenCalledTimes(count);
    await session.load();
    expect(listMock).not.toHaveBeenCalled();
  });
});
