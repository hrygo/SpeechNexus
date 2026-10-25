// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { TaskCenterSession } from "./task-center-session";
import { SessionTaskRecovery, type TaskRecoveryRecord } from "./task-recovery";
import { TaskResponseError, type TaskApi, type JobCancellation } from "./job-client";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = (id: string) => parse("Job", { ...example("queued-job"), id });
const response = (id: string) => parse("JobCancellation", { ...example("cancel-job"), job_id: id });
const pending: TaskRecoveryRecord = {
  version: 1,
  selectedId: "job-demo",
  cursor: "9007199254740993",
  cancellation: { jobId: "job-demo", key: "9f712fc4-943f-4171-b910-9132c44927a2" },
};
function fixture(record: TaskRecoveryRecord | null = null) {
  let raw = record ? JSON.stringify(record) : null;
  let writeDenied = false;
  const storage = {
    getItem: () => raw,
    setItem: (_key: string, next: string) => {
      if (writeDenied) throw new Error("synthetic-sensitive-value");
      raw = next;
    },
  };
  const get = vi.fn((id: string) => Promise.resolve(job(id)));
  const cancel = vi.fn((id: string) => Promise.resolve(response(id)));
  const api: TaskApi = {
    list: () =>
      Promise.resolve(
        parse("JobList", { object: "nexus.list", data: [], has_more: false, next_cursor: null }),
      ),
    getJob: get,
    cancel,
    async *events(id) {
      yield await Promise.resolve(
        parse("JobEvent", { ...example("job.queued"), job_id: id, id: "9007199254740994" }),
      );
    },
  };
  const create = () => new TaskCenterSession(api, new SessionTaskRecovery(storage, "recovery"));
  return {
    api,
    create,
    get,
    cancel,
    raw: () => raw,
    row: () => JSON.parse(raw!) as TaskRecoveryRecord,
    denyWrites: () => {
      writeDenied = true;
    },
    replace: (value: string) => {
      raw = value;
    },
  };
}

