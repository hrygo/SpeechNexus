// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession, unconfirmedAlertFor, type ReadTarget } from "./file-task-session";
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

// Restored status-line copy (#771). Each branch is asserted verbatim: the notice is the
// page's own claim about what the user can do, so a wording drift is a behavior change.
const resumedNotice =
  "已找到原任务身份；该请求结果尚未确认，请读取原任务核对，或按原输入与原键重放。";
const executionResumeNotice =
  "已找到原执行身份；该请求结果尚未确认，请读取原执行核对，不要新建任务。";
const replayOnlyNotice =
  "已找到原任务身份；该请求结果尚未确认，请按原输入与原键重放，不要新建任务。";
const identifiedNotice = "已找到原任务身份；请读取原任务状态。";
const terminalCreateNotice = "已找到原任务身份；该意图已被协议确定性拒绝并作废。";
const terminalCancelNotice =
  "已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。";
const unreplayableNotice =
  "已找到原任务身份；该请求因请求体超过本部署的接收上限被拒绝，原意图作废。";

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
    let requests = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      requests++;
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
    requests = 0;
    const second = make();
    await second.restore();
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(second.getSnapshot().rejected).toBeUndefined();
    // #771: the crash window (pending create, no observed answer) reloads as one
    // unconfirmed submission. The status line must say so and must never borrow the
    // wording of a refusal, which would contradict the alert and the offered replay.
    // This shape names no readable Job or execution (see the zero-request assertion
    // below), so the copy may promise only the original-key replay (review S1).
    expect(second.getSnapshot().notice).toBe(replayOnlyNotice);
    expect(second.getSnapshot().notice).not.toContain("确定性拒绝");
    expect(second.getSnapshot().notice).not.toContain("读取");
    expect(requests, "the crash window has no readable identity to reconcile").toBe(0);
    expect(canFinishTask(second.getSnapshot())).toBe(false);
    second.dispose();
  });

  it("restores an identified original execution as a query, not a replay", async () => {
    // Reachable via `operation_in_progress`: `#persist({ operationId })` keeps the
    // execution identity, and the contract asks to query that operation -- replaying the
    // submission key is exactly what the page must not promise (review M1).
    const test = setup();
    let creates = 0;
    let operations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/jobs" && request.method === "POST") {
        creates++;
        return Response.json(fixtures.examples.find((e) => e.id === "recover-operation")!.value, {
          status: 409,
        });
      }
      if (path === "/v1/nexus/operations/op-demo") operations++;
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    expect(test.read()).toMatchObject({ pending: "create", operationId: "op-demo" });
    first.dispose();
    const second = make();
    const state = second.getSnapshot();
    // Restored before any request: the identified execution is queryable, while replay is
    // structurally disabled, so the copy must not offer it.
    expect(state).toMatchObject({ unknown: true, canReplay: false });
    expect(state.rejected).toBeUndefined();
    expect(state.notice).toBe(executionResumeNotice);
    expect(state.notice).not.toContain("重放");
    await second.replay();
    expect(creates).toBe(1);
    // The action the copy names is real: reading the original state queries the operation.
    await second.restore();
    expect(operations).toBe(1);
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
    // Same identified-execution shape on the cancel class: the restored copy points at the
    // queryable execution and never offers a replay the page disables (review M1).
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: false });
    expect(second.getSnapshot().notice).toBe(executionResumeNotice);
    expect(second.getSnapshot().notice).not.toContain("重放");
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
 * it renders, pinned over the full `(pending, settled, operationId)` cross product -- all
 * 18 cells. The first axis decides what may be claimed, the second whether the intent is
 * spent, and the third whether an execution identity already exists: `canReplay` is
 * structurally false once `operationId` is non-null, yet the notice on that axis still
 * needs its own assertion (review M1) rather than being inferred from a two-value table.
 *
 * The rows marked `invalid` are shapes `SessionTaskJournal` refuses; the fabricated journal
 * below bypasses that validation on purpose so every cell's projection is pinned, and the
 * same test proves the journal keeps those cells unreachable. Reachable cells therefore
 * cannot be silently re-worded, and the journal cannot start accepting a shape whose
 * projection would over-claim a refusal or a replay.
 *
 * Copy legend: `resumed` reads the named Job or replays the key, `executionResume` queries
 * the identified Operation, `replayOnly` replays because no identity is readable,
 * `identified` reads the confirmed Job, and the last three are settled refusals.
 */
