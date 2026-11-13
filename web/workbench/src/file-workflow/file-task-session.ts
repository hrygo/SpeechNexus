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
 * What a rejected submission proved. Only `terminal` proves that no Job exists for this
 * key; `unknown-outcome` means the original submission may already execute and
 * `retryable` means this attempt was refused but the same intent may still succeed.
 */
type RejectionDisposition = "terminal" | "unknown-outcome" | "retryable";

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
 *   (backend/src/speechnexus_backend/speech/interfaces/http/job_errors.py:136-140), so
 *   this intent is kept for an explicit same-key retry instead of being spent.
 * - 503 `job_admission_contended`: admission state changed concurrently; retryable by
 *   the same public Job error table.
 * - 503 `asset_inspection_busy`: the only Asset 503 whose "a new request later may
 *   succeed" statement holds (contracts/speech-openapi.yaml:2682-2689).
 */
const rejectionTable = [
  [503, "job_outcome_unknown", "unknown-outcome"],
  [503, "job_unavailable", "retryable"],
  [503, "job_admission_contended", "retryable"],
  [503, "asset_inspection_busy", "retryable"],
  [429, "job_capacity_exhausted", "retryable"],
] as const satisfies readonly (readonly [number, string, RejectionDisposition])[];

function rejectionDisposition(status: number, code: string): RejectionDisposition {
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
  if (status === 429) return "retryable";
  // A 4xx contract refusal is the only class that proves nothing was written for this
  // key, so the intent is spent and a fresh intent is safe.
  if (status >= 400 && status < 500) return "terminal";
  // An unlisted 5xx (or non-contract status) may already have executed; never spend the
  // intent on it.
  return "unknown-outcome";
}

interface TaskView {
  readonly busy: boolean;
  readonly inputFixed: boolean;
  readonly unknown: boolean;
  readonly canReplay: boolean;
  readonly requiresInput: boolean;
  readonly uploadRejected?: boolean;
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
      this.#view = {
        ...this.#view,
        unknown: record.pending !== null,
        canReplay: record.pending !== null && record.operationId === null,
        cursor: record.cursor ?? undefined,
        notice: "已找到原任务身份。请恢复读取；未确认的请求仅按原键重放。",
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
    update: Partial<Pick<TaskRecord, "jobId" | "pending" | "operationId" | "cursor">> & {
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
      const rejection = contractError
        ? rejectionDisposition(contractError.status, contractError.code)
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
        // (contracts/speech-openapi.yaml:840-842). An already identified original
        // execution is queried instead of replayed.
        const operationId = contractError.detail.error.nexus?.operation_id;
        if (operationId) {
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
          // Never drop an identity recovered earlier; a nexus-less refusal adds none.
          recovery: this.#view.recovery ?? contractError.detail,
          notice: operationId
            ? "提交结果未知，但已取得原执行身份。请读取原执行；不要新建任务。"
            : "提交结果未知，原提交可能已执行。请读取原执行，或按原幂等键重放；不要新建任务。",
        });
      } else if (contractError && rejection === "retryable") {
        // This attempt was refused before acting on the intent, yet the same intent may
        // still succeed: keep it replayable and leave any earlier unknown-outcome evidence
        // untouched, because clearing it would hide the original pending submission.
        this.#replay = async () => {
          await action();
        };
        this.#update({
          canReplay: true,
          notice: `提交被暂时拒绝（${contractError.status} ${contractError.code}）；原意图已保留，请稍后按原键重试，不要新建任务。`,
        });
      } else if (contractError && rejection === "terminal") {
        this.#replay = undefined;
        // This branch is reachable only for a 4xx refusal that spends the intent: the
        // protocol declares no Job row for it (e.g. 422 `job_text_asset_unsupported`,
        // 409 `idempotency_conflict`), so a fresh intent is safe. Abandoning that spent
        // record still needs UI work outside this task; nothing here submits a replacement.
        this.#update({
          unknown: false,
          canReplay: false,
          recovery: undefined,
          notice: contractError.message,
        });
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
    if (this.#view.asset?.status === "ready" && !this.#view.job && !this.#view.unknown)
      await this.#run(() => this.#submit(), true);
  }

  async cancel() {
    const jobId = this.#view.job?.id;
    if (!jobId || this.#view.unknown) return;
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
