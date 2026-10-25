import {
  TaskResponseError,
  type Job,
  type JobCancellation,
  type JobList,
  type TaskApi,
} from "./job-client";
import type { TaskRecoveryJournal, TaskRecoveryRecord } from "./task-recovery";

type ReadPhase = "idle" | "loading" | "ready" | "error";
export type CancellationSnapshot = {
  jobId: string;
  phase: "submitting" | "unknown" | "received";
  response: JobCancellation | null;
};
export type TaskCenterSnapshot = {
  page: JobList | null;
  listPhase: ReadPhase;
  selectedId: string | null;
  detail: Job | null;
  detailPhase: ReadPhase;
  cancellation: CancellationSnapshot | null;
  notice: string;
  stream: "idle" | "connecting" | "listening" | "ended" | "interrupted" | "history-expired";
  cursor: string | undefined;
  streamNotice: string;
  recovery: "memory" | "ready" | "unavailable";
  recoveryNotice: string;
};

const terminal = (job: Job) => ["succeeded", "failed", "cancelled", "expired"].includes(job.status);

/** One page of observed facts and one bounded, explicitly retriable cancellation intent. */
export class TaskCenterSession {
  readonly #api: TaskApi;
  readonly #listeners = new Set<() => void>();
  #snapshot: TaskCenterSnapshot = {
    page: null,
    listPhase: "idle",
    selectedId: null,
    detail: null,
    detailPhase: "idle",
    cancellation: null,
    notice: "尚未读取任务。",
    stream: "idle",
    cursor: undefined,
    streamNotice: "尚未连接事件；列表与详情为读取时快照。",
    recovery: "memory",
    recoveryNotice: "当前入口仅保留本页恢复信息，刷新会丢失。",
  };
  #listAbort: AbortController | null = null;
  #detailAbort: AbortController | null = null;
  #cancelAbort: AbortController | null = null;
  #streamAbort: AbortController | null = null;
  #listGeneration = 0;
  #detailGeneration = 0;
  #cancelKey: string | null = null;
  #disposed = false;
  #record: TaskRecoveryRecord = { version: 1, selectedId: null, cursor: null, cancellation: null };
  constructor(
    api: TaskApi,
    private readonly journal?: TaskRecoveryJournal,
  ) {
    this.#api = api;
    if (journal) {
      try {
        this.#record = journal.load() ?? this.#record;
        const { selectedId, cursor, cancellation } = this.#record;
        this.#cancelKey = cancellation?.key ?? null;
        this.#snapshot = {
          ...this.#snapshot,
          selectedId,
          cursor: cursor ?? undefined,
          cancellation: cancellation
            ? { jobId: cancellation.jobId, phase: "unknown", response: null }
            : null,
          recovery: "ready",
          recoveryNotice:
            "本标签页保留任务标识、取消原键与已确认水位；恢复后请显式重读，信息不代表当前状态。关闭标签页或清除站点数据后不保证恢复。",
          ...(selectedId ? { notice: "已恢复任务标识，请重读所选任务；未自动发送任何请求。" } : {}),
        };
      } catch {
        this.#storageFailed();
      }
    }
  }
  getSnapshot = () => this.#snapshot;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #set(update: Partial<TaskCenterSnapshot>) {
    if (this.#disposed) return;
    this.#snapshot = { ...this.#snapshot, ...update };
    for (const listener of this.#listeners) listener();
  }
  #storageFailed() {
    this.#set({
      recovery: "unavailable",
      recoveryNotice:
        "恢复存储不可用、损坏或已被其他页面修改。已阻止取消与事件推进；仍可读取任务。请保留页面，修复存储后刷新核对原记录。",
    });
  }
  #persist(update: Partial<TaskRecoveryRecord>): boolean {
    if (this.#snapshot.recovery === "unavailable") return false;
    const record = { ...this.#record, ...update };
    try {
      this.journal?.save(record);
      this.#record = record;
      return true;
    } catch {
      this.#storageFailed();
      return false;
    }
  }

  async load(cursor?: string): Promise<void> {
    if (this.#disposed) return;
    this.#listAbort?.abort();
    const controller = (this.#listAbort = new AbortController());
    const generation = ++this.#listGeneration;
    this.#set({ listPhase: "loading" });
    try {
      const page = await this.#api.list(cursor, controller.signal);
      if (generation !== this.#listGeneration || controller.signal.aborted) return;
      this.#set({ page, listPhase: "ready", notice: "已读取任务列表快照。" });
    } catch {
      if (generation !== this.#listGeneration || controller.signal.aborted) return;
      this.#set({ listPhase: "error", notice: "列表读取失败；保留上次快照，请重新读取。" });
    }
  }

  async select(id: string): Promise<void> {
    if (this.#disposed) return;
    this.stopEvents();
    this.#detailAbort?.abort();
    const controller = (this.#detailAbort = new AbortController());
    const generation = ++this.#detailGeneration;
    const cursor = this.#snapshot.selectedId === id ? this.#snapshot.cursor : undefined;
    this.#persist({ selectedId: id, cursor: cursor ?? null });
    this.#set({
      selectedId: id,
      detail: this.#snapshot.selectedId === id ? this.#snapshot.detail : null,
      detailPhase: "loading",
      cursor,
      stream: "idle",
      streamNotice: "事件未连接；可显式连接以接收任务更新。",
    });
    try {
      const detail = await this.#api.getJob(id, controller.signal);
      if (generation !== this.#detailGeneration || controller.signal.aborted) return;
      if (detail.id !== id) throw new Error("Task resource mismatch");
      this.#set({
        detail,
        detailPhase: "ready",
        notice: "已重读任务；资源释放以 cleanup_state 为准。",
      });
    } catch {
      if (generation !== this.#detailGeneration || controller.signal.aborted) return;
      this.#set({ detailPhase: "error", notice: "详情读取失败；上次快照不代表当前状态。" });
    }
  }

  async refreshSelected(): Promise<void> {
    if (this.#snapshot.selectedId) await this.select(this.#snapshot.selectedId);
  }

  eventsAvailable(): boolean {
    return (
      !this.#disposed &&
      this.#snapshot.recovery !== "unavailable" &&
      typeof this.#api.events === "function"
    );
  }

  stopEvents(): void {
    const owner = this.#streamAbort;
    if (!owner) return;
    this.#streamAbort = null;
    owner.abort();
    this.#set({
      stream: "idle",
      streamNotice: "事件连接已停止；保留已确认水位，快照不代表当前状态。",
      ...(this.#snapshot.detailPhase === "loading" ? { detailPhase: "error" as const } : {}),
    });
  }

  async connectEvents(): Promise<void> {
    const events = this.#api.events?.bind(this.#api);
    const { selectedId: id, detail, detailPhase } = this.#snapshot;
    if (
      !this.eventsAvailable() ||
      !events ||
      !id ||
      !detail ||
      detail.id !== id ||
      detailPhase !== "ready"
    )
      return;
    this.stopEvents();
    const owner = (this.#streamAbort = new AbortController());
    const generation = this.#detailGeneration;
    const current = () =>
      !this.#disposed &&
      this.#streamAbort === owner &&
      !owner.signal.aborted &&
      this.#detailGeneration === generation &&
      this.#snapshot.selectedId === id;
    const refresh = async () => {
      this.#set({ detailPhase: "loading" });
      const job = await this.#api.getJob(id, owner.signal);
      if (job.id !== id) throw new Error("Task resource mismatch");
      return job;
    };
    this.#set({
      stream: "connecting",
      streamNotice: "正在连接并等待事件；更新后重读权威任务快照。",
    });
    try {
      for await (const event of events(id, this.#snapshot.cursor, owner.signal)) {
        if (!current()) return;
        if (event.job_id !== id) throw new Error("Task event identity mismatch");
        if (this.#snapshot.cursor && BigInt(event.id) <= BigInt(this.#snapshot.cursor)) continue;
        // Events invalidate the current snapshot; they do not describe a complete Job.
        const job = await refresh();
        if (!current()) return;
        if (!this.#persist({ cursor: event.id })) throw new Error("Unconfirmed recovery cursor");
        this.#set({
          detail: job,
          detailPhase: "ready",
          cursor: event.id,
          stream: "listening",
          streamNotice: "已按事件重读任务；取消接受、终态与资源清理分别核对。",
        });
      }
      if (current())
        this.#set({ stream: "ended", streamNotice: "事件连接已结束；可按已确认水位重新连接。" });
    } catch (error) {
      if (!current()) return;
      if (
        error instanceof TaskResponseError &&
        error.status === 410 &&
        error.code === "event_history_expired"
      ) {
        if (!this.#persist({ cursor: null })) {
          this.#set({
            stream: "interrupted",
            streamNotice: "事件历史已过期，但旧水位清除未保存；请修复恢复存储后重读。",
          });
          return;
        }
        this.#set({ cursor: undefined });
        try {
          const job = await refresh();
          if (current())
            this.#set({
              detail: job,
              detailPhase: "ready",
              stream: "history-expired",
              streamNotice: "事件历史已过期，已重读当前任务；下次不携带旧水位。",
            });
        } catch {
          if (current())
            this.#set({
              stream: "interrupted",
              detailPhase: "error",
              streamNotice: "事件历史已过期，恢复状态未确认；请重读任务。",
            });
        }
      } else
        this.#set({
          stream: "interrupted",
          ...(this.#snapshot.detailPhase === "loading" ? { detailPhase: "error" as const } : {}),
          streamNotice: "事件连接中断；保留已确认水位和旧快照，请重读或重新连接。",
        });
    } finally {
      owner.abort();
      if (this.#streamAbort === owner) this.#streamAbort = null;
    }
  }

  canCancel(): boolean {
    const { detail, detailPhase, cancellation } = this.#snapshot;
    return (
      !this.#disposed &&
      this.#snapshot.recovery !== "unavailable" &&
      detailPhase === "ready" &&
      detail !== null &&
      !terminal(detail) &&
      detail.status !== "cancelling" &&
      (!cancellation || (cancellation.phase === "received" && cancellation.jobId !== detail.id))
    );
  }

  async cancelSelected(): Promise<void> {
    if (!this.canCancel()) return;
    const jobId = this.#snapshot.detail!.id;
    this.#cancelKey = crypto.randomUUID();
    await this.#sendCancellation(jobId, this.#cancelKey);
  }

  async replayCancellation(): Promise<void> {
    const pending = this.#snapshot.cancellation;
    if (
      this.#disposed ||
      this.#snapshot.recovery === "unavailable" ||
      pending?.phase !== "unknown" ||
      !this.#cancelKey
    )
      return;
    await this.#sendCancellation(pending.jobId, this.#cancelKey);
  }

  async #sendCancellation(jobId: string, key: string): Promise<void> {
    if (!this.#persist({ cancellation: { jobId, key } })) return;
    const controller = (this.#cancelAbort = new AbortController());
    this.#set({ cancellation: { jobId, phase: "submitting", response: null } });
    try {
      const response = await this.#api.cancel(jobId, key, controller.signal);
      if (this.#disposed || controller.signal.aborted) return;
      if (response.job_id !== jobId) throw new Error("Task cancellation identity mismatch");
      if (!this.#persist({ cancellation: null }))
        throw new Error("Unconfirmed recovery acknowledgement");
      this.#set({
        cancellation: { jobId, phase: "received", response },
        notice: "已收到取消响应；请分别核对任务状态和资源清理。",
      });
      if (this.#snapshot.selectedId === jobId) await this.refreshSelected();
    } catch {
      if (this.#disposed || controller.signal.aborted) return;
      this.#set({
        cancellation: { jobId, phase: "unknown", response: null },
        notice: "取消结果未知；重读原任务，或按原键重放取消。",
      });
    }
  }

  dispose(): void {
    this.#disposed = true;
    this.#listAbort?.abort();
    this.#detailAbort?.abort();
    this.#cancelAbort?.abort();
    this.#streamAbort?.abort();
    this.#listeners.clear();
  }
}
