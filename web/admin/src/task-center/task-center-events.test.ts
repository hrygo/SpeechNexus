// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { TaskResponseError, type JobEvent, type TaskApi } from "./job-client";
import { TaskCenterSession } from "./task-center-session";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = (id = "job-demo") => parse("Job", { ...example("queued-job"), id });
const event = (id = "1", jobId = "job-demo") =>
  parse("JobEvent", { ...example("job.queued"), id, job_id: jobId });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
function fixture(values: JobEvent[] = []) {
  const api = {
    list: vi.fn(() =>
      Promise.resolve(
        parse("JobList", {
          object: "nexus.list",
          data: [job()],
          has_more: false,
          next_cursor: null,
        }),
      ),
    ),
    getJob: vi.fn<TaskApi["getJob"]>((id) => Promise.resolve(job(id))),
    cancel: vi.fn(() => Promise.resolve(parse("JobCancellation", example("cancel-job")))),
    events: vi.fn<NonNullable<TaskApi["events"]>>(async function* () {
      for (const value of values) yield await Promise.resolve(value);
    }),
  } satisfies TaskApi;
  return { api, session: new TaskCenterSession(api) };
}

describe("Admin selected-job event recovery", () => {
  it("only connects a selected readable job and an available event port", async () => {
    const { api, session } = fixture();
    await session.connectEvents();
    expect(api.events).not.toHaveBeenCalled();
    const without: TaskApi = { list: api.list, getJob: api.getJob, cancel: api.cancel };
    const snapshots = new TaskCenterSession(without);
    await snapshots.select("job-demo");
    expect(snapshots.eventsAvailable()).toBe(false);
    await snapshots.connectEvents();
    expect(snapshots.getSnapshot().stream).toBe("idle");
    session.dispose();
    snapshots.dispose();
  });

  it("re-reads whole Jobs, deduplicates large sequences, and resumes from the confirmed cursor", async () => {
    const { api, session } = fixture([
      event("9007199254740993"),
      event("9007199254740993"),
      event("2"),
    ]);
    await session.select("job-demo");
    const current = parse("Job", { ...job(), status: "running" });
    api.getJob.mockResolvedValue(current);
    await session.connectEvents();
    expect(api.getJob).toHaveBeenCalledTimes(2);
    expect(session.getSnapshot()).toMatchObject({
      detail: current,
      cursor: "9007199254740993",
      stream: "ended",
    });
    api.events.mockImplementation(async function* () {
      await Promise.resolve();
      yield* [];
    });
    await session.connectEvents();
    expect(api.events.mock.calls[1]?.slice(0, 2)).toEqual(["job-demo", "9007199254740993"]);
    session.dispose();
  });

  it("does not advance a cursor while its snapshot read is pending", async () => {
    const { api, session } = fixture([event("3")]);
    await session.select("job-demo");
    const snapshot = deferred<ReturnType<typeof job>>();
    api.getJob.mockImplementation(() => snapshot.promise);
    const pending = session.connectEvents();
    await vi.waitFor(() => expect(api.getJob).toHaveBeenCalledTimes(2));
    expect(session.getSnapshot().cursor).toBeUndefined();
    snapshot.resolve(job());
    await pending;
    expect(session.getSnapshot().cursor).toBe("3");
    session.dispose();
  });

  it("retains the last confirmed cursor and marks the snapshot stale after a failed event read", async () => {
    const { api, session } = fixture([event("4"), event("5")]);
    await session.select("job-demo");
    api.getJob
      .mockResolvedValueOnce(job())
      .mockRejectedValueOnce(new Error("synthetic-sensitive-value"));
    await session.connectEvents();
    expect(session.getSnapshot()).toMatchObject({
      cursor: "4",
      stream: "interrupted",
      detailPhase: "error",
    });
    expect(session.canCancel()).toBe(false);
    expect(session.getSnapshot().streamNotice).not.toContain("synthetic-sensitive-value");
    session.dispose();
  });

  it("clears expired history and re-reads current state without automatically reconnecting", async () => {
    const { api, session } = fixture([event("7")]);
    await session.select("job-demo");
    await session.connectEvents();
    api.events.mockImplementation(async function* () {
      yield await Promise.reject(new TaskResponseError(410, "event_history_expired"));
    });
    await session.connectEvents();
    expect(session.getSnapshot()).toMatchObject({
      stream: "history-expired",
      detailPhase: "ready",
    });
    expect(session.getSnapshot().cursor).toBeUndefined();
    expect(api.events).toHaveBeenCalledTimes(2);
    expect(api.getJob).toHaveBeenCalledTimes(3);
    api.events.mockImplementation(async function* () {
      await Promise.resolve();
      yield* [];
    });
    await session.connectEvents();
    expect(api.events.mock.calls[2]?.[1]).toBeUndefined();
    session.dispose();
  });

  it("does not claim recovery when the history-expired snapshot read fails", async () => {
    const { api, session } = fixture();
    await session.select("job-demo");
    api.events.mockImplementation(async function* () {
      yield await Promise.reject(new TaskResponseError(410, "event_history_expired"));
    });
    api.getJob.mockRejectedValue(new Error("lost read"));
    await session.connectEvents();
    expect(session.getSnapshot()).toMatchObject({ stream: "interrupted", detailPhase: "error" });
    expect(session.getSnapshot().streamNotice).toContain("恢复状态未确认");
    session.dispose();
  });

  it.each([
    { status: 401, code: "authentication_required" },
    { status: 410, code: "resource_expired" },
  ])("keeps ordinary $status errors distinct from expired history", async ({ status, code }) => {
    const { api, session } = fixture([event("8")]);
    await session.select("job-demo");
    await session.connectEvents();
    api.events.mockImplementation(async function* () {
      yield await Promise.reject(new TaskResponseError(status, code));
    });
    await session.connectEvents();
    expect(session.getSnapshot()).toMatchObject({ stream: "interrupted", cursor: "8" });
    expect(api.getJob).toHaveBeenCalledTimes(2);
    session.dispose();
  });

  it("stops the owner and ignores a late event from a transport that ignores abort", async () => {
    const { api, session } = fixture();
    const late = deferred<JobEvent>();
    let returned = false;
    api.events.mockImplementation(async function* () {
      try {
        yield await late.promise;
      } finally {
        returned = true;
      }
    });
    await session.select("job-demo");
    const pending = session.connectEvents();
    const signal = api.events.mock.calls[0]?.[2];
    session.stopEvents();
    expect(signal?.aborted).toBe(true);
    late.resolve(event("9"));
    await pending;
    expect(api.getJob).toHaveBeenCalledTimes(1);
    expect(returned).toBe(true);
    expect(session.getSnapshot()).toMatchObject({ stream: "idle", cursor: undefined });
    session.dispose();
  });

  it("does not put a late old-task snapshot under a new selected identity", async () => {
    const { api, session } = fixture([event("10")]);
    await session.select("job-demo");
    const old = deferred<ReturnType<typeof job>>();
    api.getJob.mockImplementation(() => old.promise);
    const pending = session.connectEvents();
    await vi.waitFor(() => expect(api.getJob).toHaveBeenCalledTimes(2));
    const signal = api.events.mock.calls[0]?.[2];
    api.getJob.mockImplementation((id) => Promise.resolve(job(id)));
    await session.select("job-other");
    old.resolve(job());
    await pending;
    expect(signal?.aborted).toBe(true);
    expect(session.getSnapshot()).toMatchObject({
      selectedId: "job-other",
      detail: { id: "job-other" },
      cursor: undefined,
      stream: "idle",
    });
    session.dispose();
  });

  it("does not let an old stream finalizer release the replacement owner", async () => {
    const { api, session } = fixture();
    const first = deferred<JobEvent>();
    const second = deferred<JobEvent>();
    api.events
      .mockImplementationOnce(async function* () {
        yield await first.promise;
      })
      .mockImplementationOnce(async function* () {
        yield await second.promise;
      });
    await session.select("job-demo");
    const old = session.connectEvents();
    const current = session.connectEvents();
    const owner = api.events.mock.calls[1]?.[2];
    first.resolve(event("11"));
    await old;
    expect(owner?.aborted).toBe(false);
    session.stopEvents();
    expect(owner?.aborted).toBe(true);
    second.resolve(event("12"));
    await current;
    expect(session.getSnapshot().cursor).toBeUndefined();
    session.dispose();
  });

  it("manual re-read stops events but retains the same-job cursor; selection resets it", async () => {
    const { session } = fixture([event("13")]);
    await session.select("job-demo");
    await session.connectEvents();
    await session.refreshSelected();
    expect(session.getSnapshot().cursor).toBe("13");
    await session.select("job-other");
    expect(session.getSnapshot().cursor).toBeUndefined();
    session.dispose();
  });

  it("disposal aborts the owner and suppresses late notification or re-connection", async () => {
    const { api, session } = fixture();
    const late = deferred<JobEvent>();
    api.events.mockImplementation(async function* () {
      yield await late.promise;
    });
    await session.select("job-demo");
    const changed = vi.fn();
    session.subscribe(changed);
    const pending = session.connectEvents();
    session.dispose();
    const count = changed.mock.calls.length;
    expect(api.events.mock.calls[0]?.[2]?.aborted).toBe(true);
    late.resolve(event("14"));
    await pending;
    await session.connectEvents();
    expect(changed).toHaveBeenCalledTimes(count);
    expect(api.events).toHaveBeenCalledTimes(1);
  });
});