describe("Admin same-tab task recovery", () => {
  it("restores no snapshot or request, then explicitly replays the original key/target after selection changes", async () => {
    const f = fixture(pending);
    const session = f.create();
    expect(f.get).not.toHaveBeenCalled();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(session.getSnapshot()).toMatchObject({
      selectedId: "job-demo",
      detail: null,
      detailPhase: "idle",
      cursor: pending.cursor,
      cancellation: { jobId: "job-demo", phase: "unknown" },
      recovery: "ready",
    });
    await session.select("job-other");
    await session.cancelSelected();
    expect(f.cancel).not.toHaveBeenCalled();
    await session.replayCancellation();
    expect(f.cancel).toHaveBeenCalledWith(
      "job-demo",
      pending.cancellation!.key,
      expect.any(AbortSignal),
    );
    expect(session.getSnapshot().detail?.id).toBe("job-other");
    expect(f.row()).toMatchObject({ selectedId: "job-other", cursor: null, cancellation: null });
  });

  it("persists intent before the first POST and retains it on lost response", async () => {
    const f = fixture();
    f.api.cancel = (id, key) => {
      expect(f.row().cancellation).toEqual({ jobId: id, key });
      return Promise.reject(new Error("lost response"));
    };
    const session = f.create();
    await session.select("job-demo");
    await session.cancelSelected();
    const restored = f.create();
    expect(restored.getSnapshot().cancellation?.phase).toBe("unknown");
    expect(f.row().cancellation?.key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("does not POST or overwrite the old record when intent storage fails", async () => {
    const f = fixture();
    const session = f.create();
    await session.select("job-demo");
    const before = f.raw();
    f.denyWrites();
    await session.cancelSelected();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(f.raw()).toBe(before);
    expect(session.getSnapshot().recovery).toBe("unavailable");
    expect(session.canCancel()).toBe(false);
    expect(session.getSnapshot().recoveryNotice).not.toContain("synthetic-sensitive-value");
  });

  it("can browse but cannot cancel when initial recovery data is corrupt", async () => {
    const f = fixture();
    f.replace('{"unknown":"synthetic-sensitive-value"}');
    const session = f.create();
    expect(session.getSnapshot().recovery).toBe("unavailable");
    await session.select("job-demo");
    expect(session.getSnapshot().detail?.id).toBe("job-demo");
    await session.cancelSelected();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(f.raw()).toContain("unknown");
    expect(session.getSnapshot().recoveryNotice).not.toContain("synthetic-sensitive-value");
  });

  it("keeps unknown when acknowledgement cannot be persisted", async () => {
    const f = fixture();
    f.api.cancel = (id) => {
      f.denyWrites();
      return Promise.resolve(response(id));
    };
    const session = f.create();
    await session.select("job-demo");
    await session.cancelSelected();
    expect(session.getSnapshot().cancellation).toMatchObject({ phase: "unknown", response: null });
    expect(f.row().cancellation?.jobId).toBe("job-demo");
    const restored = f.create();
    expect(restored.getSnapshot().cancellation?.phase).toBe("unknown");
  });

  it("does not accept a response belonging to another target", async () => {
    const f = fixture(pending);
    f.api.cancel = () => Promise.resolve(response("job-other"));
    const session = f.create();
    await session.replayCancellation();
    expect(session.getSnapshot().cancellation).toMatchObject({ phase: "unknown", response: null });
    expect(f.row().cancellation).toEqual(pending.cancellation);
  });

  it("persists known acknowledgement without claiming task completion or cleanup", async () => {
    const f = fixture(pending);
    const session = f.create();
    await session.replayCancellation();
    expect(session.getSnapshot().cancellation?.response?.accepted).toBe(true);
    expect(session.getSnapshot().detail?.status).toBe("queued");
    expect(session.getSnapshot().detail?.cleanup_state).toBe("pending");
    expect(f.create().getSnapshot().cancellation).toBeNull();
  });

  it("recovers an in-flight POST after dispose and ignores its late reply", async () => {
    const f = fixture();
    let resolve!: (value: JobCancellation) => void;
    f.api.cancel = () =>
      new Promise((r) => {
        resolve = r;
      });
    const session = f.create();
    await session.select("job-demo");
    const inFlight = session.cancelSelected();
    expect(session.getSnapshot().cancellation?.phase).toBe("submitting");
    session.dispose();
    const restored = f.create();
    const savedKey = f.row().cancellation!.key;
    resolve(response("job-demo"));
    await inFlight;
    expect(f.row().cancellation?.key).toBe(savedKey);
    expect(restored.getSnapshot().cancellation?.phase).toBe("unknown");
    f.api.cancel = f.cancel;
    await restored.replayCancellation();
    expect(f.cancel).toHaveBeenCalledWith("job-demo", savedKey, expect.any(AbortSignal));
  });

  it("restores a large confirmed event cursor without any automatic read or stream", async () => {
    const f = fixture({ ...pending, cancellation: null });
    const session = f.create();
    await session.refreshSelected();
    await session.connectEvents();
    expect(f.row().cursor).toBe("9007199254740994");
    session.dispose();
    f.get.mockClear();
    const restored = f.create();
    expect(f.get).not.toHaveBeenCalled();
    expect(restored.getSnapshot().detail).toBeNull();
    const received: (string | undefined)[] = [];
    f.api.events = async function* (_id, after) {
      received.push(after);
      yield* await Promise.resolve([]);
    };
    await restored.connectEvents();
    expect(received).toHaveLength(0);
    await restored.refreshSelected();
    await restored.connectEvents();
    expect(received).toEqual(["9007199254740994"]);
  });

  it("retains the old persisted cursor on failed event snapshot read", async () => {
    const f = fixture({ ...pending, cancellation: null });
    const session = f.create();
    await session.refreshSelected();
    f.api.getJob = () => Promise.reject(new Error("synthetic-sensitive-value"));
    await session.connectEvents();
    expect(f.row().cursor).toBe(pending.cursor);
    expect(session.getSnapshot().cursor).toBe(pending.cursor);
    expect(session.getSnapshot().detailPhase).toBe("error");
  });

  it("persists history expiry before reading current state", async () => {
    const f = fixture({ ...pending, cancellation: null });
    const session = f.create();
    await session.refreshSelected();
    f.api.events = async function* () {
      yield await Promise.reject(new TaskResponseError(410, "event_history_expired"));
    };
    await session.connectEvents();
    expect(f.row().cursor).toBeNull();
    expect(f.create().getSnapshot().cursor).toBeUndefined();
    expect(session.getSnapshot().stream).toBe("history-expired");
  });

  it("does not claim history recovery or advance when saving cleared cursor fails", async () => {
    const f = fixture({ ...pending, cancellation: null });
    const session = f.create();
    await session.refreshSelected();
    f.denyWrites();
    f.api.events = async function* () {
      yield await Promise.reject(new TaskResponseError(410, "event_history_expired"));
    };
    await session.connectEvents();
    expect(f.row().cursor).toBe(pending.cursor);
    expect(session.getSnapshot().cursor).toBe(pending.cursor);
    expect(session.getSnapshot().stream).toBe("interrupted");
    expect(session.getSnapshot().recovery).toBe("unavailable");
  });

  it("does not advance the visible cursor when the confirmed snapshot cannot be journaled", async () => {
    const f = fixture({ ...pending, cancellation: null });
    const session = f.create();
    await session.refreshSelected();
    f.denyWrites();
    await session.connectEvents();
    expect(session.getSnapshot().cursor).toBe(pending.cursor);
    expect(f.row().cursor).toBe(pending.cursor);
    expect(session.getSnapshot().recovery).toBe("unavailable");
    expect(session.eventsAvailable()).toBe(false);
  });

  it("blocks replay if another owner replaced the pending record", async () => {
    const f = fixture(pending);
    const session = f.create();
    const other = JSON.stringify({
      ...pending,
      cancellation: { ...pending.cancellation!, jobId: "job-other" },
    });
    f.replace(other);
    await session.replayCancellation();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(f.raw()).toBe(other);
    expect(session.getSnapshot().recovery).toBe("unavailable");
  });

  it("rejects storage denial on initial load without throwing during construction", () => {
    const f = fixture();
    const session = new TaskCenterSession(
      f.api,
      new SessionTaskRecovery(
        {
          getItem: () => {
            throw new Error("denied");
          },
          setItem: () => {},
        },
        "recovery",
      ),
    );
    expect(session.getSnapshot().recovery).toBe("unavailable");
    expect(f.get).not.toHaveBeenCalled();
  });
});
