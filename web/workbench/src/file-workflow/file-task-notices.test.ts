// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession, readTargetAction, type ReadTarget } from "./file-task-session";
import { inputFingerprint } from "./input-fingerprint";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal } from "./task-journal";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

/**
 * The `#run` notices that ask for a read or a replay (#782). Before this card every branch
 * wrote its own object -- "请读取原任务" was hard-coded even when the only readable identity was
 * the input Asset -- so `role=status` and the `role=alert` beside it pointed at two different
 * targets on one page. Each case below is a real session, and the read its notice claims is
 * compared with the route a real `restore()` issues, which copy cannot influence.
 */

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = () => parse("Job", example("queued-job"));
const asset = () => parse("Asset", example("ready-audio"));
const file = () => new File(["synthetic audio"], "synthetic.wav", { type: "audio/wav" });
const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

type Responder = (attempt: number) => Response | Promise<Response>;

function envelope(status: number, code: string, operationId?: string) {
  return parse("ErrorResponse", {
    error: {
      code,
      type:
        status === 429
          ? "rate_limit_error"
          : status >= 500
            ? "service_unavailable_error"
            : "invalid_request_error",
      message: `Synthetic rejection ${code}`,
      param: null,
      request_id: "req-demo",
      retryable: status === 429 || status >= 500,
      ...(operationId ? { nexus: { operation_id: operationId } } : {}),
    },
  });
}

function refusal(status: number, code: string, operationId?: string) {
  return Response.json(envelope(status, code, operationId), { status });
}

/** The upload recovery error the client raises for an in-flight or unknown upload. */
function uploadUnknown(location: string | null) {
  return Response.json(envelope(503, "asset_upload_unknown"), {
    status: 503,
    headers: location ? { Location: location } : {},
  });
}

const lostAnswer = (): never => {
  throw new TypeError("Synthetic lost answer");
};

interface HarnessOptions {
  create?: Responder;
  upload?: Responder;
  cancel?: Responder;
  /** Every read fails at the transport, like an offline page. */
  readFailure?: boolean;
  writable?: (value: string) => boolean;
}

function harness({
  create = () => Response.json(job(), { status: 202 }),
  upload = () => Response.json(asset(), { status: 201 }),
  cancel = () => Response.json(parse("JobCancellation", example("cancel-job")), { status: 202 }),
  readFailure = false,
  writable = () => true,
}: HarnessOptions = {}) {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (!writable(value)) throw new DOMException("Synthetic quota", "QuotaExceededError");
      data.set(key, value);
    },
  };
  /** The read routes the page really requested, in order; a copy claim cannot fake them. */
  const reads: ReadTarget[] = [];
  let createAttempts = 0;
  let uploadAttempts = 0;
  let cancelAttempts = 0;
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") return upload(uploadAttempts++);
    if (path === "/v1/nexus/assets/asset-demo/content")
      throw new TypeError("Synthetic unreadable text asset");
    if (path === "/v1/nexus/assets/asset-demo") {
      reads.push("asset");
      if (readFailure) throw new TypeError("Synthetic offline page");
      return Response.json(asset());
    }
    if (path.startsWith("/v1/nexus/operations/")) {
      reads.push("operation");
      if (readFailure) throw new TypeError("Synthetic offline page");
      // No `job_id`: the operation read stays a single request, so the measurement is the
      // read dispatch itself.
      return Response.json({ ...example("operation"), job_id: null });
    }
    if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method === "POST")
      return cancel(cancelAttempts++);
    if (path === "/v1/nexus/jobs/job-demo") {
      reads.push("job");
      if (readFailure) throw new TypeError("Synthetic offline page");
      return Response.json(job());
    }
    if (path === "/v1/nexus/jobs" && request.method === "POST") return create(createAttempts++);
    throw new Error(`Unexpected route ${request.method} ${path}`);
  });
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  return {
    make,
    reads,
    /** How many mutation requests the page really sent, so a disabled action is measurable. */
    creates: () => createAttempts,
    cancels: () => cancelAttempts,
    record: () => new SessionTaskJournal(storage, "task").load(),
    /** Put a durable record in storage before constructing the session (a reload). */
    seed: (record: string) => data.set("task", record),
  };
}

