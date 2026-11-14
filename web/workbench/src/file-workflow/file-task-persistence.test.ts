// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { canFinishTask } from "./file-task-policy";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal, type FileTaskJournal, type TaskRecord } from "./task-journal";
import { createDemoTransport } from "./dev/demo-transport";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

function setup(scenario: Parameters<typeof createDemoTransport>[0] = "success") {
  const transport = createDemoTransport(scenario);
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const bodies: string[] = [];
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    if (new URL(request.url).pathname === "/v1/nexus/jobs" && request.method === "POST")
      bodies.push(await request.clone().text());
    return transport.fetch(request);
  });
  const session = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  return {
    transport,
    bodies,
    read: () => new SessionTaskJournal(storage, "task").load()!,
    data,
    storage,
    client,
    session,
    file: new File(["synthetic audio"], "private-name.wav"),
  };
}

function rejection(status: number, code: string) {
  return Response.json(
    parse("ErrorResponse", {
      error: {
        code,
        type: "invalid_request_error",
        message: `Synthetic rejection ${code}`,
        param: null,
        request_id: "req-demo",
        retryable: false,
      },
    }),
    { status },
  );
}

describe("durable file task identities", () => {
  it("keeps the confirmed cursor if storage fails while handling expired event history", async () => {
    const test = setup("history-expired");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    const before = test.data.get("task");
    test.storage.setItem = () => {
      throw new Error("private");
    };
    await expect(first.reconnect()).resolves.toBeUndefined();
    expect(test.data.get("task")).toBe(before);
    expect(first.getSnapshot().stream).toBe("interrupted");
    expect(first.getSnapshot().notice).not.toContain("private");
    first.dispose();
  });
  it("restores an unknown create without POST, then explicitly replays the same key/body/workspace", async () => {
    const test = setup("create-unknown");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    first.dispose();
    const saved = test.data.get("task")!;
    expect(saved).not.toMatch(/private-name|synthetic audio|transcript.*text/);
    const second = test.session();
    await second.restore();
    expect(test.transport.stats.createKeys).toHaveLength(1);
    expect(second.getSnapshot().canReplay).toBe(true);
    await second.replay();
    expect(test.transport.stats.createKeys).toHaveLength(2);
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.bodies[1]).toBe(test.bodies[0]);
    expect(parse("JobCreateRequest", JSON.parse(test.bodies[0]!) as unknown).workspace_id).toBe(
      "ws-default",
    );
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    const record = test.read();
    expect(record.workspaceId).toBe("ws-default");
    expect(record.pending).toBeNull();
    second.dispose();
  });

  it("restores cancellation against the original job/key without mistaking pending cleanup for release", async () => {
    const test = setup("cancel-unknown");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    first.dispose();
    const before = test.read();
    const second = test.session();
    await second.restore();
    expect(second.getSnapshot().unknown).toBe(true);
    await second.replay();
    const after = test.read();
    expect(after.cancelKey).toBe(before.cancelKey);
    expect(after.jobId).toBe(before.jobId);
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().cancellation?.accepted).toBe(true);
    expect(second.getSnapshot().job?.cleanup_state).toBe("pending");
    second.dispose();
  });

  it("reads an acknowledged job and resumes exact event cursors after reload", async () => {
    const test = setup();
    const first = test.session();
    await first.start(test.file, "asr-quality");
    await first.reconnect();
    first.dispose();
    const second = test.session();
    await second.restore();
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    expect(second.getSnapshot().cursor).toBe("9007199254740993");
    await second.reconnect();
    expect(test.transport.stats.lastEventIds.at(-1)).toBe("9007199254740993");
    expect(test.transport.stats.createKeys).toHaveLength(1);
    second.dispose();
  });

  it("does not POST when the initial journal write fails", async () => {
    const test = setup();
    test.storage.setItem = () => {
      throw new Error("private");
    };
    const first = test.session();
    await first.start(test.file, "asr-quality");
    expect(test.transport.stats.createKeys).toHaveLength(0);
    expect(first.getSnapshot().notice).not.toContain("private");
    first.dispose();
  });

  it("restores a terminally refused create as settled instead of an unknown submission", async () => {
    const test = setup();
    let creates = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (new URL(request.url).pathname === "/v1/nexus/jobs" && request.method === "POST") {
        creates++;
        return rejection(422, "job_binding_incomplete");
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    expect(first.getSnapshot()).toMatchObject({ unknown: false, rejected: true, canReplay: false });
    first.dispose();
    // The observed refusal is what survives reload; `pending` keeps its in-flight meaning.
    expect(test.read()).toMatchObject({ pending: "create", settled: "terminal", jobId: null });
    const second = make();
    await second.restore();
    expect(second.getSnapshot()).toMatchObject({
      unknown: false,
      rejected: true,
      canReplay: false,
    });
    expect(second.getSnapshot().notice).not.toContain("结果未知");
    expect(canFinishTask(second.getSnapshot())).toBe(true);
    // #771: this verdict alone must block replay. `unreplayable` stays absent (this is the
    // terminal class, not the 413 one), so nothing but `settled === undefined` can be what
    // denies `canReplay`; the public replay action must not resend the refused body either.
    expect(second.getSnapshot().unreplayable).toBeUndefined();
    await second.replay();
    // Restoring a settled intent reads no identity and submits nothing.
    expect(creates).toBe(1);
    second.dispose();
  });

  it("keeps a lost create response unknown and never records a settled refusal", async () => {
    const test = setup();
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (new URL(request.url).pathname === "/v1/nexus/jobs" && request.method === "POST")
        throw new TypeError("Synthetic lost create response");
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(first.getSnapshot().rejected).toBeUndefined();
    expect(test.read()).not.toHaveProperty("settled");
    first.dispose();
    const second = make();
    await second.restore();
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(second.getSnapshot().rejected).toBeUndefined();
    // #771: the crash window (pending create, no observed answer) reloads as one
    // unconfirmed submission. The status line must say so and must never borrow the
    // wording of a refusal, which would contradict the alert and the offered replay.
    expect(second.getSnapshot().notice).toBe(
      "已找到原任务身份；该请求结果尚未确认，请读取原任务核对，或按原输入与原键重放。",
    );
    expect(second.getSnapshot().notice).not.toContain("确定性拒绝");
    expect(canFinishTask(second.getSnapshot())).toBe(false);
    second.dispose();
  });

  it("keeps original pending identity if storage fails after the server accepts create", async () => {
    const test = setup();
    const original = test.storage.setItem;
    test.storage.setItem = (key, value) => {
      const record = JSON.parse(value) as { pending: string | null };
      if (record.pending === null) throw new Error("private");
      original(key, value);
    };
    const first = test.session();
    await first.start(test.file, "asr-quality");
    first.dispose();
    expect(test.read().pending).toBe("create");
    test.storage.setItem = original;
    const second = test.session();
    await second.restore();
    await second.replay();
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.transport.stats.executions).toBe(1);
    second.dispose();
  });

  it("restores a terminally refused cancel as settled instead of an unknown cancellation", async () => {
    const test = setup();
    let cancellations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (
        new URL(request.url).pathname === "/v1/nexus/jobs/job-demo/cancellations" &&
        request.method === "POST"
      ) {
        cancellations++;
        return rejection(409, "execution_cleanup_pending");
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: false, rejected: true, canReplay: false });
    first.dispose();
    // The refused cancel intent is spent; the Job it named keeps its own readable facts.
    expect(test.read()).toMatchObject({
      pending: "cancel",
      jobId: "job-demo",
      settled: "terminal",
    });
    const second = make();
    await second.restore();
    expect(second.getSnapshot()).toMatchObject({
      unknown: false,
      rejected: true,
      canReplay: false,
      job: { id: "job-demo" },
    });
    // Counterexample 1: a reader that ignored the cancel class would restore this as
    // "result unknown" and forbid the explicit end of the page.
    expect(second.getSnapshot().notice).not.toContain("结果未知");
    expect(canFinishTask(second.getSnapshot())).toBe(true);
    await second.replay();
    await second.cancel();
    expect(cancellations).toBe(1);
    expect(test.transport.stats.createKeys).toHaveLength(1);
    expect(test.transport.stats.executions).toBe(1);
    expect(test.read()).toMatchObject({ pending: "cancel", settled: "terminal" });
    second.dispose();
  });

  it("keeps a lost cancellation response unknown and never records a settled refusal", async () => {
    const test = setup();
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (
        new URL(request.url).pathname === "/v1/nexus/jobs/job-demo/cancellations" &&
        request.method === "POST"
      )
        throw new TypeError("Synthetic lost cancellation response");
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(first.getSnapshot().rejected).toBeUndefined();
    expect(test.read()).toMatchObject({ pending: "cancel" });
    expect(test.read()).not.toHaveProperty("settled");
    first.dispose();
    const second = make();
    await second.restore();
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(second.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(second.getSnapshot())).toBe(false);
    second.dispose();
  });

  it("restores operation recovery identity using GET only", async () => {
    const test = setup();
    let creates = 0;
    let operations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/jobs") {
        creates++;
        return Response.json(fixtures.examples.find((e) => e.id === "recover-operation")!.value, {
          status: 409,
        });
      }
      if (path === "/v1/nexus/operations/op-demo") {
        operations++;
        return Response.json(fixtures.examples.find((e) => e.id === "operation")!.value);
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    first.dispose();
    const second = make();
    await second.restore();
    await second.replay();
    expect(creates).toBe(1);
    expect(operations).toBe(1);
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    second.dispose();
  });

  it("keeps cancellation pending when operation recovery only reads its associated job", async () => {
    const test = setup();
    let cancellations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (
        new URL(request.url).pathname === "/v1/nexus/jobs/job-demo/cancellations" &&
        request.method === "POST"
      ) {
        cancellations++;
        return Response.json(fixtures.examples.find((e) => e.id === "recover-operation")!.value, {
          status: 409,
        });
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    const before = test.read();
    expect(before.pending).toBe("cancel");
    expect(before.operationId).toBe("op-demo");
    await first.refresh();
    expect(test.read()).toEqual(before);
    expect(first.getSnapshot()).toMatchObject({
      unknown: true,
      canReplay: false,
      job: { id: "job-demo", status: "queued" },
    });
    expect(first.getSnapshot().cancellation).toBeUndefined();
    first.dispose();
    const second = make();
    await second.restore();
    await second.replay();
    expect(test.read()).toEqual(before);
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: false });
    expect(second.getSnapshot().cancellation).toBeUndefined();
    expect(cancellations).toBe(1);
    expect(test.transport.stats.createKeys).toHaveLength(1);
    second.dispose();
  });

  it("blocks a stale controller from submitting a different intent", async () => {
    const test = setup();
    const first = test.session();
    const stale = test.session();
    await first.start(test.file, "asr-quality");
    await stale.start(test.file, "asr-other");
    expect(test.transport.stats.createKeys).toHaveLength(1);
    expect(test.read().model).toBe("asr-quality");
    first.dispose();
    stale.dispose();
  });

  it("does not write late acknowledged responses after disposal", async () => {
    const test = setup();
    let release: () => void = () => undefined;
    const wait = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reached: () => void = () => undefined;
    const ready = new Promise<void>((resolve) => {
      reached = resolve;
    });
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const response = await test.transport.fetch(request);
      if (new URL(request.url).pathname === "/v1/nexus/jobs") {
        reached();
        await wait;
      }
      return response;
    });
    const first = new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const start = first.start(test.file, "asr-quality");
    await ready;
    const before = test.data.get("task");
    first.dispose();
    release();
    await start;
    expect(test.data.get("task")).toBe(before);
    expect(test.read().pending).toBe("create");
    // The response never arrived, so no refusal was observed: writing `settled` here would
    // claim a guaranteed outcome the protocol never gave.
    expect(test.read()).not.toHaveProperty("settled");
    const restored = test.session();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(restored.getSnapshot())).toBe(false);
    restored.dispose();
  });
});

