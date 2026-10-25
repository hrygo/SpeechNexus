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
  #command: FileContracts["JobCreateRequest"] | undefined;
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
      this.#command = jobCommand(record);
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
      } else {
        if (mutation && this.#upload && this.#view.unknown && !this.#view.asset) {
          this.#replay = undefined;
          this.#update({
            canReplay: false,
            notice:
              error instanceof SpeechResponseError
                ? `当前请求被拒绝（${error.status}: ${error.code}），原上传仍未确认；保留原身份。`
                : "原上传仍未确认；请保留原身份。",
          });
          return;
        }
        if (mutation) {
          this.#replay = undefined;
          this.#update({ unknown: false, canReplay: false, recovery: undefined });
        }
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
    this.#text = undefined;
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

  async #submit() {
    const signal = this.#lifetime.signal;
    // Replay the original validated payload, independent of refreshed asset views.
    if (this.#command) {
      this.#persist({ pending: "create" });
      const job = await this.#client.create(this.#command, this.#createKey, signal);
      this.#acceptJob(job, { jobId: job.id, pending: null, operationId: null });
      this.#update({ job, notice: "任务已接收。读取当前状态或连接任务事件。" });
      return;
    }
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
    this.#command = jobCommand(this.#record);
    await this.#submit();
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