function crashWindowRecord() {
  return {
    version: 1,
    model: "asr-quality",
    assetId: "asset-demo",
    workspaceId: "ws-default",
    createKey: uuid(1),
    cancelKey: uuid(2),
    jobId: null,
    pending: "create",
    operationId: null,
    cursor: null,
  };
}

async function pendingUploadRecord() {
  return JSON.stringify({
    version: 3,
    model: "asr-quality",
    assetId: null,
    workspaceId: null,
    createKey: uuid(1),
    cancelKey: uuid(2),
    jobId: null,
    pending: "upload",
    operationId: null,
    cursor: null,
    uploadKey: uuid(3),
    inputFingerprint: await inputFingerprint(file()),
    assetMediaType: null,
    assetBytes: null,
    assetSha256: null,
  });
}

/**
 * The read targets a rendered sentence names, read from the product's own phrase table. The
 * table is exported by the session, so a test cannot hold a second copy that a wording change
 * would leave stale.
 */
function namedReads(copy: string): ReadTarget[] {
  return (Object.keys(readTargetAction) as ReadTarget[]).filter((target) =>
    copy.includes(readTargetAction[target]),
  );
}

/**
 * Every read verb in a sentence, in whatever wording it was written. The phrase table is the
 * only place a read may be worded, so a hand-written "请读取原任务" (or a vaguer "请读取素材
 * 状态") is caught even when it does not match any table entry: it is a second action
 * judgement, which is the defect this card removes (#782).
 */
const READ_VERB = /(?:读取|核对)原(?:执行|任务|素材)(?:核对)?|读取素材状态|恢复读取/g;

interface Case {
  readonly name: string;
  /** The protocol answers this state needs, so the case builds it for real. */
  readonly options: HarnessOptions;
  /** The handle the state *should* enable; declared, not read off the copy. */
  readonly readTarget: ReadTarget | null;
  readonly canReplay: boolean;
  readonly unknown: boolean;
  readonly build: (
    h: ReturnType<typeof harness>,
  ) => Promise<{ session: FileTaskSession; reads: readonly ReadTarget[] }>;
}

