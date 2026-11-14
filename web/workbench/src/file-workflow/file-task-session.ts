import {
  type SpeechFileClient,
  SpeechResponseError,
  SpeechUploadRecoveryError,
  parse,
  type FileContracts,
} from "./speech-file-client";
import {
  jobCommand,
  TaskJournalError,
  type FileTaskJournal,
  type TaskRecord,
  type TtsSelection,
  type UploadRecord,
  uploadJobRecord,
  isUploadRecord,
  uploadHint,
} from "./task-journal";
import { inputFingerprint, textUploadFile, UploadInputMismatchError } from "./input-fingerprint";

const assetIdentityFields = [
  "id",
  "workspace_id",
  "purpose",
  "media_type",
  "bytes",
  "sha256",
] as const;

/** The durable intent's original text could not be read back; no substitute may be sent. */
class OriginalTextUnavailableError extends Error {
  constructor() {
    super("Original text asset is not readable");
  }
}

/**
 * What a rejected submission settles. `terminal` means the protocol will not accept this
 * same request again (a fresh intent needs its own key); it does not assert that the key
 * has no Job row. `unknown-outcome` means the submission may already execute,
 * `intent-kept` means this attempt was refused but the same intent may still succeed, and
 * `unreplayable` means the refusal is about the bytes of this request: replay is futile
 * by construction, yet the refusal cannot speak for an earlier attempt either (#729).
 */
export type RejectionDisposition = "terminal" | "unknown-outcome" | "intent-kept" | "unreplayable";

/** Which submission class was in flight; some codes only settle one of them. */
export type RejectionOperation = "create" | "cancel" | "upload";

/**
 * Dispositions tied to an exact (HTTP status, contract error code) pair. The public
 * protocol binds each code to one status, so neither field alone classifies a rejection:
 * - 503 `job_outcome_unknown`: the submission may already be executing; the contract
 *   requires keeping the original key and request, then replaying or querying the
 *   original Job (contracts/speech-openapi.yaml:840-842).
 * - 429 `job_capacity_exhausted`: admission writes no row and asks for a Retry-After
 *   delay (contracts/speech-openapi.yaml:823-824).
 * - 503 `job_unavailable`: storage refused this attempt without reporting a Job fact.
 *   The public Job error table marks exactly `job_outcome_unknown`,
 *   `job_capacity_exhausted` and `job_admission_contended` retryable
 *   (backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:129-134), while
 *   `job_unavailable` itself is `retryable: false` (same file:113). The kept intent is
 *   still replayed under its original key, which cannot duplicate an execution; the
 *   disposition is named `intent-kept` rather than `retryable` so that the protocol's
 *   retry permission (`ServiceUnavailable`: "retry only when `retryable` is true",
 *   contracts/speech-openapi.yaml:2897-2901) is not confused with "the durable intent
 *   survives". The client does not consume `error.retryable` at all.
 * - 503 `job_admission_contended`: admission state changed concurrently; retryable by
 *   the same public Job error table.
 * - 503 `asset_inspection_busy`: the only Asset 503 whose "a new request later may
 *   succeed" statement holds (contracts/speech-openapi.yaml:2682-2689).
 * 413 `job_request_too_large` (#703) is deliberately not in this table: unlike every
 * entry above it is a third class, handled by `unreplayable` in `rejectionDisposition`
 * below, because no single disposition answers both questions it raises (#729).
 * - 422 `workspace_not_configured`: the workspace exists but has no provisioned quota
 *   row, so admission has nothing to charge (#702). It is *not* terminal: provisioning
 *   the workspace admits the same body under the same idempotency key, so the durable
 *   intent must stay replayable (`intent-kept`). It is deliberately kept apart from the
 *   503 `job_unavailable` storage outage: both keep the intent, but the status and code
 *   tell the user which refusal happened. It is absent from `settlesReplayedIntent`
 *   below, which only spends a create intent.
 */
const rejectionTable = [
  [503, "job_outcome_unknown", "unknown-outcome"],
  [503, "job_unavailable", "intent-kept"],
  [503, "job_admission_contended", "intent-kept"],
  [503, "asset_inspection_busy", "intent-kept"],
  [429, "job_capacity_exhausted", "intent-kept"],
  // A missing quota row is a configuration gap, not a closed door: after an operator
  // registers an engine for the workspace, this same body under this same key is
  // admitted, so spending the intent would strand a replayable request (#702 review M1).
  [422, "workspace_not_configured", "intent-kept"],
] as const satisfies readonly (readonly [number, string, RejectionDisposition])[];

/** Exported for the disposition table's own tests; the session is the only caller. */
export function rejectionDisposition(
  status: number,
  code: string,
  operation: RejectionOperation,
): RejectionDisposition {
  const known = rejectionTable.find(([knownStatus, knownCode]) => {
    return knownStatus === status && knownCode === code;
  });
  if (known) return known[2];
  // A `*unknown` code always means the outcome is unknown, whatever its status: the
  // protocol also uses 4xx for it, e.g. 409 `voice_draft_outcome_unknown` and
  // `pronunciation_outcome_unknown`, whose submissions may already have committed
  // (contracts/speech-openapi.yaml:2950-2954, 3177-3180).
  if (code.endsWith("unknown")) return "unknown-outcome";
  // 429 is a rate refusal that names its own delay, never a spent intent.
  if (status === 429) return "intent-kept";
  // 409 `execution_cleanup_pending` means a Job already exists whose original execution
  // has not confirmed stop (contracts/speech-openapi.yaml:2852-2854), so it settles only
  // the cancel attempt this client sends against that known Job identity. The backend
  // emits it from cancel/retry handlers only (backend/.../interfaces/http/jobs.py:607),
  // and this client never calls retry. The generic create-path 409 component also lists
  // "cleanup pending" (contracts/speech-openapi.yaml:2873-2878), so if a future backend
  // answered a create with it, it would say nothing about whether *this* intent was
  // admitted: keep the intent unknown instead of spending it.
  if (operation !== "cancel" && status === 409 && code === "execution_cleanup_pending")
    return "unknown-outcome";
  // 413 `job_request_too_large` is the one refusal that is about the bytes of *this
  // request* rather than about the intent under the key, and it is the only class that a
  // terminal-versus-unknown choice cannot answer without lying (#729):
  // - `unknown-outcome` would keep one action, replaying the same body under the key. The
  //   body cannot change, so the answer can never change either: the user is pinned to a
  //   loop whose only offered action is guaranteed to fail (#703/#707).
  // - `terminal` would clear the intent, and the page would then present it as settled —
  //   but the receive layer answers 413 before it looks the original key up, so it proves
  //   nothing about an earlier submission under that key (#707 review M1).
  // The disposition is therefore a third class: settled as a *replay* question, unsettled
  // as an *execution* question. Create-scoped like the 409 rule above, because the stable
  // code names the Job request; the upload path answers its own `asset_too_large`.
  if (status === 413 && code === "job_request_too_large" && operation === "create")
    return "unreplayable";
  // Any other 4xx contract refusal settles *this attempt*: the protocol does not accept
  // the same request again (a fresh intent needs its own key), so the unknown flag may be
  // cleared. Persisting that verdict for a create intent additionally requires
  // `settlesReplayedIntent` below, which removes the attempts that may have been sent
  // before the protocol checked the original key. This is not a claim that the key has no
  // Job row: 409 `idempotency_conflict` identifies another intent's Job under the same key
  // (contracts/speech-openapi.yaml:803-805), and duplicate execution stays impossible
  // because replaying this body under the key is precisely what conflicts.
  if (status >= 400 && status < 500) return "terminal";
  // An unlisted 5xx (or non-contract status) may already have executed; never spend the
  // intent on it.
  return "unknown-outcome";
}