/**
 * Restored recovery projection (#771): the four fields the page reads plus the status line
 * it renders, one row per `(pending, settled)` pair. `operationId` is `null` in every row
 * because that is the only shape where `canReplay` is still a question; an identified
 * original execution always denies replay, which is a separate, already-pinned rule.
 *
 * The rows marked `invalid` are shapes `SessionTaskJournal` refuses; the fabricated journal
 * below bypasses that validation on purpose so the projection is pinned for all nine cells,
 * including the incoherent ones, and the same test proves the journal keeps them unreachable.
 * Reachable rows therefore cannot be silently re-worded, and the journal cannot start
 * accepting a shape whose projection would over-claim a refusal.
 */
const resumedNotice =
  "已找到原任务身份；该请求结果尚未确认，请读取原任务核对，或按原输入与原键重放。";
const identifiedNotice = "已找到原任务身份；请读取原任务状态。";
const terminalCreateNotice = "已找到原任务身份；该意图已被协议确定性拒绝并作废。";
const terminalCancelNotice =
  "已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。";
const unreplayableNotice =
  "已找到原任务身份；该请求因请求体超过本部署的接收上限被拒绝，原意图作废。";

interface RestoreRow {
  readonly pending: "create" | "cancel" | null;
  readonly settled: "terminal" | "unreplayable" | undefined;
  /** Whether the durable journal can actually carry this pair. */
  readonly journal: "valid" | "invalid";
  readonly unknown: boolean;
  readonly canReplay: boolean;
  readonly rejected: boolean;
  readonly unreplayable: boolean;
  readonly notice: string;
}