interface RestoreRow {
  readonly pending: "create" | "cancel" | null;
  readonly settled: "terminal" | "unreplayable" | undefined;
  readonly operationId: "op-demo" | null;
  /** Whether the durable journal can actually carry this cell. */
  readonly journal: "valid" | "invalid";
  readonly unknown: boolean;
  readonly canReplay: boolean;
  readonly rejected: boolean;
  readonly unreplayable: boolean;
  readonly notice: string;
  /** Whether the copy may offer the original-key replay action. */
  readonly mentionsReplay: boolean;
}

const restoreMatrix: readonly RestoreRow[] = [
  // operationId === null: no execution identity, so the original key is the only handle.
  {
    pending: null,
    settled: undefined,
    operationId: null,
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: false,
    unreplayable: false,
    notice: identifiedNotice,
    mentionsReplay: false,
  },
  {
    pending: null,
    settled: "terminal",
    operationId: null,
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
    mentionsReplay: false,
  },
  {
    pending: null,
    settled: "unreplayable",
    operationId: null,
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
  {
    pending: "create",
    settled: undefined,
    operationId: null,
    journal: "valid",
    unknown: true,
    canReplay: true,
    rejected: false,
    unreplayable: false,
    notice: replayOnlyNotice,
    mentionsReplay: true,
  },
  {
    pending: "create",
    settled: "terminal",
    operationId: null,
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
    mentionsReplay: false,
  },
  {
    pending: "create",
    settled: "unreplayable",
    operationId: null,
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
  {
    pending: "cancel",
    settled: undefined,
    operationId: null,
    journal: "valid",
    unknown: true,
    canReplay: true,
    rejected: false,
    unreplayable: false,
    notice: resumedNotice,
    mentionsReplay: true,
  },
  {
    pending: "cancel",
    settled: "terminal",
    operationId: null,
    journal: "valid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCancelNotice,
    mentionsReplay: false,
  },
  {
    pending: "cancel",
    settled: "unreplayable",
    operationId: null,
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
  // operationId === "op-demo": an execution identity exists, so replay is structurally off.
  {
    pending: null,
    settled: undefined,
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: false,
    unreplayable: false,
    notice: identifiedNotice,
    mentionsReplay: false,
  },
  {
    pending: null,
    settled: "terminal",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
    mentionsReplay: false,
  },
  {
    pending: null,
    settled: "unreplayable",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
  {
    pending: "create",
    settled: undefined,
    operationId: "op-demo",
    journal: "valid",
    unknown: true,
    canReplay: false,
    rejected: false,
    unreplayable: false,
    notice: executionResumeNotice,
    mentionsReplay: false,
  },
  {
    pending: "create",
    settled: "terminal",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCreateNotice,
    mentionsReplay: false,
  },
  {
    pending: "create",
    settled: "unreplayable",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
  {
    pending: "cancel",
    settled: undefined,
    operationId: "op-demo",
    journal: "valid",
    unknown: true,
    canReplay: false,
    rejected: false,
    unreplayable: false,
    notice: executionResumeNotice,
    mentionsReplay: false,
  },
  {
    pending: "cancel",
    settled: "terminal",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: false,
    notice: terminalCancelNotice,
    mentionsReplay: false,
  },
  {
    pending: "cancel",
    settled: "unreplayable",
    operationId: "op-demo",
    journal: "invalid",
    unknown: false,
    canReplay: false,
    rejected: true,
    unreplayable: true,
    notice: unreplayableNotice,
    mentionsReplay: false,
  },
];

const restoreKey = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

function restoreRecord(
  pending: RestoreRow["pending"],
  settled: RestoreRow["settled"],
  operationId: RestoreRow["operationId"],
): TaskRecord {
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
    operationId,
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

/** Fixture example, cloned so no parser mutates a value shared across cells. */
const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);

/**
 * The copy fragments each readable handle must own (#780). The alert is asserted against
 * the fragment of the handle the state enables, never against a whole literal, so wording
 * and enablement cannot drift apart without one of the two failing.
 */
const readPhrase: Readonly<Record<ReadTarget, string>> = {
  operation: "读取原执行",
  job: "读取原任务",
  asset: "核对原素材",
};

function namedReads(alert: string): ReadTarget[] {
  return (Object.keys(readPhrase) as ReadTarget[]).filter((target) =>
    alert.includes(readPhrase[target]),
  );
}

/** The read handle a row's own declared facts require, independent of the widget's table. */
function expectedRead(row: RestoreRow): ReadTarget | null {
  const record = restoreRecord(row.pending, row.settled, row.operationId);
  if (record.operationId !== null) return "operation";
  return record.jobId !== null ? "job" : null;
}

/** A restored page whose only observable effect is the routes its reads actually hit. */
function restoreWithRequests(record: TaskRecord) {
  const requests: string[] = [];
  const journal: FileTaskJournal = {
    load: () => record,
    save: () => {
      throw new Error("a restored page never writes");
    },
  };
  const client = new SpeechFileClient("http://127.0.0.1", (input) => {
    const request = input instanceof Request ? input : new Request(input);
    const path = new URL(request.url).pathname;
    requests.push(path);
    // No `job_id`: the operation read must stay a single request, so the projection this
    // test measures is the read dispatch alone, not its follow-up.
    if (path.startsWith("/v1/nexus/operations/"))
      return Promise.resolve(Response.json({ ...example("operation"), job_id: null }));
    if (path.startsWith("/v1/nexus/jobs/"))
      return Promise.resolve(Response.json(example("queued-job")));
    return Promise.resolve(Response.json(example("ready-audio")));
  });
  return { session: new FileTaskSession(client, journal), requests };
}

/** The handle a real request proves the page can read; any other route would be a bug. */
function hitTarget(path: string): ReadTarget {
  if (path.startsWith("/v1/nexus/operations/")) return "operation";
  if (path.startsWith("/v1/nexus/jobs/")) return "job";
  return "asset";
}

describe("restored recovery projection", () => {
  it.each(restoreMatrix)(
    "pending=$pending settled=$settled operationId=$operationId projects one coherent verdict",
    (row) => {
      const record = restoreRecord(row.pending, row.settled, row.operationId);
      const state = restoreSnapshot(record);
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
      // #771 review M1: the replay sentence is an action claim, so it must track the
      // enabled action exactly -- including the operationId axis, where replay is
      // structurally disabled and the copy may only name the queryable execution.
      expect(state.notice.includes("重放")).toBe(row.mentionsReplay);
      expect(state.canReplay).toBe(row.mentionsReplay);
      // Compatibility of the four fields, checked on every cell: a refusal and an unknown
      // outcome are different answers to the same question, so they can never both be true;
      // replay exists only for the unknown outcome; `unreplayable` is a kind of refusal.
      expect(state.unknown && state.rejected === true).toBe(false);
      expect(state.canReplay && !state.unknown).toBe(false);
      expect(state.unreplayable === true && state.rejected !== true).toBe(false);
      expect(journalVerdict(record)).toBe(row.journal);
    },
  );
});

/**
 * Alert coverage over the same 18 cells (#780). The alert is what asks the user to act, so
 * its action list must be exactly the actions this state enables. The equality is asserted
 * against the state's own handles -- `readTarget` and `canReplay` -- not against another
 * copy of the sentence, and a second test grounds those handles in the routes a real
 * restore() issues, so a change that merely rewords both sides cannot satisfy it.
 */
describe("restored alert names exactly the enabled actions", () => {
  it.each(restoreMatrix)(
    "pending=$pending settled=$settled operationId=$operationId alerts only what it can do",
    (row) => {
      const state = restoreSnapshot(restoreRecord(row.pending, row.settled, row.operationId));
      const alert = unconfirmedAlertFor({ read: state.readTarget, replay: state.canReplay });
      expect(state.readTarget).toBe(expectedRead(row));
      // Copy vs enablement, both directions: the alert names the read handle this state
      // enables and no other; the crash window enables none, so it names none.
      expect(namedReads(alert)).toEqual(state.readTarget === null ? [] : [state.readTarget]);
      expect(alert.includes("重放")).toBe(state.canReplay);
      // A state without any handle may not ask the user to verify something it can reach.
      if (state.readTarget === null && state.canReplay)
        expect(alert).toContain("没有可核对的原素材、原任务或原执行身份");
    },
  );

  it.each(restoreMatrix)(
    "pending=$pending settled=$settled operationId=$operationId reads only what the alert names",
    async (row) => {
      const record = restoreRecord(row.pending, row.settled, row.operationId);
      const { session, requests } = restoreWithRequests(record);
      const before = session.getSnapshot();
      const alert = unconfirmedAlertFor({ read: before.readTarget, replay: before.canReplay });
      await session.restore();
      // Ground truth: the routes the page actually hits. This is what "可用" means, and
      // it is the one thing the copy cannot influence.
      expect(namedReads(alert)).toEqual([...new Set(requests.map(hitTarget))]);
      expect(before.readTarget).toBe(requests.length === 0 ? null : hitTarget(requests[0]!));
    },
  );
});