const cases: readonly Case[] = [
  {
    name: "lost create answer: the only read is the confirmed input Asset",
    options: { create: lostAnswer },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "crash window restored, then a replay that loses its answer again",
    options: { create: lostAnswer },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      h.seed(JSON.stringify(crashWindowRecord()));
      const session = h.make();
      await session.replay();
      return { session, reads: [] };
    },
  },
  {
    name: "413 non-first answer: the refusal cannot void a possibly executed submission",
    options: {
      create: (attempt) => (attempt === 0 ? lostAnswer() : refusal(413, "job_request_too_large")),
    },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      await session.replay();
      return { session, reads: [] };
    },
  },
  {
    name: "503 job_outcome_unknown without an execution identity",
    options: { create: () => refusal(503, "job_outcome_unknown") },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "503 job_outcome_unknown with an identified original execution",
    options: { create: () => refusal(503, "job_outcome_unknown", "op-demo") },
    readTarget: "operation",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "409 operation_in_progress against the identified original execution",
    options: { create: () => refusal(409, "operation_in_progress", "op-demo") },
    readTarget: "operation",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "409 idempotency_conflict that names the original execution is queried, not replayed",
    options: { create: () => refusal(409, "idempotency_conflict", "op-demo") },
    readTarget: "operation",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      // #782 review M1: the record's key still looks replayable, but the kept envelope names
      // the execution, so a replay must send nothing -- the key could only be refused again
      // before it reached that execution.
      await session.replay();
      expect(h.creates()).toBe(1);
      return { session, reads: [] };
    },
  },
  {
    name: "409 job_retry_not_allowed that names the original execution is queried, not replayed",
    options: { cancel: () => refusal(409, "job_retry_not_allowed", "op-demo") },
    readTarget: "operation",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      await session.cancel();
      await session.cancel();
      expect(h.cancels()).toBe(1);
      return { session, reads: [] };
    },
  },
  {
    name: "413 first answer that still names the original execution",
    options: { create: () => refusal(413, "job_request_too_large", "op-demo") },
    readTarget: "operation",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "503 job_outcome_unknown on a pending cancellation",
    options: { cancel: () => refusal(503, "job_outcome_unknown") },
    readTarget: "job",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      await session.cancel();
      return { session, reads: [] };
    },
  },
  {
    name: "409 job_retry_not_allowed on a pending cancellation",
    options: { cancel: () => refusal(409, "job_retry_not_allowed") },
    readTarget: "job",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      await session.cancel();
      return { session, reads: [] };
    },
  },
  {
    name: "create refusal about the caller (401): keep the key and the input Asset",
    options: { create: () => refusal(401, "invalid_credentials") },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "429 job_capacity_exhausted keeps the intent replayable",
    options: { create: () => refusal(429, "job_capacity_exhausted") },
    readTarget: "asset",
    canReplay: true,
    unknown: false,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "unconfirmed upload that named a recoverable Asset hint",
    options: { upload: () => uploadUnknown("/v1/nexus/assets/asset-demo") },
    readTarget: "asset",
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "unconfirmed upload with no Asset identity at all",
    options: { upload: () => uploadUnknown(null) },
    readTarget: null,
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "unconfirmed upload whose recovery hint could not be saved",
    options: {
      upload: () => uploadUnknown("/v1/nexus/assets/asset-demo"),
      writable: (value) => !value.includes('"version":5'),
    },
    readTarget: null,
    canReplay: false,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "restored unconfirmed upload, refused while its input is re-provided",
    options: { upload: () => refusal(429, "job_capacity_exhausted") },
    readTarget: null,
    canReplay: false,
    unknown: true,
    build: async (h) => {
      h.seed(await pendingUploadRecord());
      const session = h.make();
      await session.resumeFile(file());
      return { session, reads: [] };
    },
  },
  {
    name: "input that disagrees with the original upload fingerprint",
    options: {},
    readTarget: null,
    canReplay: false,
    unknown: true,
    build: async (h) => {
      h.seed(await pendingUploadRecord());
      const session = h.make();
      await session.resumeFile(new File(["different audio"], "different.wav"));
      return { session, reads: [] };
    },
  },
  {
    name: "restored create whose original text cannot be read back",
    options: {},
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      h.seed(
        JSON.stringify({
          ...crashWindowRecord(),
          version: 2,
          voiceId: "voice-demo",
          voiceRevision: "1",
          profileId: "profile-demo",
          profileRevision: "1",
        }),
      );
      const session = h.make();
      await session.replay();
      return { session, reads: [] };
    },
  },
  {
    name: "local journal failure before any identity was kept",
    options: { writable: () => false },
    readTarget: null,
    canReplay: true,
    unknown: true,
    build: async (h) => {
      const session = h.make();
      await session.start(file(), "asr-quality");
      return { session, reads: [] };
    },
  },
  {
    name: "failed read of the restored crash window",
    options: { readFailure: true },
    readTarget: "asset",
    canReplay: true,
    unknown: true,
    build: async (h) => {
      h.seed(JSON.stringify(crashWindowRecord()));
      const session = h.make();
      await session.restore();
      // The failed restore already requested the Asset; the measurement below only re-reads
      // this snapshot through the same affordance.
      return { session, reads: [...new Set(h.reads)] };
    },
  },
];