/**
 * Admission refusals of the public create path that the protocol emits only after it has
 * looked up the original idempotency key. `PublicJobs.submit` replays a known key before
 * touching the intent and checks it again before propagating a validation refusal
 * (backend/src/speechnexus_backend/speech/application/public_jobs.py:607-647), while the
 * body-level answers (`invalid_job_request`, `unsupported_parameter`) are produced before
 * any key lookup (backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:442-461).
 *
 * For these codes a refusal therefore also proves that an earlier attempt under the same
 * key never became a Job. Every other 4xx may be answered before the replay check — a
 * gateway 401/403/404/405 or a body-level 400/413 — so it says nothing about an earlier
 * submission whose answer was never observed, and must leave the intent unknown (#707
 * review M1). The list only covers `asr`/`tts` creates, the only kinds this client issues;
 * the voice-draft kinds resolve part of their binding before the replay.
 *
 * 413 stays out of this list, and must: the receive layer answers it before the key lookup,
 * so it proves nothing about an earlier attempt. It is not thereby unrecoverable — the
 * exit comes from `rejectionDisposition`'s `unreplayable` verdict, which records that the
 * *request* can never be accepted again without pretending the key has no Job (#729).
 *
 * The publication/asset 422s belong to the same class even though they read mutable facts:
 * `_submit_new` → `_prepare_job` resolves them only after the initial replay
 * (:1046, :741-880), its `except` re-reads the key before re-raising (:624-647), and the
 * contract itself puts the original-key replay before mutable publication facts
 * (contracts/speech-openapi.yaml:858-863). Emission points:
 * - `model_not_published`: :783 (model is not a string), :801 (no published binding),
 *   :805 (binding lacks the required task), :837 (no catalogue revision).
 * - `profile_not_published`: :822 (profile revision is not published).
 * - `asset_not_ready`: `_require_inputs` (:833 → :1379-1431), plus the admission store's
 *   own `asset_not_ready` rejection (:1468-1488), which runs inside the admission
 *   transaction, i.e. even later than the key check (#715).
 *
 * `workspace_not_configured` is deliberately absent: the admission store emits it after
 * the key check, so it *would* prove that no earlier attempt became a Job, but it is a
 * configuration refusal rather than a closed door -- replaying the same key succeeds once
 * the workspace has a quota row -- so the intent must not be spent (#702 review M1).
 */
export function settlesReplayedIntent(error: {
  readonly status: number;
  readonly code: string;
}): boolean {
  if (error.status === 409) return error.code === "idempotency_conflict";
  if (error.status !== 422) return false;
  return (
    error.code === "job_text_asset_unsupported" ||
    error.code === "voice_not_published" ||
    error.code === "model_not_published" ||
    error.code === "profile_not_published" ||
    error.code === "asset_not_ready" ||
    error.code.startsWith("job_binding_") ||
    error.code.startsWith("model_binding_")
  );
}

/**
 * Cancel refusals that answer a question about the target Job, so they settle the
 * durable attempt instead of leaving it unknown. The record always names its Job, which
 * stays readable, so the persisted verdict can only be "this cancellation request is
 * spent", never "this Job is forgotten".
 * - 409 `execution_cleanup_pending`: the Job is terminal and its execution has not
 *   confirmed stop. `PostgresPublicJobs.cancel` reports it right after it locked the Job
 *   and resolved this workspace's cancellation key
 *   (backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:751-768),
 *   and #691 already reads that context as a conclusive answer on the cancel path.
 * - 409 `idempotency_conflict`: the key lookup itself refused (same method:758-765), so a
 *   different cancellation owns this key and no reply of this attempt can change that.
 * - 404 `job_not_found`: for a Job this deployment cannot see, so nothing left to
 *   reconcile. That reading rests on the current cancel implementation, not on the
 *   contract: the handler calls `service.cancel` without a workspace
 *   (backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:600-614, 794-808),
 *   `PublicJobs.cancel` defaults `workspace_id=None`
 *   (backend/src/speechnexus_backend/speech/application/public_jobs.py:1274-1285), and
 *   `_lock_cancel_target` adds the workspace predicate only when it is not None
 *   (backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:1014-1031),
 *   so the lookup is by job id alone. `speech_jobs` rows are never deleted, so the id this
 *   record holds cannot disappear; the same `job_not_found` also covers a malformed id
 *   (`ValueError`, …/http/jobs.py:612-613), which this record cannot carry because its
 *   `jobId` came from a parsed `Job` resource. Residual risk: the contract also allows 404
 *   for "Jobs outside the caller's workspace" (contracts/speech-openapi.yaml:990), so if
 *   the backend ever scopes this lookup by workspace, a 404 stops proving absence and this
 *   settlement would mark a still-cancellable Job as spent. The same premise already
 *   carries the lost-response replay: two POSTs under one key, the second answered 404,
 *   settle here, which is safe only while the lookup ignores the workspace and Job rows
 *   never vanish.
 * The remaining cancel 4xx describe the *caller*, not the Job — 400
 * `invalid_job_request`, 401/403 authentication or authorization, 405 routing
 * (contracts/speech-openapi.yaml:1024-1031) — and can be answered before the request is
 * examined, so retrying after the environment is fixed is a real reconciliation and they
 * stay unknown (#707 review M1). Every outcome-unknown class and every 5xx is classified
 * before this predicate and keeps its unknown state as well.
 */
export function settlesCancelAttempt(error: {
  readonly status: number;
  readonly code: string;
}): boolean {
  if (error.status === 404) return error.code === "job_not_found";
  return (
    error.status === 409 &&
    (error.code === "execution_cleanup_pending" || error.code === "idempotency_conflict")
  );
}

/**
 * Which refusal envelope becomes the visible recovery identity. A later refusal never
 * replaces an already identified original execution, and an envelope carrying
 * `nexus.operation_id` is kept over one without it so the queryable identity is not lost.
 */