const restoreMatrix: readonly RestoreRow[] = [
  {
    pending: null,
    settled: undefined,
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: false,
    unreplayable: false,
    notice: identifiedNotice,
  },
  {
    pending: null,
    settled: "terminal",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
  },
  {
    pending: null,
    settled: "unreplayable",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
  },
  {
    pending: "create",
    settled: undefined,
    journal: "valid",
    unknown: true,
    canReplay: true,
    rejected: false,
    unreplayable: false,
    notice: resumedNotice,
  },
  {
    pending: "create",
    settled: "terminal",
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
  },
  {
    pending: "create",
    settled: "unreplayable",
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
  },
  {
    pending: "cancel",
    settled: undefined,
    journal: "valid",
    unknown: true,
    canReplay: true,
    rejected: false,
    unreplayable: false,
    notice: resumedNotice,
  },
  {
    pending: "cancel",
    settled: "terminal",
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCancelNotice,
  },
  {
    pending: "cancel",
    settled: "unreplayable",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
  },
];

const restoreKey = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

function restoreRecord(pending: RestoreRow["pending"], settled: RestoreRow["settled"]): TaskRecord {
  return {
    version: 1,
    model: "asr-quality",
    assetId: "asset-demo",
    workspaceId: "ws-default",
    createKey: restoreKey(1),
    cancelKey: restoreKey(2),
    // A pending create owns no Job; a cancel names one, and an acknowledged intent keeps it.
    jobId: pending === "create" ? null : "job-demo",
    pending,
    operationId: null,
    cursor: null,
    ...(settled === undefined ? {} : { settled }),
  };
}

