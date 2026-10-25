import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";

export type ExportFormat = Contracts["ExportCreateRequest"]["format"];
export type BookExportProblem = "unavailable" | "conflict" | "rejected" | "missing" | "unknown";

export interface BookExportState {
  readonly job: Contracts["ExportJob"] | null;
  readonly busy: boolean;
  readonly problem: BookExportProblem | null;
  readonly watching: boolean;
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "expired"]);
const FLIGHT = new Set(["queued", "running", "cancelling"]);

export function isTerminal(status: string): boolean {
  return TERMINAL.has(status);
}

function assert(valid: boolean): asserts valid {
  if (!valid) throw new Error("Export response mismatch");
}

function problemOf(error: unknown): BookExportProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.code === "revision_conflict" || error.code === "idempotency_conflict")
      return "conflict";
    if (error.code === "invalid_change") return "rejected";
    if (error.status >= 500 || error.code === "store_unavailable") return "unavailable";
    return "unknown";
  }
  // A transport failure is not evidence that the request was rejected.
  if (error instanceof TypeError) return "unavailable";
  return "unknown";
}

/**
 * Watches one book export. Progress may only move forward, a terminal export never
 * changes again, and a late answer never overwrites a newer one.
 */
export class BookExportSession {
  #state: BookExportState = { job: null, busy: false, problem: null, watching: false };
  #listeners = new Set<() => void>();
  #timer: ReturnType<typeof setTimeout> | null = null;
  #generation = 0;
  readonly #pollMs: number;

  constructor(
    private readonly client: WorkbenchClient,
    private readonly book: Contracts["Book"],
    options: { pollMs?: number } = {},
  ) {
    this.#pollMs = options.pollMs ?? 1500;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getSnapshot = (): BookExportState => this.#state;

  async start(format: ExportFormat, key: string): Promise<void> {
    const current = this.#state.job;
    if (this.#state.busy || (current !== null && !isTerminal(current.status))) return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null });
    try {
      const job = await this.client.createBookExport(
        this.book.id,
        { expected_casting_revision: this.book.casting_revision, format },
        key,
      );
      if (generation !== this.#generation) return;
      this.#set({ job, busy: false, problem: null });
      this.#watch(job);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ busy: false, problem: problemOf(error), watching: false });
    }
  }

  async cancel(key: string): Promise<void> {
    const current = this.#state.job;
    if (this.#state.busy || current === null || isTerminal(current.status)) return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null });
    try {
      const job = await this.client.cancelBookExport(current.id, key);
      if (generation !== this.#generation) return;
      assert(job.id === current.id && job.book_id === current.book_id);
      assert(job.manifest_revision === current.manifest_revision && job.format === current.format);
      // A repeated cancellation is accepted without doing work.
      assert(BigInt(job.progress_completed) >= BigInt(current.progress_completed));
      this.#set({ job, busy: false, problem: null });
      this.#watch(job);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ busy: false, problem: problemOf(error), watching: false });
    }
  }

  dispose(): void {
    this.#generation += 1;
    this.#clear();
    this.#listeners.clear();
  }

  #clear(): void {
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
  }

  #watch(job: Contracts["ExportJob"]): void {
    this.#clear();
    const generation = this.#generation;
    if (isTerminal(job.status)) {
      this.#set({ watching: false });
      return;
    }
    assert(FLIGHT.has(job.status));
    this.#timer = setTimeout(() => {
      this.#timer = null;
      void this.#poll(generation, job.id);
    }, this.#pollMs);
    this.#set({ watching: true });
  }

  async #poll(generation: number, id: string): Promise<void> {
    if (generation !== this.#generation) return;
    try {
      const next = await this.client.bookExport(id);
      if (generation !== this.#generation) return;
      const previous = this.#state.job;
      assert(previous !== null && next.id === previous.id && next.book_id === previous.book_id);
      assert(
        next.format === previous.format && next.manifest_revision === previous.manifest_revision,
      );
      assert(BigInt(next.progress_completed) >= BigInt(previous.progress_completed));
      if (isTerminal(previous.status)) return;
      this.#set({ job: next, problem: null, busy: false });
      this.#watch(next);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ busy: false, problem: problemOf(error), watching: false });
    }
  }

  #set(next: Partial<BookExportState>): void {
    this.#state = { ...this.#state, ...next };
    for (const listener of this.#listeners) listener();
  }
}
