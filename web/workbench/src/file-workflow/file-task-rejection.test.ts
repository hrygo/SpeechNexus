// @vitest-environment node
import { describe, expect, it } from "vitest";
import {
  FileTaskSession,
  rejectionDisposition,
  retainRecovery,
  settlesCancelAttempt,
  settlesReplayedIntent,
} from "./file-task-session";
import { canFinishTask } from "./file-task-policy";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal } from "./task-journal";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = () => parse("Job", example("queued-job"));
const asset = () => parse("Asset", example("ready-audio"));
const file = () => new File(["synthetic audio"], "synthetic.wav", { type: "audio/wav" });

type Responder = (attempt: number) => Response | Promise<Response>;

function envelope(
  status: number,
  code: string,
  extra: { operationId?: string; receipt?: boolean } = {},
) {
  const type =
    status === 429
      ? "rate_limit_error"
      : status >= 500
        ? "service_unavailable_error"
        : "invalid_request_error";
  return parse("ErrorResponse", {
    error: {
      code,
      type,
      message: `Synthetic rejection ${code}`,
      param: null,
      request_id: "req-demo",
      retryable: status === 429 || status >= 500,
      ...(extra.operationId
        ? {
            nexus: {
              operation_id: extra.operationId,
              ...(extra.receipt
                ? {
                    receipt: {
                      receipt_id: "receipt-demo",
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
  });
}

function rejection(
  status: number,
  code: string,
  extra: { operationId?: string; receipt?: boolean } = {},
) {
  return Response.json(envelope(status, code, extra), { status });
}

const createOk: Responder = () => Response.json(job(), { status: 202 });
const uploadOk: Responder = () => Response.json(asset(), { status: 201 });

function setup({
  create = createOk,
  upload = uploadOk,
  cancel = () => {
    throw new Error("Unexpected cancellation request");
  },
  failWrite,
}: {
  create?: Responder;
  upload?: Responder;
  cancel?: Responder;
  /** Returns true for a write the fixture must refuse, e.g. a one-shot quota error. */
  failWrite?: (value: string) => boolean;
} = {}) {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (failWrite?.(value)) throw new DOMException("Synthetic quota", "QuotaExceededError");
      data.set(key, value);
    },
  };
  let attempts = 0;
  let uploadAttempts = 0;
  let executions = 0;
  let cancellations = 0;
  const createKeys: string[] = [];
  const bodies: string[] = [];
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") return upload(uploadAttempts++);
    if (path === "/v1/nexus/assets/asset-demo") return Response.json(asset());
    if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method === "POST") {
      cancellations++;
      return cancel(0);
    }
    if (path === "/v1/nexus/jobs/job-demo" && request.method === "GET") return Response.json(job());
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
    // The Host's explicit abandonment removes the recovery key; the journal keeps no
    // "current intent" of its own.
    clear: () => data.clear(),
    stats: {
      get executions() {
        return executions;
      },
      get cancellations() {
        return cancellations;
      },
      createKeys,
      bodies,
    },
  };
}

describe("replayed-intent settlement predicate", () => {
  it.each([
    // Refusals the protocol answers only after looking up the original key.
    [409, "idempotency_conflict", true],
    [422, "job_binding_incomplete", true],
    [422, "job_text_asset_unsupported", true],
    [422, "voice_not_published", true],
    [422, "model_not_published", true],
    [422, "profile_not_published", true],
    [422, "asset_not_ready", true],
    [422, "model_binding_retired", true],
    [422, "model_binding_task_not_supported", true],
    // Answers that may precede the key lookup (auth, gateway, body parsing): never settle.
    [401, "invalid_credentials", false],
    [403, "forbidden", false],
    [404, "job_not_found", false],
    [405, "method_not_allowed", false],
    [400, "invalid_job_request", false],
    // The create body above the deployment receive bound (#703). Unreplayable for this
    // intent, yet still outside the key-check whitelist: it is answered before the
    // protocol looks the original key up, so it proves nothing about an earlier
    // submission under that key (#707 review M1, #729).
    [413, "job_request_too_large", false],
    [422, "unsupported_parameter", false],
    [422, "workspace_not_found", false],
    [409, "operation_in_progress", false],
    [409, "voice_draft_outcome_unknown", false],
  ])("settles only %i %s after the key check: %s", (status, code, settles) => {
    expect(settlesReplayedIntent({ status, code })).toBe(settles);
  });
});

describe("submission rejection disposition", () => {
  // The disposition decides what the user is told: a terminal refusal of this
  // request, "the submission may already execute", "this attempt was refused
  // but the intent may still succeed", or the request-bytes refusal that is spent
  // for replay yet cannot claim anything about execution (#729). An unlisted 5xx
  // is unknown; a 4xx is a protocol refusal, never an unknown outcome.
  it.each([
    // Over the deployment receive bound (#703): deterministic for replay, but answered
    // before the key lookup, so it is its own third class rather than "terminal" (#729).
    [413, "job_request_too_large", "unreplayable"],
    // Other 4xx causes keep the same disposition: the document, not the transport.
    [400, "invalid_job_request", "terminal"],
    [413, "asset_too_large", "terminal"],
    [422, "unsupported_parameter", "terminal"],
    [409, "idempotency_conflict", "terminal"],
    // Answers that admit the submission may already be executing.
    [503, "job_outcome_unknown", "unknown-outcome"],
    [409, "voice_draft_outcome_unknown", "unknown-outcome"],
    // Refusals that leave the same intent replayable.
    [429, "job_capacity_exhausted", "intent-kept"],
    [503, "job_unavailable", "intent-kept"],
    [503, "job_admission_contended", "intent-kept"],
  ] as const)("%i %s is %s", (status, code, disposition) => {
    expect(rejectionDisposition(status, code, "create")).toBe(disposition);
  });

  it("does not let the body-bound refusal fall through to the unknown 5xx branch", () => {
    // A 5xx or a non-contract status is unknown because the request may have run.
    expect(rejectionDisposition(500, "job_request_too_large", "create")).toBe("unknown-outcome");
    // The declared pair is a refusal, so it must not inherit that classification.
    expect(rejectionDisposition(413, "job_request_too_large", "create")).toBe("unreplayable");
    expect(rejectionDisposition(413, "job_request_too_large", "create")).not.toBe(
      "unknown-outcome",
    );
    // Create-scoped: the upload path answers its own `asset_too_large`, and a cancel has
    // no durable create intent to spend.
    expect(rejectionDisposition(413, "job_request_too_large", "upload")).toBe("terminal");
    expect(rejectionDisposition(413, "job_request_too_large", "cancel")).toBe("terminal");
  });
});

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
    // A 4xx refusal settles this attempt (the protocol will not accept the same request
    // again), so the unknown flag may be cleared and a fresh intent needs its own key;
    // this is the behavior that must not regress.
    expect(snapshot.unknown).toBe(false);
    expect(snapshot.canReplay).toBe(false);
    expect(snapshot.recovery).toBeUndefined();
    expect(snapshot.notice).toContain("job_text_asset_unsupported");
    // The spent intent is now abandonable instead of holding the page forever (#707).
    expect(snapshot.rejected).toBe(true);
    expect(canFinishTask(snapshot)).toBe(true);
    // Only the observed refusal is persisted; a crash before any answer has no `settled`.
    expect(f.stored()).toMatchObject({ pending: "create", settled: "terminal", jobId: null });
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    session.dispose();
  });

  it("treats a body over the deployment receive bound as a refusal, not an unknown outcome", async () => {
    // #703 gave the create body ceiling its own declared answer: 413
    // `job_request_too_large`. It is a deterministic refusal of this request, so the
    // panel must name it as a rejection instead of the generic "the submission may
    // already have executed" message, and must not offer a replay whose body is the same
    // impossible request (#729). It still may not claim an earlier submission never
    // executed: the answer precedes the key lookup, so the verdict is `unreplayable`
    // rather than `terminal` (#707 review M1).
    const f = setup({ create: () => rejection(413, "job_request_too_large") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    expect(snapshot.notice).toContain("413");
    expect(snapshot.notice).toContain("job_request_too_large");
    expect(snapshot.notice).toContain("作废");
    expect(snapshot.notice).not.toContain("提交结果未知");
    // 已作废: the protocol refused, so the page must not claim the result is unconfirmed.
    expect(snapshot).toMatchObject({
      unknown: false,
      rejected: true,
      unreplayable: true,
      canReplay: false,
    });
    expect(snapshot.recovery).toBeUndefined();
    // 不声称已确证无执行: the refusal predates the key lookup, so no execution claim.
    expect(snapshot.notice).not.toContain("未执行");
    expect(snapshot.notice).not.toContain("尚未确认");
    // 可放弃: the explicit end-of-page path replaces a guaranteed-failing replay.
    expect(canFinishTask(snapshot)).toBe(true);
    expect(f.stored()).toMatchObject({
      pending: "create",
      settled: "unreplayable",
      jobId: null,
    });
    // Replaying is refused client-side: the same bytes would just be refused again.
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    expect(f.stats.executions).toBe(0);
    session.dispose();
  });

  it("keeps an earlier unknown outcome unknown when a replay is refused before the key check", async () => {
    // A lost answer and a refusal share one persisted shape (`pending: "create"`, no Job),
    // so a refusal that an auth layer can answer *before* the protocol looks up the
    // original key cannot settle the earlier attempt. Persisting it as final would let the
    // user abandon a possibly executed submission and start a duplicate (#707 review M1).
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(503, "job_outcome_unknown")
          : rejection(401, "invalid_credentials"),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });

    await session.replay();
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().rejected).toBeUndefined();
    expect(session.getSnapshot().notice).toContain("不能证明原提交未执行");
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    expect(f.stats.createKeys).toHaveLength(2);
    expect(new Set(f.stats.createKeys).size).toBe(1);
    expect(f.stored()).toMatchObject({ pending: "create" });
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();

    // Reloading must still rebuild the unknown submission, never a settled refusal.
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(restored.getSnapshot())).toBe(false);
    restored.dispose();
  });

  it("keeps a reloaded unknown outcome unknown when its replay is refused before the key check", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(503, "job_outcome_unknown")
          : rejection(401, "invalid_credentials"),
    });
    const first = f.make();
    await first.start(file(), "asr-quality");
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    await restored.replay();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(f.stored()).not.toHaveProperty("settled");
    restored.dispose();
  });

  it("settles a replayed intent when the refusal is emitted after the key check", async () => {
    // `job_binding_incomplete` comes from the admission path: `PublicJobs.submit` replays
    // the original key before it validates the intent and re-checks it before propagating
    // this refusal, so it also proves no earlier attempt under the key became a Job.
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(503, "job_outcome_unknown")
          : rejection(422, "job_binding_incomplete"),
    });
    const first = f.make();
    await first.start(file(), "asr-quality");
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot().unknown).toBe(true);
    await restored.replay();
    expect(restored.getSnapshot()).toMatchObject({
      unknown: false,
      rejected: true,
      canReplay: false,
    });
    expect(canFinishTask(restored.getSnapshot())).toBe(true);
    expect(f.stored()).toMatchObject({ pending: "create", settled: "terminal" });
    restored.dispose();
  });

  // #715: these three publication/asset 422s are resolved only after `PublicJobs.submit`
  // has read the original key, and the handler re-reads it before propagating them
  // (public_jobs.py:607-647; `_prepare_job` runs inside `_submit_new`). They are the same
  // class as `job_text_asset_unsupported`, so the spent intent must become abandonable
  // instead of staying an unknown submission whose only exit is a duplicate.
  it.each(["model_not_published", "profile_not_published", "asset_not_ready"] as const)(
    "settles a 422 %s refusal as an abandonable spent intent and keeps it settled after reload",
    async (code) => {
      const f = setup({ create: () => rejection(422, code) });
      const first = f.make();
      await first.start(file(), "asr-quality");
      const snapshot = first.getSnapshot();
      // 已作废: the protocol answered definitively, so no unknown state and no replay.
      expect(snapshot).toMatchObject({ unknown: false, rejected: true, canReplay: false });
      expect(snapshot.recovery).toBeUndefined();
      expect(snapshot.notice).toContain(code);
      // 可放弃: the page offers explicit abandonment instead of holding the intent forever.
      expect(canFinishTask(snapshot)).toBe(true);
      expect(f.stored()).toMatchObject({ pending: "create", settled: "terminal", jobId: null });
      first.dispose();

      // Reloading must rebuild the settled verdict, never revive it as an unknown submission.
      const restored = f.make();
      await restored.restore();
      expect(restored.getSnapshot()).toMatchObject({
        unknown: false,
        rejected: true,
        canReplay: false,
      });
      expect(restored.getSnapshot().recovery).toBeUndefined();
      expect(restored.getSnapshot().notice).toContain("确定性拒绝并作废");
      expect(canFinishTask(restored.getSnapshot())).toBe(true);
      // Neither restore nor a replay handle may submit the spent intent again.
      await restored.replay();
      expect(f.stats.createKeys).toHaveLength(1);
      expect(f.stats.executions).toBe(0);
      restored.dispose();
    },
  );

  // #729: 413 is answered by the receive layer before the original key is looked up, so it
  // cannot settle the intent (#707 M1) — yet replaying the same body can never succeed
  // either, so "unknown + replay" strands the user in a guaranteed-failing loop. The
  // refusal is about the request bytes: the intent is void for replay purposes only, and
  // the page must not claim the key has no Job.
  it("persists the 413 verdict so a reload still offers the exit", async () => {
    const f = setup({ create: () => rejection(413, "job_request_too_large") });
    const first = f.make();
    await first.start(file(), "asr-quality");
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({
      unknown: false,
      rejected: true,
      unreplayable: true,
      canReplay: false,
    });
    // The reload copy names the refusal and the void intent; it neither revives the
    // unknown state nor claims the refused submission never executed.
    expect(restored.getSnapshot().notice).toContain("作废");
    expect(restored.getSnapshot().notice).not.toContain("结果未知");
    expect(restored.getSnapshot().notice).not.toContain("未执行");
    expect(canFinishTask(restored.getSnapshot())).toBe(true);
    await restored.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    expect(f.stats.executions).toBe(0);
    restored.dispose();
  });

  it("keeps 401, 403 and 405 unknown and unabandonable on a first attempt", async () => {
    // Counterexample to broadening the new verdict: these may be answered before the key
    // lookup too, but unlike 413 the same request becomes meaningful after the environment
    // is fixed, so the original key must stay the only reconciliation (#707/#723).
    for (const [status, code] of [
      [401, "invalid_credentials"],
      [403, "forbidden"],
      [405, "method_not_allowed"],
    ] as const) {
      const f = setup({ create: () => rejection(status, code) });
      const session = f.make();
      await session.start(file(), "asr-quality");
      const snapshot = session.getSnapshot();
      expect(snapshot).toMatchObject({ unknown: true, canReplay: true });
      expect(snapshot.rejected).toBeUndefined();
      expect(snapshot.unreplayable).toBeUndefined();
      expect(snapshot.notice).toContain("不能证明原提交未执行");
      expect(canFinishTask(snapshot)).toBe(false);
      expect(f.stored()).toMatchObject({ pending: "create" });
      expect(f.stored()).not.toHaveProperty("settled");
      session.dispose();
    }
  });

  it("keeps the 413 crash window unknown: a lost answer records no verdict", async () => {
    // The refusal never arrived, so nothing may be persisted: a reload must rebuild the
    // unconfirmed submission instead of reading it as a spendable intent (#729 AC 3).
    const f = setup({
      create: () => {
        throw new TypeError("Synthetic lost 413 response");
      },
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({ unknown: true, canReplay: true });
    expect(snapshot.rejected).toBeUndefined();
    expect(snapshot.unreplayable).toBeUndefined();
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(restored.getSnapshot())).toBe(false);
    restored.dispose();
  });

  it("voids a 413 that answers the intent's only create request, even after a failed journal write", async () => {
    // #729 review M1 (A1-3b): `#submit` assigns `#record` in memory *before* it persists, so
    // judging by "a record exists" would read this retry as a second answer and pin a page
    // whose single create request is the one the 413 refused. The verdict must follow "a
    // request was really sent", which a failed local write does not do.
    let failedOnce = false;
    const f = setup({
      failWrite: (value) => {
        if (failedOnce || !value.includes('"pending":"create"')) return false;
        failedOnce = true;
        return true;
      },
      create: () => rejection(413, "job_request_too_large"),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(failedOnce).toBe(true);
    // The journal write failed, so no request left this page: the intent is still unconfirmed.
    expect(f.stats.createKeys).toHaveLength(0);
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(canFinishTask(session.getSnapshot())).toBe(false);

    await session.replay();
    // The only create request of this intent got the 413, so it is the first answer.
    expect(f.stats.createKeys).toHaveLength(1);
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({
      unknown: false,
      rejected: true,
      unreplayable: true,
      canReplay: false,
    });
    expect(snapshot.notice).not.toContain("此前已有提交尝试");
    expect(snapshot.notice).not.toContain("未执行");
    expect(canFinishTask(snapshot)).toBe(true);
    expect(f.stored()).toMatchObject({ settled: "unreplayable" });
    session.dispose();
  });

  it("keeps a 413 replay of an earlier unobserved attempt unknown instead of voiding it", async () => {
    // The dangerous shape: attempt 1 was sent, its answer was lost, so it may already have
    // become a Job. Attempt 2 replays the same key and is refused with 413, which the receive
    // layer answers *before* the key lookup. Voiding the intent here would let the user
    // abandon a possibly executed submission and start a duplicate -- the one thing #707
    // review M1 forbids -- so the verdict may only be written when this 413 is the intent's
    // first answer (#729).
    const f = setup({
      create: (attempt) => {
        if (attempt === 0) throw new TypeError("Synthetic lost create response");
        return rejection(413, "job_request_too_large");
      },
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });

    await session.replay();
    const refused = session.getSnapshot();
    // No exit is granted: the earlier attempt is still the unresolved question.
    expect(refused).toMatchObject({ unknown: true, canReplay: true });
    expect(refused.rejected).toBeUndefined();
    expect(refused.unreplayable).toBeUndefined();
    expect(canFinishTask(refused)).toBe(false);
    expect(refused.notice).toContain("已有提交尝试");
    expect(refused.notice).toContain("不能证明原提交未执行");
    expect(refused.notice).not.toContain("作废");
    // Nothing is persisted as a verdict, so a reload rebuilds the same unknown intent.
    expect(f.stored()).toMatchObject({ pending: "create", jobId: null });
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();

    const reloaded = f.make();
    await reloaded.restore();
    expect(reloaded.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(reloaded.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(reloaded.getSnapshot())).toBe(false);
    reloaded.dispose();
  });

  it("keeps an identified execution queryable when a 413 answers a second attempt", async () => {
    // A non-first 413 gains an identity: the guard withholds the exit, and the identity stays
    // the reconciliation instead of a replay that would be refused again (#729).
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(401, "invalid_credentials")
          : rejection(413, "job_request_too_large", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    await session.replay();
    const refused = session.getSnapshot();
    expect(refused).toMatchObject({ unknown: true, canReplay: false });
    expect(refused.rejected).toBeUndefined();
    expect(refused.unreplayable).toBeUndefined();
    expect(refused.recovery?.error.nexus?.operation_id).toBe("op-demo");
    expect(refused.notice).toContain("读取原执行");
    expect(canFinishTask(refused)).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it("does not void after a known refusal either, because the durable shape cannot tell them apart", async () => {
    // A 429 is a refusal that proves nothing executed, yet the journal keeps only
    // `pending: "create"` -- the same shape a lost answer leaves. The first-answer rule is
    // therefore applied to every non-first 413: the exit is withheld rather than risk
    // abandoning a submission whose earlier attempt a reload would still call unknown (#729).
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(429, "job_capacity_exhausted")
          : rejection(413, "job_request_too_large"),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    await session.replay();
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().unreplayable).toBeUndefined();
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it("never hides an identified execution behind the 413 verdict", async () => {
    // A 413 cannot normally name an execution, but an identity that is already known is
    // the only reconciliation left and must stay queryable (#707 M2).
    const f = setup({
      create: () => rejection(413, "job_request_too_large", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    // Query the identified execution; never replay the oversized body.
    expect(snapshot).toMatchObject({ unknown: true, canReplay: false });
    expect(snapshot.rejected).toBeUndefined();
    expect(snapshot.unreplayable).toBeUndefined();
    expect(snapshot.recovery?.error.nexus?.operation_id).toBe("op-demo");
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it("never settles a refusal that names an identified execution", async () => {
    // An envelope carrying `nexus.operation_id` identifies an execution the user could
    // still reconcile; a persisted settled verdict would hide that identity (#707 M2).
    const f = setup({
      create: () => rejection(409, "idempotency_conflict", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().rejected).toBeUndefined();
    expect(session.getSnapshot().recovery?.error.nexus?.operation_id).toBe("op-demo");
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it("keeps a body-level refusal unknown on a first attempt", async () => {
    // `unsupported_parameter` is answered while parsing the body, before the protocol looks
    // up the key (interfaces/http/jobs.py `_create`), so even a first refusal cannot rule
    // out a concurrent commit; it must not settle the durable intent.
    const f = setup({ create: () => rejection(422, "unsupported_parameter") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(session.getSnapshot())).toBe(false);
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it("never starts a replacement create while a terminally refused intent awaits explicit abandonment", async () => {
    const f = setup({ create: () => rejection(422, "job_binding_incomplete") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(canFinishTask(session.getSnapshot())).toBe(true);
    // #707 counterexample 3: the new-intent path is the Host's explicit abandonment, not
    // any silent retry. Starting over and continuing validation must both stay refused.
    await session.start(file(), "asr-quality");
    await session.continueAfterValidation();
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(1);
    expect(f.stored()).toMatchObject({ settled: "terminal" });
    session.dispose();
  });

  it("settles a 409 idempotency_conflict spent intent and only a new key can replace it", async () => {
    // That code identifies *another* intent's Job under this key: this attempt is spent,
    // but the replacement must not reuse the key or it would conflict again.
    const f = setup({ create: () => rejection(409, "idempotency_conflict") });
    const first = f.make();
    await first.start(file(), "asr-quality");
    expect(first.getSnapshot()).toMatchObject({ unknown: false, rejected: true, canReplay: false });
    const spentKey = f.stats.createKeys[0]!;
    expect(f.stored()).toMatchObject({ settled: "terminal" });
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({
      unknown: false,
      rejected: true,
      canReplay: false,
    });
    expect(f.stats.createKeys).toHaveLength(1);
    // Only the explicit abandonment removes the recovery record; then a fresh key is used.
    f.clear();
    const fresh = f.make();
    await fresh.start(file(), "asr-quality");
    expect(f.stats.createKeys).toHaveLength(2);
    expect(f.stats.createKeys[1]).not.toBe(spentKey);
    fresh.dispose();
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

  it("keeps the intent unknown when a create attempt is refused with 409 execution_cleanup_pending", async () => {
    // That code proves a Job already exists (contracts/speech-openapi.yaml:2852-2854), so on
    // a create attempt it says nothing about whether *this* intent was admitted.
    const f = setup({ create: () => rejection(409, "execution_cleanup_pending") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({ unknown: true, canReplay: true });
    expect(snapshot.recovery?.error.code).toBe("execution_cleanup_pending");
    expect(snapshot.notice).toContain("读取原执行");
    expect(canFinishTask(snapshot)).toBe(false);
    // It proves a Job exists, so it must never be recorded as a spent intent (#707).
    expect(snapshot.rejected).toBeUndefined();
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });

  it.each(["operation_in_progress", "response_not_replayable"] as const)(
    "keeps %s unknown against its identified original execution and never settles it",
    async (code) => {
      const f = setup({
        create: () =>
          rejection(409, code, {
            operationId: "op-demo",
            receipt: code === "response_not_replayable",
          }),
      });
      const session = f.make();
      await session.start(file(), "asr-quality");
      const snapshot = session.getSnapshot();
      expect(snapshot).toMatchObject({ unknown: true, canReplay: false });
      expect(snapshot.notice).toContain("不要创建替代执行");
      expect(f.stored()).toMatchObject({ pending: "create", operationId: "op-demo" });
      expect(f.stored()).not.toHaveProperty("settled");
      expect(snapshot.rejected).toBeUndefined();
      expect(canFinishTask(snapshot)).toBe(false);
      session.dispose();
    },
  );

  it("treats 409 execution_cleanup_pending on the cancel path as a settled attempt", async () => {
    // The cancel request was deterministically refused against a known Job identity, so the
    // same operation context that keeps a create unknown must clear it here (#691), and the
    // refusal is persisted as spent instead of reloading as an unknown cancellation (#723).
    const f = setup({ cancel: () => rejection(409, "execution_cleanup_pending") });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    await session.cancel();
    const snapshot = session.getSnapshot();
    expect(snapshot.unknown).toBe(false);
    expect(snapshot.canReplay).toBe(false);
    expect(snapshot.recovery).toBeUndefined();
    expect(snapshot.notice).toContain("execution_cleanup_pending");
    expect(snapshot.cancellation).toBeUndefined();
    // The refused cancel attempt is spent, so the page offers only an explicit end; the
    // Job it named stays readable through the ordinary refresh.
    expect(snapshot.rejected).toBe(true);
    expect(f.stored()).toMatchObject({
      pending: "cancel",
      settled: "terminal",
      jobId: "job-demo",
    });
    session.dispose();
  });

  it("keeps an earlier recovery envelope when a later 503 carries no operation identity", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0 ? rejection(500, "internal_error") : rejection(503, "job_outcome_unknown"),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot().recovery?.error.code).toBe("internal_error");
    await session.replay();
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({ unknown: true, canReplay: true });
    // The first envelope still describes the unresolved submission; the nexus-less 503
    // must not replace it with an identity-free one.
    expect(snapshot.recovery?.error.code).toBe("internal_error");
    expect(snapshot.recovery?.error.message).toBe("Synthetic rejection internal_error");
    session.dispose();
  });

  it("adopts a later operation identity so it stays queryable", async () => {
    const f = setup({
      create: (attempt) =>
        attempt === 0
          ? rejection(500, "internal_error")
          : rejection(503, "job_outcome_unknown", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    expect(session.getSnapshot().recovery?.error.nexus).toBeUndefined();
    await session.replay();
    const snapshot = session.getSnapshot();
    expect(snapshot.recovery?.error.nexus?.operation_id).toBe("op-demo");
    expect(snapshot.canReplay).toBe(false);
    expect(f.stored()).toMatchObject({ operationId: "op-demo" });
    await session.replay();
    expect(f.stats.createKeys).toHaveLength(2);
    session.dispose();
  });
});

describe("cancel-attempt settlement", () => {
  it.each([
    // Answers about the target Job: the attempt is spent, the Job stays readable.
    [409, "execution_cleanup_pending", true],
    [409, "idempotency_conflict", true],
    [404, "job_not_found", true],
    // Answers about the caller or the envelope; retrying after a fix is a real
    // reconciliation, so they may precede the cancellation being examined.
    [400, "invalid_job_request", false],
    [401, "invalid_credentials", false],
    [403, "forbidden", false],
    [405, "method_not_allowed", false],
    // Outcome-unknown classes are classified before this predicate and never reach it.
    [503, "job_outcome_unknown", false],
    [409, "voice_draft_outcome_unknown", false],
    [409, "operation_in_progress", false],
    [409, "response_not_replayable", false],
    [404, "asset_not_found", false],
  ])("settles only the Job answers on the cancel path: %i %s -> %s", (status, code, settles) => {
    expect(settlesCancelAttempt({ status, code })).toBe(settles);
  });

  it.each([
    [409, "execution_cleanup_pending"],
    [409, "idempotency_conflict"],
  ] as const)(
    "persists a cancel refusal answered after the key check as spent: %i %s",
    async (status, code) => {
      const f = setup({ cancel: () => rejection(status, code) });
      const first = f.make();
      await first.start(file(), "asr-quality");
      await first.cancel();
      expect(first.getSnapshot()).toMatchObject({
        unknown: false,
        rejected: true,
        canReplay: false,
      });
      expect(first.getSnapshot().notice).toContain(code);
      expect(canFinishTask(first.getSnapshot())).toBe(true);
      expect(f.stored()).toMatchObject({
        pending: "cancel",
        jobId: "job-demo",
        settled: "terminal",
      });
      first.dispose();

      // Counterexample 1: without the cancel class, this reload would rebuild an unknown
      // cancellation and show "结果未知" for a refusal the protocol already gave.
      const restored = f.make();
      await restored.restore();
      expect(restored.getSnapshot()).toMatchObject({
        unknown: false,
        rejected: true,
        canReplay: false,
        job: { id: "job-demo" },
      });
      expect(restored.getSnapshot().notice).not.toContain("结果未知");
      expect(canFinishTask(restored.getSnapshot())).toBe(true);
      // The spent intent is neither replayed nor re-cancelled; only its Job is read.
      await restored.replay();
      await restored.cancel();
      expect(f.stats.cancellations).toBe(1);
      expect(f.stats.createKeys).toHaveLength(1);
      expect(f.stored()).toMatchObject({ pending: "cancel", settled: "terminal" });
      restored.dispose();
    },
  );

  it("settles a cancel for a Job the protocol reports as absent", async () => {
    // `job_not_found` answers a question about the named Job: nothing left to reconcile,
    // so the intent is spent instead of stranding the page in unknown (#723).
    const f = setup({ cancel: () => rejection(404, "job_not_found") });
    const first = f.make();
    await first.start(file(), "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: false, rejected: true });
    expect(canFinishTask(first.getSnapshot())).toBe(true);
    expect(f.stored()).toMatchObject({ pending: "cancel", settled: "terminal" });
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: false, rejected: true });
    expect(restored.getSnapshot().notice).not.toContain("结果未知");
    await restored.cancel();
    expect(f.stats.cancellations).toBe(1);
    restored.dispose();
  });

  it("keeps a cancel refusal about the caller and envelope unknown", async () => {
    // A 401 can be answered before the cancellation is examined at all, so it cannot prove
    // the earlier attempt did not take effect; retrying after the environment is fixed is
    // the reconciliation (#707 review M1).
    const f = setup({ cancel: () => rejection(401, "invalid_credentials") });
    const first = f.make();
    await first.start(file(), "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(first.getSnapshot().rejected).toBeUndefined();
    expect(first.getSnapshot().notice).toContain("不能证明原取消未生效");
    expect(canFinishTask(first.getSnapshot())).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "cancel", jobId: "job-demo" });
    expect(f.stored()).not.toHaveProperty("settled");
    first.dispose();

    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(restored.getSnapshot())).toBe(false);
    restored.dispose();
  });

  it("keeps a lost cancellation response unknown and never records a settled refusal", async () => {
    // Counterexample 3: the request left but no answer arrived. Nothing observed a
    // refusal, so the record must stay in-flight and reload as unknown.
    const f = setup({
      cancel: () => {
        throw new TypeError("Synthetic lost cancellation response");
      },
    });
    const first = f.make();
    await first.start(file(), "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(first.getSnapshot().rejected).toBeUndefined();
    expect(canFinishTask(first.getSnapshot())).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "cancel", jobId: "job-demo" });
    expect(f.stored()).not.toHaveProperty("settled");
    first.dispose();

    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(f.stored()).not.toHaveProperty("settled");
    expect(canFinishTask(restored.getSnapshot())).toBe(false);
    // Retrying the original cancellation key is still the only reconciliation.
    await restored.replay();
    expect(f.stats.cancellations).toBe(2);
    restored.dispose();
  });

  it.each([
    [503, "job_outcome_unknown"],
    [409, "voice_draft_outcome_unknown"],
    [500, "internal_error"],
  ] as const)("never settles an unknown-outcome cancel: %i %s", async (status, code) => {
    const f = setup({ cancel: () => rejection(status, code) });
    const first = f.make();
    await first.start(file(), "asr-quality");
    await first.cancel();
    const snapshot = first.getSnapshot();
    expect(snapshot.unknown).toBe(true);
    expect(snapshot.rejected).toBeUndefined();
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "cancel" });
    expect(f.stored()).not.toHaveProperty("settled");
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    restored.dispose();
  });

  it("keeps a cancel intent that was refused before it acted replayable and un-settled", async () => {
    // 503 `job_unavailable` is `intent-kept`, not a verdict about this attempt: the same
    // intent may still succeed, so nothing is persisted as final (#723).
    const f = setup({ cancel: () => rejection(503, "job_unavailable") });
    const first = f.make();
    await first.start(file(), "asr-quality");
    await first.cancel();
    expect(first.getSnapshot()).toMatchObject({ unknown: false, canReplay: true });
    expect(first.getSnapshot().notice).toContain("原意图已保留");
    expect(first.getSnapshot().rejected).toBeUndefined();
    expect(f.stored()).toMatchObject({ pending: "cancel" });
    expect(f.stored()).not.toHaveProperty("settled");
    first.dispose();
    const restored = f.make();
    await restored.restore();
    expect(restored.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(restored.getSnapshot().rejected).toBeUndefined();
    expect(f.stored()).not.toHaveProperty("settled");
    restored.dispose();
  });

  it("never settles a cancel refusal that names an identified execution", async () => {
    // An envelope carrying `nexus.operation_id` identifies an execution the user could
    // still reconcile; persisting a settled verdict would hide that identity (#707 M2).
    const f = setup({
      cancel: () => rejection(409, "execution_cleanup_pending", { operationId: "op-demo" }),
    });
    const session = f.make();
    await session.start(file(), "asr-quality");
    await session.cancel();
    const snapshot = session.getSnapshot();
    expect(snapshot).toMatchObject({ unknown: true, canReplay: true });
    expect(snapshot.recovery?.error.nexus?.operation_id).toBe("op-demo");
    expect(snapshot.rejected).toBeUndefined();
    expect(canFinishTask(snapshot)).toBe(false);
    expect(f.stored()).toMatchObject({ pending: "cancel", operationId: null });
    expect(f.stored()).not.toHaveProperty("settled");
    session.dispose();
  });
});

describe("recovery identity retention", () => {
  it("never replaces an already identified original execution", () => {
    const identified = envelope(503, "job_outcome_unknown", { operationId: "op-first" });
    for (const incoming of [
      envelope(503, "job_outcome_unknown"),
      envelope(503, "job_outcome_unknown", { operationId: "op-second" }),
      envelope(500, "internal_error"),
      envelope(409, "operation_in_progress", { operationId: "op-second" }),
    ])
      expect(retainRecovery(identified, incoming)).toBe(identified);
  });

  it("keeps a later identity over an envelope without one", () => {
    const previous = envelope(500, "internal_error");
    const incoming = envelope(503, "job_outcome_unknown", { operationId: "op-demo" });
    expect(retainRecovery(previous, incoming)).toBe(incoming);
    expect(retainRecovery(undefined, incoming)).toBe(incoming);
  });

  it("keeps the first identity-free envelope when nothing identifies an execution", () => {
    const previous = envelope(500, "internal_error");
    const incoming = envelope(503, "job_outcome_unknown");
    expect(retainRecovery(previous, incoming)).toBe(previous);
    expect(retainRecovery(undefined, incoming)).toBe(incoming);
  });
});