export function retainRecovery(
  previous: FileContracts["ErrorResponse"] | undefined,
  incoming: FileContracts["ErrorResponse"],
): FileContracts["ErrorResponse"] {
  const identity = (value: FileContracts["ErrorResponse"] | undefined) =>
    value?.error.nexus?.operation_id;
  if (identity(previous)) return previous!;
  if (identity(incoming)) return incoming;
  return previous ?? incoming;
}

interface TaskView {
  readonly busy: boolean;
  readonly inputFixed: boolean;
  readonly unknown: boolean;
  readonly canReplay: boolean;
  readonly requiresInput: boolean;
  readonly uploadRejected?: boolean;
  /**
   * The protocol definitively refused this attempt, so the durable intent is spent: it
   * can be neither replayed nor replaced. Only explicit abandonment ends it; a Job the
   * record names stays readable, but its cancel intent cannot be sent again. Never set
   * for an unknown outcome, which stays reconcilable by its original key.
   */
  readonly rejected?: boolean;
  /**
   * The refusal was about the bytes of this request (413), so the durable intent can never
   * be replayed successfully, but the refusal also proves nothing about an earlier attempt
   * under the key (#729). It always accompanies `rejected`, and only changes what the page
   * may claim: the intent is void, not "no execution confirmed". A `terminal` refusal never
   * sets it, because there the protocol answered after checking the original key.
   */
  readonly unreplayable?: boolean;
  readonly assetRecoveryId?: string | undefined;
  readonly notice: string;
  readonly asset?: FileContracts["Asset"];
  readonly job?: FileContracts["Job"];
  readonly cancellation?: FileContracts["JobCancellation"];
  readonly recovery?: FileContracts["ErrorResponse"] | undefined;
  readonly operation?: FileContracts["Operation"] | undefined;
  readonly cursor?: string | undefined;
  readonly stream: "idle" | "connecting" | "ended" | "interrupted" | "history-expired";
}

/**
 * The one reconciling action an unconfirmed restored submission can actually take. An
 * identified original execution is *queried* -- its key cannot be replayed, see
 * `canReplay`; a pending cancel names a readable Job and keeps both paths; a create crash
 * window carries no readable identity at all, so only its original key is left. The copy
 * must name exactly that action and never offer one the page disables (#771 review M1/S1).
 */
function resumedNoticeFor(record: TaskRecord): string {
  if (record.operationId !== null)
    return "已找到原执行身份；该请求结果尚未确认，请读取原执行核对，不要新建任务。";
  if (record.jobId !== null)
    return "已找到原任务身份；该请求结果尚未确认，请读取原任务核对，或按原输入与原键重放。";
  return "已找到原任务身份；该请求结果尚未确认，请按原输入与原键重放，不要新建任务。";
}

