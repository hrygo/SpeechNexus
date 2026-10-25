import {
  parse,
  SpeechResponseError,
  type FileContracts,
  type SpeechFileClient,
} from "../file-workflow/speech-file-client";
import { checkVoiceReference, voiceCommand, type VoiceCommand } from "./voice-command";

export const voiceJournalKey = "speechnexus.workbench.voice-create.v1";
type Storage = Pick<globalThis.Storage, "getItem" | "setItem" | "removeItem">;
type Record = {
  version: 2;
  command: VoiceCommand;
  createKey: string;
  cancelKey: string;
  jobId: string | null;
  operationId: string | null;
  pending: "create" | "cancel" | null;
};
type Job = FileContracts["Job"];
type Snapshot = {
  record: Record | null;
  job: Job | null;
  cancellation: FileContracts["JobCancellation"] | null;
  current: boolean;
  busy: boolean;
  blocked: boolean;
  error: string | null;
};
type Client = Pick<SpeechFileClient, "create" | "getAsset" | "getJob" | "cancel" | "getOperation">;
const terminal = new Set(["succeeded", "failed", "cancelled", "expired"]);
function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function record(value: unknown): Record {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("record");
  const r = value as Record;
  const version = (value as { version?: unknown }).version;
  const keys = [
    "version",
    "command",
    "createKey",
    "cancelKey",
    "jobId",
    "pending",
    ...(version === 2 ? ["operationId"] : []),
  ];
  if (
    Object.keys(r).length !== keys.length ||
    !keys.every((k) => Object.hasOwn(r, k)) ||
    ![1, 2].includes(version as number) ||
    ![null, "create", "cancel"].includes(r.pending)
  )
    throw new Error("record");
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  if (
    typeof r.createKey !== "string" ||
    typeof r.cancelKey !== "string" ||
    !uuid.test(r.createKey) ||
    !uuid.test(r.cancelKey) ||
    r.createKey === r.cancelKey
  )
    throw new Error("record");
  const operationId = version === 1 ? null : r.operationId;
  for (const id of [r.jobId, operationId])
    if (id !== null && (typeof id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(id)))
      throw new Error("record");
  if (operationId !== null && !r.pending) throw new Error("record");
  if ((r.pending === "create") !== (r.jobId === null)) throw new Error("record");
  return { ...r, version: 2, operationId, command: voiceCommand(r.command) };
}
/** No audio or current Job cache is persisted; every POST crosses the same original-intent CAS. */
export class VoiceSession {
  #raw: string | null = null;
  #view: Snapshot = freeze({
    record: null,
    job: null,
    cancellation: null,
    current: false,
    busy: false,
    blocked: false,
    error: null,
  });
  #listeners = new Set<() => void>();
  #epoch = 0;
  #active = true;
  #disposed = false;
  #abort: AbortController | null = null;
  #unsavedUpdate: { value: Record | null } | null = null;
  constructor(
    private readonly client: Client,
    private readonly storage: Storage,
  ) {
    try {
      this.#raw = storage.getItem(voiceJournalKey);
      if (this.#raw !== null) {
        if (this.#raw.length > 128 * 1024) throw new Error("record");
        this.#set({ record: record(JSON.parse(this.#raw) as unknown) });
      }
    } catch {
      this.#set({ blocked: true, error: "recovery_unavailable" });
    }
  }
  snapshot = () => this.#view;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #set(patch: Partial<Snapshot>) {
    this.#view = freeze(structuredClone({ ...this.#view, ...patch }));
    this.#listeners.forEach((f) => f());
  }
  #save(value: Record | null) {
    try {
      if (this.storage.getItem(voiceJournalKey) !== this.#raw) throw new Error("cas");
      const next = value ? JSON.stringify(record(value)) : null;
      if (next !== null) this.storage.setItem(voiceJournalKey, next);
      else this.storage.removeItem(voiceJournalKey);
      if (this.storage.getItem(voiceJournalKey) !== next) throw new Error("cas");
      this.#raw = next;
      this.#set({ record: value, blocked: false });
    } catch {
      this.#set({ blocked: true, current: false, error: "storage_unavailable" });
      throw new Error("storage");
    }
  }
  retryStorage() {
    if (this.#disposed || this.#view.busy || this.#view.error === "recovery_unavailable") return;
    try {
      this.#save(this.#unsavedUpdate ? this.#unsavedUpdate.value : this.#view.record);
      this.#unsavedUpdate = null;
      this.#set({ error: null });
    } catch {
      /* preserve original intent */
    }
  }
  #start() {
    if (!this.#active || this.#disposed || this.#view.busy || this.#view.blocked) return null;
    const abort = new AbortController();
    this.#abort = abort;
    const epoch = ++this.#epoch;
    this.#set({ busy: true, current: false, error: null });
    return { epoch, signal: AbortSignal.any([abort.signal, AbortSignal.timeout(15000)]) };
  }
  #live(epoch: number) {
    return !this.#disposed && this.#active && epoch === this.#epoch;
  }
  #job(value: unknown, r: Record) {
    const job = parse("Job", value);
    if (
      job.kind !== r.command.kind ||
      job.workspace_id !== r.command.workspace_id ||
      (r.jobId !== null && job.id !== r.jobId)
    )
      throw new Error("voice_job_identity");
    return job;
  }
  async create(value: unknown) {
    if (this.#view.record) return;
    const op = this.#start();
    if (!op) return;
    try {
      const command = voiceCommand(value);
      if (command.kind === "voice_clone")
        checkVoiceReference(
          command,
          await this.client.getAsset(command.input.reference_audio_asset_id, op.signal),
        );
      if (!this.#live(op.epoch)) return;
      this.#save({
        version: 2,
        command,
        createKey: crypto.randomUUID(),
        cancelKey: crypto.randomUUID(),
        jobId: null,
        operationId: null,
        pending: "create",
      });
      await this.#dispatch(op);
    } catch (error) {
      this.#mutationError(error, op.epoch, true);
    } finally {
      if (this.#live(op.epoch)) this.#set({ busy: false });
    }
  }
  #mutationError(error: unknown, epoch: number, firstCreate = false) {
    if (!this.#live(epoch) || this.#view.blocked) return;
    const r = this.#view.record;
    try {
      if (r && error instanceof SpeechResponseError) {
        const detail = parse("ErrorResponse", error.detail).error;
        if (detail.nexus) {
          if (r.operationId && r.operationId !== detail.nexus.operation_id)
            throw new Error("identity");
          this.#unsavedUpdate = { value: { ...r, operationId: detail.nexus.operation_id } };
          this.#save(this.#unsavedUpdate.value);
          this.#unsavedUpdate = null;
          this.#set({ error: "operation_recovery" });
          return;
        }
        // Only this invocation's first POST can prove rejection. Replay/restoration
        // and failed receipt storage retain their earlier unknown execution.
        if (
          firstCreate &&
          error.status === 422 &&
          detail.code === "unsupported_parameter" &&
          detail.type === "invalid_request_error" &&
          !detail.retryable
        ) {
          this.#unsavedUpdate = { value: null };
          this.#save(null);
          this.#unsavedUpdate = null;
          this.#set({ error: detail.code });
          return;
        }
      }
    } catch {
      if (this.#view.blocked) return;
    }
    this.#set({ error: r ? "outcome_unknown" : "input_or_reference_invalid" });
  }
  async #dispatch(op: { epoch: number; signal: AbortSignal }) {
    const r = this.#view.record;
    if (!r?.pending) return;
    this.#save(r);
    if (r.pending === "create") {
      const job = this.#job(
        await this.client.create(structuredClone(r.command), r.createKey, op.signal),
        r,
      );
      if (!this.#live(op.epoch)) return;
      this.#save({ ...r, jobId: job.id, pending: null });
      this.#set({ job, current: false, error: null });
    } else {
      const result = parse(
        "JobCancellation",
        await this.client.cancel(r.jobId!, r.cancelKey, op.signal),
      );
      if (result.job_id !== r.jobId) throw new Error("voice_job_identity");
      if (!this.#live(op.epoch)) return;
      this.#save({ ...r, pending: null });
      this.#set({ cancellation: result, current: false, error: null });
    }
  }
  async replay() {
    if (!this.#view.record?.pending || this.#view.record.operationId) return;
    const op = this.#start();
    if (!op) return;
    try {
      await this.#dispatch(op);
    } catch (error) {
      this.#mutationError(error, op.epoch);
    } finally {
      if (this.#live(op.epoch)) this.#set({ busy: false });
    }
  }
  async refresh() {
    const r = this.#view.record;
    if (!r || (!r.jobId && !r.operationId)) return;
    const op = this.#start();
    if (!op) return;
    try {
      let jobId = r.jobId;
      if (r.operationId) {
        const operation = parse(
          "Operation",
          await this.client.getOperation(r.operationId, op.signal),
        );
        if (!this.#live(op.epoch)) return;
        if (
          operation.id !== r.operationId ||
          operation.session_id ||
          (jobId && operation.job_id && operation.job_id !== jobId)
        )
          throw new Error("voice_operation_identity");
        // Operation.kind currently has no design/clone variants. Its job_id is
        // only a lookup hint; the generated Job plus original scope/kind decide.
        jobId = operation.job_id ?? jobId;
        if (!jobId) {
          this.#set({ error: "operation_recovery" });
          return;
        }
      }
      const job = this.#job(await this.client.getJob(jobId!, op.signal), { ...r, jobId });
      if (this.#live(op.epoch)) {
        if (r.pending === "create")
          this.#save({ ...r, jobId: job.id, pending: null, operationId: null });
        this.#set({ job, current: true });
      }
    } catch {
      if (this.#live(op.epoch) && !this.#view.blocked) this.#set({ error: "current_unavailable" });
    } finally {
      if (this.#live(op.epoch)) this.#set({ busy: false });
    }
  }
  async cancel() {
    const r = this.#view.record;
    if (
      !r?.jobId ||
      r.pending ||
      !this.#view.current ||
      !this.#view.job ||
      terminal.has(this.#view.job.status)
    )
      return;
    const op = this.#start();
    if (!op) return;
    try {
      this.#save({ ...r, pending: "cancel" });
      await this.#dispatch(op);
    } catch (error) {
      this.#mutationError(error, op.epoch);
    } finally {
      if (this.#live(op.epoch)) this.#set({ busy: false });
    }
  }
  canNext() {
    const s = this.#view;
    return (
      !!s.record &&
      !s.record.pending &&
      !s.blocked &&
      !s.busy &&
      s.current &&
      !!s.job &&
      terminal.has(s.job.status) &&
      ["complete", "not_required"].includes(s.job.cleanup_state)
    );
  }
  next() {
    if (!this.#active || this.#disposed || !this.canNext()) return;
    try {
      this.#save(null);
      this.#set({ job: null, cancellation: null, current: false, error: null });
    } catch {
      /* retain */
    }
  }
  setActive(active: boolean) {
    if (this.#disposed || active === this.#active) return;
    this.#active = active;
    if (!active) {
      ++this.#epoch;
      this.#abort?.abort();
      this.#set({ busy: false, current: false });
    }
  }
  dispose() {
    if (this.#disposed) return;
    this.setActive(false);
    this.#disposed = true;
    this.#listeners.clear();
  }
}