function restoreSnapshot(record: TaskRecord) {
  const journal: FileTaskJournal = {
    load: () => record,
    save: () => {
      throw new Error("a restored page never writes");
    },
  };
  const client = new SpeechFileClient("http://127.0.0.1", () => {
    throw new Error("a restored page never requests");
  });
  return new FileTaskSession(client, journal).getSnapshot();
}

function journalVerdict(record: TaskRecord): "valid" | "invalid" {
  const data = new Map<string, string>([["restore", JSON.stringify(record)]]);
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  try {
    new SessionTaskJournal(storage, "restore").load();
    return "valid";
  } catch {
    return "invalid";
  }
}

describe("restored recovery projection", () => {
  it.each(restoreMatrix)(
    "pending=$pending settled=$settled projects one coherent verdict",
    (row) => {
      const state = restoreSnapshot(restoreRecord(row.pending, row.settled));
      expect({
        unknown: state.unknown,
        canReplay: state.canReplay,
        rejected: state.rejected === true,
        unreplayable: state.unreplayable === true,
      }).toEqual({
        unknown: row.unknown,
        canReplay: row.canReplay,
        rejected: row.rejected,
        unreplayable: row.unreplayable,
      });
      expect(state.notice).toBe(row.notice);
      // Compatibility of the four fields, checked on every cell: a refusal and an unknown
      // outcome are different answers to the same question, so they can never both be true;
      // replay exists only for the unknown outcome; `unreplayable` is a kind of refusal.
      expect(state.unknown && state.rejected === true).toBe(false);
      expect(state.canReplay && !state.unknown).toBe(false);
      expect(state.unreplayable === true && state.rejected !== true).toBe(false);
      expect(journalVerdict(restoreRecord(row.pending, row.settled))).toBe(row.journal);
    },
  );
});