/** A single file intent; explicit replay retains its original keys and payload. */
export class FileTaskSession {
  readonly #client: SpeechFileClient;
  readonly #lifetime = new AbortController();
  readonly #listeners = new Set<() => void>();
  readonly #uploadKey: string;
  readonly #createKey: string;
  readonly #cancelKey: string;
  readonly #journal: FileTaskJournal | undefined;
  #record: TaskRecord | null;
  #upload: UploadRecord | null;
  #assetHintId: string | null = null;
  #uploadHasBeenAttempted = false;
  /**
   * Whether a create request has already been issued for this durable intent. A 413 may void
   * the intent only when this attempt is the first *sent* one: an earlier request under the
   * same key may already have become a Job, and the receive layer answers 413 before it looks
   * the key up, so it cannot rule that out (#729, #707 review M1).
   *
   * The fact is "a request was sent", not "a record exists": `#submit` assigns `#record` in
   * memory before it persists, so a failed journal write would otherwise read as a second
   * attempt and pin a page whose only create was the one that got the 413 (#729 review M1).
   * It flips only right before the request is actually sent. A restored create record starts
   * as issued because the record is written immediately before the request, so a crash in
   * between is indistinguishable from a sent one -- the safe direction, since treating it as
   * first could void a possibly executed submission. A restored upload record never carried
   * a create request, so it starts unissued.
   */
  #createEverIssued = false;
  /** The entry snapshot of `!#createEverIssued` for the request in flight; see `#submit`. */
  #createAttemptIsFirst = false;
  #view: TaskView = {
    busy: false,
    inputFixed: false,
    unknown: false,
    canReplay: false,
    requiresInput: false,
    notice: "选择输入开始任务。",
    stream: "idle",
  };
  #watch: AbortController | undefined;
  #file: File | undefined;
  #text: string | undefined;
  #assetIdentity:
    Readonly<Pick<FileContracts["Asset"], (typeof assetIdentityFields)[number]>> | undefined;
  #tts: Readonly<TtsSelection> | undefined;
  #model: string | undefined;
  #replay: (() => Promise<void>) | undefined;

  constructor(client: SpeechFileClient, journal?: FileTaskJournal) {
    this.#client = client;
    this.#journal = journal;
    const saved = journal?.load() ?? null;
    // A restored upload may have reached an earlier writer; a later ingress rejection is no proof.
    this.#uploadHasBeenAttempted = saved !== null;
    this.#view = { ...this.#view, inputFixed: saved !== null };
    this.#record = saved?.version === 1 || saved?.version === 2 ? saved : null;
    this.#createEverIssued = this.#record !== null;
    this.#upload = isUploadRecord(saved) ? saved : null;
    this.#assetHintId = uploadHint(this.#upload);
    this.#uploadKey = this.#upload?.uploadKey ?? crypto.randomUUID();
    this.#createKey = saved?.createKey ?? crypto.randomUUID();
    this.#cancelKey = saved?.cancelKey ?? crypto.randomUUID();
    if (this.#upload) {
      const upload = this.#upload;
      this.#model = upload.model;
      if (upload.version === 4 || upload.version === 6)
        this.#tts = Object.freeze({
          model: upload.model,
          voiceId: upload.voiceId,
          voiceRevision: upload.voiceRevision,
          profileId: upload.profileId,
          profileRevision: upload.profileRevision,
        });
      if (upload.pending === "validation")
        this.#assetIdentity = Object.freeze({
          id: upload.assetId!,
          workspace_id: upload.workspaceId!,
          purpose: upload.version === 3 || upload.version === 5 ? "asr_input" : "text_input",
          media_type: upload.assetMediaType!,
          bytes: upload.assetBytes!,
          sha256: upload.assetSha256!,
        });
      this.#view = {
        ...this.#view,
        unknown: upload.pending === "upload",
        requiresInput: upload.pending === "upload" && this.#assetHintId === null,
        assetRecoveryId: this.#assetHintId ?? undefined,
        notice:
          this.#assetHintId && upload.pending === "upload"
            ? "已保留原素材标识，请显式读取；不会重传或创建替代素材。"
            : upload.pending === "upload"
              ? "原上传尚未确认。请重新提供同一输入，按原键恢复。"
              : "已找到原素材身份。请恢复读取后继续原任务。",
      };
    }
    if (this.#record) {
      const record = this.#record;
      this.#model = record.model;
      // The submit body is rebuilt on demand: v2 text lives only in the uploaded asset,
      // never in this record, so replay reads it back instead of trusting local storage.
      const settled = record.settled;
      // A settled cancel intent names its Job and stays readable, so it says which
      // class was refused; only the refusal itself is final (#723).
      const settledCancel = settled === "terminal" && record.pending === "cancel";
      // `settled` is written only after an observed refusal, so it alone decides whether
      // the page may claim one. The four restored fields derive from it as one table
      // (file-task-persistence.test.ts pins every row): `unknown` and `rejected` are
      // mutually exclusive by construction, `canReplay` implies `unknown`, and
      // `unreplayable` implies `rejected`. The crash window carries no `settled` and
      // describes an unconfirmed submission, so it must never be worded as a refusal
      // (#771).
      this.#view = {
        ...this.#view,
        // `pending` alone cannot tell a submission still awaiting its answer from one the
        // protocol already refused; only an observed refusal persists `settled`, so a
        // reload keeps that distinction instead of rebuilding every pending create as
        // unknown (#707) and every pending cancel as "outcome unknown" (#723). The crash
        // window never carries the field and stays unknown.
        unknown: record.pending !== null && settled === undefined,
        canReplay: record.pending !== null && record.operationId === null && settled === undefined,
        cursor: record.cursor ?? undefined,
        ...(settled === undefined ? {} : { rejected: true }),
        // The two settled values differ only in what the page may claim, never in what the
        // user can do: both are abandoned explicitly and both are unreplayable.
        ...(settled === "unreplayable" ? { unreplayable: true } : {}),
        notice:
          settled === "unreplayable"
            ? "已找到原任务身份；该请求因请求体超过本部署的接收上限被拒绝，原意图作废。"
            : settledCancel
              ? "已找到原任务身份；该取消请求已被协议确定性拒绝并作废。请读取原任务核对，再显式收尾。"
              : settled === "terminal"
                ? "已找到原任务身份；该意图已被协议确定性拒绝并作废。"
                : record.pending !== null
                  ? resumedNoticeFor(record)
                  : "已找到原任务身份；请读取原任务状态。",
      };
      if (this.#view.canReplay)
        this.#replay =
          record.pending === "cancel" && record.jobId
            ? () => this.#cancelIntent(record.jobId!)
            : () => this.#submit();
    }
  }
  getSnapshot = () => this.#view;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  #update(update: Partial<TaskView>) {
    if (this.#lifetime.signal.aborted) return;
    this.#view = { ...this.#view, ...update };
    this.#listeners.forEach((listener) => listener());
  }

  #persist(
    update: Partial<
      Pick<TaskRecord, "jobId" | "pending" | "operationId" | "cursor" | "settled">
    > & {
      effectiveModelKey?: string;
    },
  ) {
    this.#lifetime.signal.throwIfAborted();
    if (!this.#record) return;
    const record = { ...this.#record, ...update };
    this.#journal?.save(record);
    this.#record = record;
    this.#upload = null;
  }

  #checkJob(job: FileContracts["Job"]) {
    if (
      this.#record &&
      (job.workspace_id !== this.#record.workspaceId ||
        job.kind !== (this.#record.version === 1 ? "asr" : "tts") ||
        (this.#record.jobId !== null && job.id !== this.#record.jobId))
    )
      throw new Error("Speech resource mismatch");
    if (this.#record?.version !== 2) return;
    const record = this.#record;
    const effective = job.effective_configuration;
    const model = effective.models.tts;
    if (
      effective.voice?.id !== record.voiceId ||
      effective.voice.revision !== record.voiceRevision ||
      effective.profile?.id !== record.profileId ||
      effective.profile.revision !== record.profileRevision ||
      !model
    )
      throw new Error("Speech resource mismatch");
    // The request model can be an alias. Pin its resolved identity and revision,
    // validated by the generated Job parser, rather than comparing it to the alias.
    const key = JSON.stringify([model.id, model.revision]);
    if (record.effectiveModelKey !== undefined && record.effectiveModelKey !== key)
      throw new Error("Speech resource mismatch");
    return key;
  }

  #acceptJob(
    job: FileContracts["Job"],
    update: Partial<Pick<TaskRecord, "jobId" | "pending" | "operationId" | "cursor">> = {},
  ) {
    const effectiveModelKey = this.#checkJob(job);
    // Persist confirmation before exposing it; a failed write leaves the original
    // request pending. Legacy v2 records learn a pin only from a valid full Job.
    const newPin =
      effectiveModelKey !== undefined &&
      this.#record?.version === 2 &&
      this.#record.effectiveModelKey === undefined;
    if (newPin || Object.keys(update).length > 0)
      this.#persist({ ...update, ...(newPin ? { effectiveModelKey } : {}) });
  }

  #checkAsset(asset: FileContracts["Asset"]) {
    if (
      !this.#assetIdentity ||
      assetIdentityFields.some((field) => asset[field] !== this.#assetIdentity![field])
    )
      throw new Error("Speech resource mismatch");
  }

  /** Read existing identities only. Never create or cancel during restoration. */
  async restore() {
    if (this.#record || this.#upload) await this.refresh();
  }

  async #run<T>(action: () => Promise<T>, mutation = false): Promise<T | undefined> {
    if (this.#view.busy || this.#lifetime.signal.aborted) return;
    this.stopEvents();
    this.#update({ busy: true });
    try {
      const value = await action();
      if (mutation) {
        this.#replay = undefined;
        this.#update({ unknown: false, canReplay: false, recovery: undefined });
      }
      return value;
    } catch (error) {
      if (this.#lifetime.signal.aborted) return;
      // Only a mutation rejection can spend a submission; a failed read changes no intent.
      const contractError = mutation && error instanceof SpeechResponseError ? error : undefined;
      // The durable record names the submission class in flight: `#cancelIntent` persists
      // `pending: "cancel"` before its request and `#submit` persists `pending: "create"`
      // before its own, while an upload-only attempt has no record yet. Some codes settle
      // only one class, so the classifier needs this context.
      const operation: RejectionOperation | undefined = mutation
        ? this.#record?.pending === "cancel"
          ? "cancel"
          : this.#upload
            ? "upload"
            : "create"
        : undefined;
      const rejection =
        contractError && operation
          ? rejectionDisposition(contractError.status, contractError.code, operation)
          : undefined;
      if (error instanceof UploadInputMismatchError) {
        this.#update({ notice: "输入与原素材不一致，请重新提供原文件或原文本。" });
      } else if (mutation && error instanceof SpeechUploadRecoveryError) {
        this.#replay = undefined;
        if (error.assetId) {
          try {
            this.#rememberAssetHint(error.assetId);
          } catch {
            this.#update({
              unknown: true,
              canReplay: false,
              notice: "原素材恢复标识未能保存。请保留原请求，修复存储后按原输入和原键恢复。",
            });
            return;
          }
        } else
          this.#replay = async () => {
            await action();
          };
        this.#update({
          unknown: true,
          canReplay: !error.assetId,
          ...(error.assetId ? { requiresInput: false, assetRecoveryId: error.assetId } : {}),
          recovery: undefined,
          operation: undefined,
          notice: error.assetId
            ? "原素材上传未决，已保留原标识；请读取素材状态，不要创建替代上传。"
            : "原上传结果未知，尚无可用素材标识；仅按原输入和原幂等键恢复。",
        });
      } else if (
        mutation &&
        error instanceof SpeechResponseError &&
        ["operation_in_progress", "response_not_replayable"].includes(error.code)
      ) {
        this.#replay = undefined;
        try {
          this.#persist({ operationId: error.detail.error.nexus?.operation_id ?? null });
        } catch {
          /* Pending original intent remains durable; no replacement is submitted. */
        }
        this.#update({
          unknown: true,
          canReplay: false,
          recovery: error.detail,
          operation: undefined,
          notice: "原执行已有恢复身份。请重新读取原执行状态；不要创建替代执行。",
        });
      } else if (mutation && error instanceof OriginalTextUnavailableError) {
        // Nothing was submitted, so the original request is intact: keep it replayable
        // instead of reporting an unknown outcome or creating a substitute task.
        this.#replay = async () => {
          await action();
        };
        this.#update({
          unknown: true,
          canReplay: true,
          notice: "原文本素材暂不可读取。保留原请求身份；请按原键重试，不要创建替代任务。",
        });
      } else if (mutation && !(error instanceof SpeechResponseError)) {
        this.#replay = async () => {
          await action();
        };
        this.#update({
          unknown: true,
          canReplay: true,
          notice:
            error instanceof TaskJournalError
              ? "恢复记录未能确认。保留原请求身份，请恢复读取或按原键重试。"
              : "操作结果未知。请读取原任务，或按原幂等键重放；不要创建替代任务。",
        });
      } else if (mutation && this.#upload && this.#view.unknown && !this.#view.asset) {
        // An unconfirmed original upload outranks every rejection class below: no class
        // may clear it or pretend this attempt proved anything about it.
        this.#replay = undefined;
        this.#update({
          canReplay: false,
          notice: contractError
            ? `当前请求被拒绝（${contractError.status}: ${contractError.code}），原上传仍未确认；保留原身份。`
            : "原上传仍未确认；请保留原身份。",
        });
      } else if (contractError && rejection === "unknown-outcome") {
        // The original submission may already be executing; the contract requires keeping
        // its key and request, then replaying or querying that Job, never a new intent
        // (contracts/speech-openapi.yaml:840-842). An identified original execution is
        // queried instead of replayed.
        const recovery = retainRecovery(this.#view.recovery, contractError.detail);
        const operationId = recovery.error.nexus?.operation_id;
        // Persist only the identity this branch actually keeps, so the durable record and
        // the visible recovery never point at two different operations.
        if (operationId && recovery === contractError.detail) {
          try {
            this.#persist({ operationId });
          } catch {
            /* Pending original intent remains durable; no replacement is submitted. */
          }
        }
        this.#replay = operationId
          ? undefined
          : async () => {
              await action();
            };
        this.#update({
          unknown: true,
          canReplay: !operationId,
          recovery,
          notice: operationId
            ? "提交结果未知，但已取得原执行身份。请读取原执行；不要新建任务。"
            : "提交结果未知，原提交可能已执行。请读取原执行，或按原幂等键重放；不要新建任务。",
        });
      } else if (contractError && rejection === "intent-kept") {
        // This attempt was refused before acting on the intent, yet the same intent may
        // still succeed. Keep it replayable and leave any earlier unknown-outcome evidence
        // untouched, because clearing it would hide the original pending submission. The
        // same key cannot start a second execution, so replay is the safe reconciliation.
        this.#replay = async () => {
          await action();
        };
        this.#update({
          canReplay: true,
          notice: `提交被暂时拒绝（${contractError.status} ${contractError.code}）；原意图已保留，按原键重试不会产生第二次执行，不要新建任务。`,
        });
      } else if (contractError && rejection === "unreplayable") {
        // The refusal is about the bytes of this request (#729). Replaying it can only
        // produce the same refusal, so the durable intent is spent *for replay* — but the
        // answer was produced before the protocol looked up the original key, so it cannot
        // be presented as proof that nothing executed. Both facts must survive reload,
        // which is why the verdict is persisted as `unreplayable` and not as `terminal`.
        // An identified execution would be the only reconciliation left, so it is never
        // hidden behind this verdict: any known identity keeps the unknown state instead
        // (#707 M2). This does not submit a replacement; the user abandons explicitly in
        // the UI and the Host removes this record, so the next intent gets a fresh key.
        const identified =
          Boolean(this.#view.job) ||
          Boolean(this.#record?.jobId) ||
          Boolean(this.#record?.operationId) ||
          Boolean(contractError.detail.error.nexus?.operation_id);
        if (!this.#createAttemptIsFirst) {
          // This intent already sent an earlier create request, so a Job may exist under the
          // original key and the 413 -- answered before the key lookup -- says nothing about
          // it. Voiding the intent here would hand the user an explicit "abandon and start
          // over" over a possibly executed submission, which is exactly the duplicate this
          // card must not create (#707 review M1). Keep the unknown state, its identity and
          // the replay handle: the original key remains the only reconciliation. The state is
          // unconditional because the durable shape (`pending: "create"`, no Job, no verdict)
          // is identical for a lost answer, so a narrower page state would contradict the
          // one a reload rebuilds.
          this.#replay = async () => {
            await action();
          };
          this.#update({
            unknown: true,
            canReplay: !identified,
            recovery: retainRecovery(this.#view.recovery, contractError.detail),
            notice: identified
              ? `提交被拒绝（${contractError.status} ${contractError.code}），且已有可查询的执行身份；请读取原执行核对成功、失败或不确定，不要新建任务。`
              : `提交被拒绝（${contractError.status} ${contractError.code}）：请求体超过本部署的接收上限，但该意图此前已有提交尝试，这次拒绝不能证明原提交未执行；保留原键与未知状态，不要新建任务。`,
          });
        } else if (identified) {
          // Query, do not replay: the identity is the only reconciliation, and re-sending
          // the same bytes would be refused again before it could ever reach that Job.
          this.#replay = undefined;
          this.#update({
            unknown: true,
            canReplay: false,
            recovery: retainRecovery(this.#view.recovery, contractError.detail),
            notice: `提交被拒绝（${contractError.status} ${contractError.code}），且已有可查询的执行身份；请读取原执行核对成功、失败或不确定，不要新建任务。`,
          });
        } else {
          this.#replay = undefined;
          try {
            // A reload must keep the verdict (the original key can never be replayed), so
            // it is recorded before it is shown. Only an *observed* refusal reaches this
            // point: a lost answer persists no `settled` field and reloads as unknown.
            this.#persist({ settled: "unreplayable" });
          } catch {
            /* The protocol's answer still stands in this page; a reload degrades to unknown. */
          }
          this.#update({
            unknown: false,
            canReplay: false,
            recovery: undefined,
            rejected: true,
            unreplayable: true,
            notice: `提交被拒绝（${contractError.status} ${contractError.code}）：请求体超过本部署的接收上限，这次请求被拒绝、原意图作废；同一请求不能按原键重放，请结束本页并用新的幂等键新建。`,
          });
        }
      } else if (contractError && rejection === "terminal") {
        // This branch settles *this attempt*: the protocol does not accept the same
        // request again (e.g. 422 `job_text_asset_unsupported`), so a fresh intent with its
        // own key is the only way forward. It does not claim the key has no Job row: 409
        // `idempotency_conflict` identifies *another* intent's Job under this key
        // (contracts/speech-openapi.yaml:803-805), which still spends this one, but the
        // replacement must not reuse the key. 409 `execution_cleanup_pending` is classified
        // as an unknown outcome for a create attempt before reaching here, precisely
        // because it proves a Job exists. Nothing here submits a replacement; the user
        // abandons the spent intent explicitly in the UI.
        //
        // Settling a durable intent is stricter than settling this attempt: the
        // conclusion is persisted and a reload will present it as final, so it must not
        // hide an earlier submission of the same intent whose outcome was never observed
        // (a lost answer keeps the record in the *same* shape as a refusal). Only a
        // refusal the protocol emits after it has checked the original key proves that
        // earlier submission never took effect. A refusal that may precede that check
        // (401/403/404/405 auth and gateway answers, body-level 400) cannot speak for
        // the earlier attempt, so it stays unknown and the original key remains the only
        // reconciliation. This is the temporal gap that makes a state-only predicate
        // unsafe (#707 review M1). The cancel path keeps the same rule where it applies,
        // but its record also names the Job, so a refusal that is an answer about that
        // Job (`settlesCancelAttempt`) spends only the cancellation request and leaves the
        // identity readable: no reply can bring the Job back or accept its cancellation.
        // A body-level 413 never reaches this branch: it is the `unreplayable` class
        // above, where the original key is dead as well, so neither replay nor unknown is
        // a truthful answer (#729).
        // An identified original execution stays queryable, so it is never settled: that
        // would hide the only unresolved submission the user could reconcile.
        const envelopeOperationId = contractError.detail.error.nexus?.operation_id;
        const settlesCreate =
          operation === "create" &&
          !this.#view.job &&
          !this.#record?.jobId &&
          !this.#record?.operationId &&
          !envelopeOperationId &&
          settlesReplayedIntent(contractError);
        // A cancel attempt already names the Job it was refused against, so the
        // create-path gates (`jobId`/`view.job` must be empty) cannot apply here. What
        // makes the persisted verdict safe is the predicate above, not the record shape:
        // the refused attempt is spent, while the Job it named stays readable.
        const settlesCancel =
          operation === "cancel" &&
          this.#record?.pending === "cancel" &&
          this.#record.jobId !== null &&
          !this.#record.operationId &&
          !envelopeOperationId &&
          settlesCancelAttempt(contractError);
        const settles = settlesCreate || settlesCancel;
        const refusalProvesNothing =
          (operation === "create" && !settlesCreate) || (operation === "cancel" && !settlesCancel);
        if (refusalProvesNothing) {
          // Keep the unknown state, its recovery envelope and the replay handle: this
          // refusal rules out this *answer*, not an earlier submission, so the original
          // key stays the only reconciliation.
          this.#replay = async () => {
            await action();
          };
          this.#update({
            unknown: true,
            canReplay: true,
            recovery: retainRecovery(this.#view.recovery, contractError.detail),
            notice:
              operation === "cancel"
                ? `取消请求被拒绝（${contractError.status} ${contractError.code}），但该拒绝不能证明原取消未生效；保留原键与未知状态，请读取原任务。`
                : `提交被拒绝（${contractError.status} ${contractError.code}），但该拒绝不能证明原提交未执行；保留原键与未知状态，不要新建任务。`,
          });
        } else {
          this.#replay = undefined;
          if (settles) {
            try {
              // Record the observed refusal so a reload keeps it settled instead of
              // rebuilding an unconfirmed submission. Only a *refused* attempt reaches
              // this point: the crash window between sending and any answer persists
              // `pending: "create"`/`pending: "cancel"` without this field and reloads as
              // unknown.
              this.#persist({ settled: "terminal" });
            } catch {
              /* The protocol's answer still stands in this page; a reload degrades to unknown. */
            }
          }
          this.#update({
            unknown: false,
            canReplay: false,
            recovery: undefined,
            ...(settles ? { rejected: true } : {}),
            notice: contractError.message,
          });
        }
      } else {
        this.#update({
          notice: error instanceof SpeechResponseError ? error.message : "读取失败，请重新读取。",
        });
      }
    } finally {
      this.#update({ busy: false });
    }
  }

  async #prepareUpload() {
    if (!this.#journal) return;
    if (this.#upload) {
      this.#lifetime.signal.throwIfAborted();
      this.#journal.save(this.#upload);
      return;
    }
    const file = this.#tts && this.#text !== undefined ? textUploadFile(this.#text) : this.#file;
    if (!file) throw new Error("Missing upload input");
    const fingerprint = await inputFingerprint(file, this.#lifetime.signal);
    const identity = {
      model: this.#model!,
      assetId: null,
      workspaceId: null,
      createKey: this.#createKey,
      cancelKey: this.#cancelKey,
      jobId: null,
      pending: "upload" as const,
      operationId: null,
      cursor: null,
      uploadKey: this.#uploadKey,
      inputFingerprint: fingerprint,
      assetMediaType: null,
      assetBytes: null,
      assetSha256: null,
    };
    const upload: UploadRecord = this.#tts
      ? { ...identity, ...this.#tts, version: 4 }
      : { ...identity, version: 3 };
    this.#lifetime.signal.throwIfAborted();
    this.#journal.save(upload);
    this.#upload = upload;
  }

  #rememberAssetHint(id: string) {
    this.#lifetime.signal.throwIfAborted();
    if (this.#assetHintId && this.#assetHintId !== id) throw new Error("Asset identity changed");
    if (this.#upload) {
      const record: UploadRecord =
        this.#upload.version === 3 || this.#upload.version === 5
          ? { ...this.#upload, version: 5, assetHintId: id }
          : { ...this.#upload, version: 6, assetHintId: id };
      this.#journal!.save(record);
      this.#upload = record;
    }
    this.#assetHintId = id;
  }

  #acceptAsset(asset: FileContracts["Asset"]) {
    if (
      asset.purpose !== (this.#tts ? "text_input" : "asr_input") ||
      (this.#tts && asset.media_type !== "text/plain") ||
      (this.#assetHintId !== null && asset.id !== this.#assetHintId)
    )
      throw new Error("Speech resource mismatch");
    this.#acknowledgeUpload(asset);
    this.#assetIdentity = Object.freeze({ ...asset });
    this.#update({
      asset,
      unknown: false,
      canReplay: false,
      requiresInput: false,
      uploadRejected: false,
      assetRecoveryId: undefined,
    });
    this.#replay = undefined;
    this.#file = undefined;
  }

  #acknowledgeUpload(asset: FileContracts["Asset"]) {
    if (!this.#upload) return;
    const upload = {
      ...this.#upload,
      pending: "validation" as const,
      assetId: asset.id,
      workspaceId: asset.workspace_id,
      assetMediaType: asset.media_type,
      assetBytes: asset.bytes,
      assetSha256: asset.sha256,
    };
    this.#lifetime.signal.throwIfAborted();
    this.#journal!.save(upload);
    this.#upload = upload;
  }

  async #uploadInput() {
    const first = !this.#uploadHasBeenAttempted;
    this.#uploadHasBeenAttempted = true;
    try {
      return this.#tts && this.#text !== undefined
        ? await this.#client.uploadText(this.#text, this.#uploadKey, this.#lifetime.signal)
        : this.#file
          ? await this.#client.upload(this.#file, this.#uploadKey, this.#lifetime.signal)
          : undefined;
    } catch (error) {
      // Only these published first-attempt rejections establish no accepted audio upload.
      // Never use a later 413 to discard an earlier unknown request, hint, or restored intent.
      if (
        first &&
        error instanceof SpeechResponseError &&
        error.detail.error.nexus?.operation_id === undefined &&
        ((error.status === 413 && error.code === "asset_too_large") ||
          (error.status === 422 && error.code === "asset_empty"))
      )
        this.#update({ uploadRejected: true, unknown: false });
      throw error;
    }
  }

  /**
   * The one submit body for a durable record. v2 text is supplied only here: in-memory for
   * a fresh intent, otherwise read back from its immutable uploaded asset, never from the
   * recovery journal.
   */
  async #submitCommand(record: TaskRecord): Promise<FileContracts["JobCreateRequest"]> {
    if (record.version === 1) return jobCommand(record);
    let text = this.#text;
    if (text === undefined) {
      try {
        text = await this.#client.assetText(record.assetId, this.#lifetime.signal);
      } catch {
        // Aborted calls leave view updates to #run; a real read failure must not become a
        // substitute intent, so it keeps the original request identity replayable.
        throw new OriginalTextUnavailableError();
      }
    }
    return jobCommand(record, text);
  }

  async #submit() {
    const signal = this.#lifetime.signal;
    // A refused intent is spent: resubmitting it would just repeat the same refusal, and
    // the UI only offers explicit abandonment (#707). Both settled values are spent; the
    // `unreplayable` one adds that the refusal was about the request bytes (#729).
    if (this.#view.rejected || this.#record?.settled !== undefined) return;
    // Read before the record below is created and before this request is sent: see
    // `#createEverIssued` for why "already sent" -- not "record exists" -- is the proof that
    // no earlier create request exists for this intent.
    const firstCreateAttempt = !this.#createEverIssued;
    if (!this.#record) {
      if (!this.#view.asset) {
        if (this.#assetHintId) throw new Error("Read the original Asset first");
        await this.#prepareUpload();
        const asset = await this.#uploadInput();
        if (!asset) throw new Error("Missing file intent");
        this.#acceptAsset(asset);
      }
      const asset = this.#view.asset;
      if (asset) this.#checkAsset(asset);
      if (asset?.status !== "ready") {
        this.#update({ notice: "素材尚未就绪。请重新读取校验状态。" });
        return;
      }
      const identity = {
        model: this.#model!,
        assetId: asset.id,
        workspaceId: asset.workspace_id,
        createKey: this.#createKey,
        cancelKey: this.#cancelKey,
        jobId: null,
        pending: "create" as const,
        operationId: null,
        cursor: null,
      };
      this.#record = this.#upload
        ? uploadJobRecord(this.#upload)
        : this.#tts
          ? { ...identity, ...this.#tts, version: 2 }
          : { ...identity, version: 1 };
    }
    // A replay reuses the original keys and the body built from the durable record; the body
    // itself is never parked on the instance, so the text does not outlive its submission.
    const command: FileContracts["JobCreateRequest"] = await this.#submitCommand(this.#record);
    this.#text = undefined;
    this.#persist({ pending: "create" });
    // The journal write above succeeded, so the request really is going out from here on:
    // record that (and keep the entry snapshot for this attempt's error branch).
    this.#createAttemptIsFirst = firstCreateAttempt;
    this.#createEverIssued = true;
    const job = await this.#client.create(command, this.#createKey, signal);
    this.#acceptJob(job, { jobId: job.id, pending: null, operationId: null });
    this.#update({ job, notice: "任务已接收。读取当前状态或连接任务事件。" });
  }

  async start(file: File, model: string) {
    if (this.#model !== undefined || this.#lifetime.signal.aborted || !model.trim()) return;
    this.#file = file;
    this.#model = model;
    this.#update({ inputFixed: true });
    await this.#run(() => this.#submit(), true);
  }

  async startText(text: string, selection: TtsSelection) {
    if (this.#model !== undefined || this.#lifetime.signal.aborted) return;
    let command: FileContracts["JobCreateRequest"];
    try {
      // Validate inline input before any upload; the durable Job references the resulting asset.
      command = parse("JobCreateRequest", {
        kind: "tts",
        model: selection.model,
        input: { text },
        voice: { id: selection.voiceId, revision: selection.voiceRevision },
        profile: { id: selection.profileId, revision: selection.profileRevision },
        output: { audio_format: "wav", save_artifacts: true },
      });
      if (new TextDecoder().decode(new TextEncoder().encode(text)) !== text)
        throw new Error("Text cannot roundtrip UTF-8");
    } catch {
      this.#update({ notice: "配音参数无效，请检查文本和固定版本。" });
      return;
    }
    if (command.kind !== "tts") return;
    this.#tts = Object.freeze({
      model: selection.model,
      voiceId: command.voice.id,
      voiceRevision: command.voice.revision,
      profileId: selection.profileId,
      profileRevision: selection.profileRevision,
    });
    this.#model = selection.model;
    this.#text = text;
    this.#update({ inputFixed: true });
    await this.#run(() => this.#submit(), true);
  }

  async replay() {
    if (this.#replay) await this.#run(this.#replay, true);
  }

  async #resumeInput(file: () => File, text?: string) {
    const upload = this.#upload;
    if (!upload || upload.pending !== "upload" || this.#assetHintId !== null) return;
    await this.#run(async () => {
      const input = file();
      if ((await inputFingerprint(input, this.#lifetime.signal)) !== upload.inputFingerprint)
        throw new UploadInputMismatchError();
      if (upload.version === 3 || upload.version === 5) this.#file = input;
      else this.#text = text!;
      this.#update({ requiresInput: false });
      await this.#submit();
    }, true);
  }

  async resumeFile(file: File) {
    if (this.#upload?.version === 3 || this.#upload?.version === 5)
      await this.#resumeInput(() => file);
  }

  async resumeText(text: string) {
    if (this.#upload?.version === 4 || this.#upload?.version === 6)
      await this.#resumeInput(() => textUploadFile(text), text);
  }

  async refresh() {
    await this.#run(async () => {
      const operationId =
        this.#view.recovery?.error.nexus?.operation_id ?? this.#record?.operationId;
      if (operationId) {
        const operation = await this.#client.getOperation(operationId, this.#lifetime.signal);
        this.#update({ operation, notice: "已读取原执行；不会创建替代执行。" });
        if (operation.job_id) {
          const job = await this.#client.getJob(operation.job_id, this.#lifetime.signal);
          if (this.#record?.pending === "cancel") {
            // Finding the original job does not acknowledge its cancellation request.
            this.#acceptJob(job);
            this.#update({
              job,
              unknown: true,
              canReplay: false,
              notice: "已读取原任务；取消请求仍未确认，保留原恢复身份。",
            });
            return;
          }
          this.#acceptJob(job, { jobId: job.id, pending: null, operationId: null });
          this.#replay = undefined;
          this.#update({ job, unknown: false, canReplay: false, recovery: undefined });
        }
      } else if (this.#view.job || this.#record?.jobId) {
        const job = await this.#client.getJob(
          this.#view.job?.id ?? this.#record!.jobId!,
          this.#lifetime.signal,
        );
        this.#acceptJob(job);
        this.#update({
          job,
          notice: "已重新读取当前任务。",
        });
      } else if (this.#view.asset || this.#upload?.assetId || this.#assetHintId) {
        const asset = await this.#client.getAsset(
          this.#view.asset?.id ?? this.#upload?.assetId ?? this.#assetHintId!,
          this.#lifetime.signal,
        );
        if (this.#assetIdentity) this.#checkAsset(asset);
        else this.#acceptAsset(asset);
        this.#update({
          asset,
          notice: "已重新读取素材。",
        });
      }
    });
  }

  async continueAfterValidation() {
    if (
      this.#view.asset?.status === "ready" &&
      !this.#view.job &&
      !this.#view.unknown &&
      !this.#view.rejected
    )
      await this.#run(() => this.#submit(), true);
  }

  async cancel() {
    const jobId = this.#view.job?.id;
    // A settled cancel attempt is spent (#723): resending its key could be in flight
    // while the record still says "refused", so a reload could not tell that unknown
    // outcome from the recorded one. The Job stays readable; the page offers explicit
    // finish instead. A settled create attempt owns no Job, so the same guard is inert.
    if (!jobId || this.#view.unknown || this.#view.rejected) return;
    await this.#run(() => this.#cancelIntent(jobId), true);
  }

  async #cancelIntent(jobId: string) {
    this.#persist({ pending: "cancel", operationId: null });
    const cancellation = await this.#client.cancel(jobId, this.#cancelKey, this.#lifetime.signal);
    const job = await this.#client.getJob(jobId, this.#lifetime.signal);
    this.#acceptJob(job, { pending: null, operationId: null });
    this.#update({ cancellation, notice: "已收到取消响应；资源释放以 cleanup_state 为准。" });
    this.#update({ job });
  }

  stopEvents() {
    this.#watch?.abort();
    this.#watch = undefined;
    if (this.#view.stream === "connecting") this.#update({ stream: "idle" });
  }

  async reconnect() {
    const jobId = this.#view.job?.id;
    if (!jobId || this.#view.busy || this.#lifetime.signal.aborted) return;
    this.stopEvents();
    const watch = new AbortController();
    this.#watch = watch;
    this.#update({ stream: "connecting" });
    try {
      for await (const event of this.#client.events(jobId, this.#view.cursor, watch.signal)) {
        if (watch.signal.aborted) return;
        if (this.#view.cursor && BigInt(event.id) <= BigInt(this.#view.cursor)) continue;
        // Events invalidate a snapshot. Never fabricate a complete Job from partial event data.
        const job = await this.#client.getJob(jobId, watch.signal);
        if (!watch.signal.aborted) {
          this.#acceptJob(job, { cursor: event.id });
          this.#update({ job, cursor: event.id });
        }
      }
      if (!watch.signal.aborted)
        this.#update({ stream: "ended", notice: "事件连接已结束，可按水位重连。" });
    } catch (error) {
      if (watch.signal.aborted || this.#lifetime.signal.aborted) return;
      if (
        error instanceof SpeechResponseError &&
        error.status === 410 &&
        error.code === "event_history_expired"
      ) {
        try {
          this.#persist({ cursor: null });
          this.#update({ cursor: undefined });
          const job = await this.#client.getJob(jobId, watch.signal);
          if (!watch.signal.aborted) {
            this.#acceptJob(job);
            this.#update({
              job,
              stream: "history-expired",
              notice: "事件历史已过期，已重新读取当前任务；下次从当前事件流连接。",
            });
          }
        } catch {
          if (!watch.signal.aborted)
            this.#update({
              stream: "interrupted",
              notice: "事件历史已过期，恢复状态未确认，请重试。",
            });
        }
      } else
        this.#update({
          stream: "interrupted",
          notice: "事件连接中断，保留已确认水位；请重新连接。",
        });
    } finally {
      if (this.#watch === watch) this.#watch = undefined;
    }
  }

  async download(artifactId: string) {
    if (!this.#view.job?.artifacts.some((item) => item.artifact_id === artifactId)) return;
    return this.#run(() => this.#client.download(artifactId, this.#lifetime.signal));
  }

  dispose() {
    this.#lifetime.abort();
    this.#watch?.abort();
    this.#listeners.clear();
    this.#file = undefined;
    this.#text = undefined;
    this.#replay = undefined;
  }
}
