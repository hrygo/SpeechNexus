import { exportJournal, type ExportCommand } from "./book-journal";
import type { CommandJournal, JournalStorage } from "../recovery/command-journal";
import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";

export type ExportFormat = Contracts["ExportCreateRequest"]["format"];
export type BookExportProblem =
  "unavailable" | "conflict" | "rejected" | "missing" | "unknown" | "storage";

export interface BookExportState {
  readonly job: Contracts["ExportJob"] | null;
  readonly busy: boolean;
  readonly problem: BookExportProblem | null;
  readonly watching: boolean;
  readonly pending: ExportCommand | null;
  readonly rejected: boolean;
  readonly blocked: boolean;
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
  #state: BookExportState = {
    job: null,
    busy: false,
    problem: null,
    watching: false,
    pending: null,
    rejected: false,
    blocked: false,
  };
  #listeners = new Set<() => void>();
  #timer: ReturnType<typeof setTimeout> | null = null;
  #generation = 0;
  readonly #pollMs: number;
  #journal: CommandJournal<ExportCommand> | null = null;

  constructor(
    private readonly client: WorkbenchClient,
    private readonly book: Contracts["Book"],
    options: { pollMs?: number; storage?: JournalStorage } = {},
  ) {
    this.#pollMs = options.pollMs ?? 1500;
    try {
      this.#journal = exportJournal(book.id, options.storage);
      const record = this.#journal.load();
      if (record.command) assert(record.command.bookId === book.id);
      this.#state = {
        ...this.#state,
        pending: record.command,
        rejected: record.rejected,
        problem: record.command ? "unknown" : null,
      };
    } catch {
      this.#state = { ...this.#state, blocked: true, problem: "storage" };
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getSnapshot = (): BookExportState => this.#state;

  async start(format: ExportFormat, key: string): Promise<void> {
    const current = this.#state.job;
    if (
      this.#state.pending ||
      this.#state.busy ||
      (current !== null && !isTerminal(current.status))
    )
      return;
    await this.#begin({
      kind: "create",
      bookId: this.book.id,
      body: { expected_casting_revision: this.book.casting_revision, format },
      key,
    });
  }

  async cancel(key: string): Promise<void> {
    const current = this.#state.job;
    if (this.#state.pending || this.#state.busy || !current || isTerminal(current.status)) return;
    await this.#begin({ kind: "cancel", bookId: current.book_id, exportId: current.id, key });
  }
  async #begin(command: ExportCommand) {
    try {
      if (!this.#journal) throw new Error("Recovery unavailable");
      this.#journal.begin(command);
      this.#set({ pending: this.#journal.load().command, rejected: false, blocked: false });
    } catch {
      this.#set({ blocked: true, problem: "storage" });
      return;
    }
    await this.replay();
  }
  async replay(): Promise<void> {
    const command = this.#state.pending;
    if (!command || this.#state.busy || !this.#journal) return;
    try {
      this.#journal.save(this.#journal.load());
    } catch {
      this.#set({ blocked: true, problem: "storage" });
      return;
    }
    const generation = this.#generation;
    this.#clear();
    this.#set({ busy: true, blocked: false, problem: null, watching: false });
    try {
      const job =
        command.kind === "create"
          ? await this.client.createBookExport(command.bookId, command.body, command.key)
          : await this.client.cancelBookExport(command.exportId, command.key);
      if (generation !== this.#generation) return;
      assert(job.book_id === command.bookId);
      if (command.kind === "create") {
        assert(
          job.manifest_revision === command.body.expected_casting_revision &&
            job.format === command.body.format,
        );
      } else {
        assert(job.id === command.exportId);
        const current = this.#state.job;
        if (current) {
          assert(
            job.manifest_revision === current.manifest_revision && job.format === current.format,
          );
          assert(BigInt(job.progress_completed) >= BigInt(current.progress_completed));
        }
      }
      try {
        this.#journal.clear();
      } catch {
        this.#set({ busy: false, blocked: true, problem: "storage" });
        return;
      }
      this.#set({ job, pending: null, rejected: false, busy: false, problem: null });
      this.#watch(job);
    } catch (error) {
      if (generation !== this.#generation) return;
      const rejected =
        error instanceof WorkbenchResponseError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status);
      try {
        this.#journal.failure(rejected);
        const record = this.#journal.load();
        this.#set({
          busy: false,
          rejected: record.rejected,
          problem: record.uncertain ? "unknown" : problemOf(error),
        });
      } catch {
        this.#set({ busy: false, blocked: true, problem: "storage" });
      }
    }
  }
  dismissRejected(): void {
    if (!this.#state.rejected || this.#state.busy || !this.#journal) return;
    try {
      this.#journal.clear();
      this.#set({ pending: null, rejected: false, problem: null, blocked: false });
    } catch {
      this.#set({ blocked: true, problem: "storage" });
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
