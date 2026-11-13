// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { canFinishTask } from "./file-task-policy";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal } from "./task-journal";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = () => parse("Job", example("queued-job"));
const asset = () => parse("Asset", example("ready-audio"));
const file = () => new File(["synthetic audio"], "synthetic.wav", { type: "audio/wav" });

type Responder = (attempt: number) => Response | Promise<Response>;

function rejection(status: number, code: string, extra: { operationId?: string } = {}) {
  const type =
    status === 429
      ? "rate_limit_error"
      : status >= 500
        ? "service_unavailable_error"
        : "invalid_request_error";
  return Response.json(
    parse("ErrorResponse", {
      error: {
        code,
        type,
        message: `Synthetic rejection ${code}`,
        param: null,
        request_id: "req-demo",
        retryable: status === 429 || status >= 500,
        ...(extra.operationId ? { nexus: { operation_id: extra.operationId } } : {}),
      },
    }),
    { status },
  );
}

const createOk: Responder = () => Response.json(job(), { status: 202 });
const uploadOk: Responder = () => Response.json(asset(), { status: 201 });

function setup({
  create = createOk,
  upload = uploadOk,
}: { create?: Responder; upload?: Responder } = {}) {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  let attempts = 0;
  let uploadAttempts = 0;
  let executions = 0;
  const createKeys: string[] = [];
  const bodies: string[] = [];
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") return upload(uploadAttempts++);
    if (path === "/v1/nexus/assets/asset-demo") return Response.json(asset());
    if (path === "/v1/nexus/jobs" && request.method === "POST") {
      createKeys.push(request.headers.get("Idempotency-Key") ?? "");
      bodies.push(await request.clone().text());
      const response = await create(attempts++);
      if (response.status === 202) executions++;
      return response;
    }
    throw new Error("Unexpected route");
  });
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  const stored = () => new SessionTaskJournal(storage, "task").load();
  return {
    make,
    stored,
    stats: {
      get executions() {
        return executions;
      },
      createKeys,
      bodies,
    },
  };
}

describe("submission rejection classes", () => {
  it("keeps one unknown outcome and its recovery identity for 503 job_outcome_unknown", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(503, "job_outcome_unknown")
          : Response.json(job(), { status: 202 }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    // The submission may already execute: the state must stay unknown and reconcilable.
    expect(snapshot.unknown).toBe(true);
    expect(snapshot.canReplay).toBe(true);
    expect(snapshot.recovery?.error.code).toBe("job_outcome_unknown");
    expect(snapshot.notice).toContain("读取原执行");
    expect(snapshot.notice).toContain("不要新建");
    // A fresh intent is the duplicated-execution path this card removes.
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "create", jobId: null });
    session.dispose();
  });

  it("replays the original key after 503 job_outcome_unknown instead of starting over", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(503, "job_outcome_unknown")
          : Response.json(job(), { status: 202 }),
    });
    const first = f.make();
    await first.start(file(), "asr-quality");
    expect(first.getSnapshot().unknown).toBe(true);
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    await restored.replay();
    expect(f.stats.createKeys).toHaveLength(2);
    expect(new Set(f.stats.createKeys).size).toBe(1);
    expect(f.stats.executions).toBe(1);
    expect(restored.getSnapshot()).toMatchObject({ unknown: false, canReplay: false });
    restored.dispose();
  });

  it("treats an unlisted 5xx create failure as an unknown outcome, never a spent intent", async () => {
    const f = setup({ create: () => rejection(500, "internal_error") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().recovery?.error.code).toBe("internal_error");
    expect(session.getSnapshot().notice).toContain("不要新建任务");
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    session.dispose();
  });

  it("never spends a 4xx intent whose code marks the outcome unknown", async () => {
    // A 409 is normally terminal (e.g. `idempotency_conflict`); both fields decide, so a
    // 4xx carrying an unknown-outcome code must not clear the unknown state.
    const f = setup({ create: () => rejection(409, "voice_draft_outcome_unknown") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    session.dispose();
  });

  it("queries an identified original execution instead of replaying it after 503 job_outcome_unknown", async () => {
    const f = setup({
      create: () => rejection(503, "job_outcome_unknown", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: false });
    expect(session.getSnapshot().notice).toContain("读取原执行");
    expect(f.stored()).toMatchObject({ operationId: "op-demo" });
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    session.dispose();
  });

  it("keeps the original intent retryable for a first 429 job_capacity_exhausted", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(429, "job_capacity_exhausted")
          : Response.json(job(), { status: 202 }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    // Nothing was admitted, so the durable intent stays fixed and replayable.
    expect(snapshot.unknown).toBe(false);
    expect(snapshot.canReplay).toBe(true);
    expect(snapshot.recovery).toBeUndefined();
    expect(snapshot.notice).toContain("原意图已保留");
    expect(snapshot.notice).toContain("不要新建任务");
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "create" });
    await session.replay();
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    expect(new Set(f.stats.createKeys).size).toBe(1);
    expect(f.stats.executions).toBe(1);
    session.dispose();
  });

  it("never clears an earlier unknown outcome when a replay is refused by 429 job_capacity_exhausted", async () => {
    const f = setup({
      create: (attempt) => {
        if (attempt === 0) throw new TypeError("Synthetic lost create response");
        return attempt === 1
          ? rejection(429, "job_capacity_exhausted")
          : Response.json(job(), { status: 202 });
      },
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot().unknown).toBe(true);
    await session.replay();
    const refused = session.getSnapshot();
    expect(refused.unknown).toBe(true);
    expect(refused.canReplay).toBe(true);
    expect(refused.notice).toContain("原意图已保留");
    expect(canFinishTask(refused)).toBe(false);
    await session.replay();
    expect(session.getSnapshot()).toMatchObject({ unknown: false, canReplay: false });
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    expect(new Set(f.stats.createKeys).size).toBe(1);
    expect(f.stats.executions).toBe(1);
    session.dispose();
  });

  it.each([
    [503, "job_unavailable"],
    [503, "job_admission_contended"],
    [503, "asset_inspection_busy"],
  ] as const)("retains the intent and the unknown state for %i %s", async (status, code) => {
    const f = setup({
      create: (attempt) => {
        if (attempt === 0) throw new TypeError("Synthetic lost create response");
        return attempt === 1 ? rejection(status, code) : Response.json(job(), { status: 202 });
      },
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    await session.replay();
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().notice).toContain("按原键重试");
    await session.replay();
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    expect(new Set(f.stats.createKeys).size).toBe(1);
    expect(f.stats.executions).toBe(1);
    session.dispose();
  });

  it("keeps the existing terminal behavior for a deterministic 422 rejection", async () => {
    const f = setup({ create: () => rejection(422, "job_text_asset_unsupported") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    // No Job row exists for a 4xx contract refusal, so the intent is spent and a new
    // intent is safe; this is the behavior that must not regress.
    expect(snapshot.unknown).toBe(false);
    expect(snapshot.canReplay).toBe(false);
    expect(snapshot.recovery).toBeUndefined();
    expect(snapshot.notice).toContain("job_text_asset_unsupported");
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    session.dispose();
  });

  it("keeps an explicit new-intent path for a first deterministic upload rejection", async () => {
    const f = setup({ upload: () => rejection(413, "asset_too_large") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({ uploadRejected: true, unknown: false, canReplay: false });
    expect(canFinishTask(snapshot)).toBe(true);
    expect(f.stats.createKeys).toHaveLength(0);
    session.dispose();
  });
});