describe("#run notices name exactly the actions this snapshot enables", () => {
  for (const row of cases) {
    it(row.name, async () => {
      const h = harness(row.options);
      const built = await row.build(h);
      const state = built.session.getSnapshot();
      // The declared shape is the oracle: a case that built another state fails here instead
      // of proving something about unrelated copy.
      expect({
        readTarget: state.readTarget,
        canReplay: state.canReplay,
        unknown: state.unknown,
      }).toEqual({ readTarget: row.readTarget, canReplay: row.canReplay, unknown: row.unknown });

      const copy = state.notice;
      // The read the copy names is the one the page holds, and no other ...
      expect(namedReads(copy)).toEqual(row.readTarget === null ? [] : [row.readTarget]);
      // ... and it is worded by the shared table, not by whatever verb the branch prefers.
      expect(copy.match(READ_VERB) ?? []).toEqual(
        row.readTarget === null ? [] : [readTargetAction[row.readTarget]],
      );
      // The replay sentence tracks the enabled replay in both directions.
      expect(copy.includes("重放")).toBe(row.canReplay);

      // Ground truth for "可用": the route the one read affordance really issues. This is
      // what a hand-written target cannot fake.
      h.reads.length = 0;
      await built.session.restore();
      expect([...new Set(h.reads)]).toEqual(
        row.readTarget === null ? built.reads : [row.readTarget],
      );
      built.session.dispose();
    });
  }
});

/**
 * The six combinations the cases above build. This is a coverage table over those cases, not a
 * proof that the state machine has no other shape: it is a set of the declared rows, so it can
 * only catch a row that starts declaring an unexpected pair, never an unbuilt state.
 * `operation` never carries a replay here because that is now *enforced*: an identified
 * execution is queried, `canReplay` is `!operationId` on the live path (including
 * `refusalProvesNothing`, #782 review M1) and requires `operationId === null` on the restored
 * path; the two `409 ... names the original execution` cases pin both directions. A Job without
 * a replay does not occur while the outcome is unknown -- an unconfirmed cancellation always
 * holds its original key -- and the only Job-naming shape that also names an Operation reads as
 * `operation`.
 */
describe("the case table still reflects the shapes these branches build", () => {
  it("declares exactly the six (readTarget, canReplay) pairs above", () => {
    const pairs = [...new Set(cases.map((row) => `${row.readTarget}:${row.canReplay}`))].sort();
    expect(pairs).toEqual([
      "asset:false",
      "asset:true",
      "job:true",
      "null:false",
      "null:true",
      "operation:false",
    ]);
  });
});

describe("status lines that do not describe an unconfirmed outcome", () => {
  it("asks for the Asset read when the accepted input is not ready yet", async () => {
    const pending = { ...asset(), status: "staging" };
    const h = harness({ upload: () => Response.json(pending, { status: 201 }) });
    const session = h.make();
    await session.start(file(), "asr-quality");
    const state = session.getSnapshot();
    expect(state.readTarget).toBe("asset");
    expect(state.canReplay).toBe(false);
    expect(namedReads(state.notice)).toEqual(["asset"]);
    expect(state.notice.match(READ_VERB) ?? []).toEqual([readTargetAction.asset]);
    // The page really reads that Asset, so the request in the copy is performable.
    h.reads.length = 0;
    await session.restore();
    expect(h.reads).toEqual(["asset"]);
    session.dispose();
  });

  it("offers the accepted Job's read and the event stream, nothing else", async () => {
    const h = harness();
    const session = h.make();
    await session.start(file(), "asr-quality");
    const state = session.getSnapshot();
    expect(state.readTarget).toBe("job");
    expect(namedReads(state.notice)).toEqual(["job"]);
    expect(state.notice.match(READ_VERB) ?? []).toEqual([readTargetAction.job]);
    expect(state.notice).not.toContain("重放");
    h.reads.length = 0;
    await session.restore();
    expect(h.reads).toEqual(["job"]);
    session.dispose();
  });
});
