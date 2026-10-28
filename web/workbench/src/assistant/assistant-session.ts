import { assistantJournal, type AssistantCommand } from "./assistant-journal";
import type { CommandJournal, JournalStorage } from "../recovery/command-journal";
import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";
import { catalogueProblemOf, type CatalogueRead } from "../voices/catalogue-state";

/**
 * The deployment's own verdict on the assistant capability. The consumer never infers
 * readiness from configuration it cannot see, and never invents an answer.
 *
 * - available: the serving runtime can carry the turn out.
 * - disabled: this deployment deliberately does not offer it.
 * - unavailable: it is asked for, but a required dependency is missing.
 * - unknown: the status has not been read yet.
 */
export type AssistantAvailability = "unknown" | "available" | "disabled" | "unavailable";
export type AssistantProblem =
  "missing" | "conflict" | "rejected" | "unavailable" | "unknown" | "storage";

export interface AssistantState {
  readonly availability: AssistantAvailability;
  readonly turn: Contracts["AssistantTurn"] | null;
  /**
   * The published catalogue is a read with its own outcome, not a nullable list: an unread or
   * failed catalogue must never be presented as a deployment that published nothing.
   */
  readonly catalogue: CatalogueRead;
  readonly busy: boolean;
  readonly problem: AssistantProblem | null;
  readonly watching: boolean;
  readonly pending: AssistantCommand | null;
  readonly rejected: boolean;
  readonly blocked: boolean;
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const FLIGHT = new Set(["queued", "running"]);

export function isTerminal(status: string): boolean {
  return TERMINAL.has(status);
}

function assert(valid: boolean): asserts valid {
  if (!valid) throw new Error("Assistant response mismatch");
}

function problemOf(error: unknown): AssistantProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.code === "revision_conflict" || error.code === "idempotency_conflict")
      return "conflict";
    if (error.status >= 400 && error.status < 500) return "rejected";
    return "unavailable";
  }
  // A transport failure is not evidence that the request was rejected.
  if (error instanceof TypeError) return "unavailable";
  return "unknown";
}

/**
 * Watches one assistant turn. The workbench owns the turn, the base runs the job: the
 * turn may only move forward, a terminal turn never changes again, and text is shown
 * only once the contract says the turn succeeded.
 */
export class AssistantSession {
  #state: AssistantState = {
    availability: "unknown",
    turn: null,
    catalogue: { kind: "unread" },
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
  #journal: CommandJournal<AssistantCommand> | null = null;

  constructor(
    private readonly client: WorkbenchClient,
    options: { pollMs?: number; storage?: JournalStorage } = {},
  ) {
    this.#pollMs = options.pollMs ?? 1500;
    try {
      this.#journal = assistantJournal(options.storage);
      const record = this.#journal.load();
      this.#state = {
        ...this.#state,
        pending: record.command,
        rejected: record.rejected,
        problem: record.command ? (record.rejected ? "rejected" : "unknown") : null,
      };
    } catch {
      this.#state = { ...this.#state, blocked: true, problem: "storage" };
    }
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getSnapshot = (): AssistantState => this.#state;

  /** Read the deployment verdict, then the published catalogue when turns are possible. */
  async load(signal?: AbortSignal): Promise<void> {
    const generation = this.#generation;
    this.#set({ busy: true, problem: null });
    let availability: AssistantAvailability;
    try {
      const status = await this.client.status(signal);
      if (generation !== this.#generation) return;
      assert(Array.isArray(status.capabilities));
      const capability = status.capabilities.find((item) => item.name === "assistant");
      assert(capability !== undefined);
      availability =
        status.workbench_enabled && capability.status === "available"
          ? "available"
          : capability.status === "available"
            ? "unavailable"
            : capability.status;
      this.#set({
        availability,
        busy: false,
        problem: this.#state.blocked ? "storage" : this.#state.pending ? "unknown" : null,
      });
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ availability: "unknown", busy: false, problem: problemOf(error) });
      return;
    }
    if (availability !== "available") return;
    await this.readCatalogue(signal);
  }

  /**
   * Read the published catalogue on its own. The assistant capability stands on the status
   * read, so a failed catalogue is reported against the catalogue alone: the form stays
   * closed with a named reason and an explicit retry, instead of the deployment being called
   * empty or the whole capability being reported as missing.
   */
  async readCatalogue(signal?: AbortSignal): Promise<void> {
    const generation = this.#generation;
    this.#set({ catalogue: { kind: "loading" } });
    try {
      const page = await this.client.voices(signal);
      if (generation !== this.#generation) return;
      this.#set({ catalogue: { kind: "ready", voices: page.data }, busy: false });
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({
        catalogue: { kind: "problem", problem: catalogueProblemOf(error) },
        busy: false,
      });
    }
  }

  async start(
    projectId: string,
    body: Contracts["AssistantTurnCreateRequest"],
    key: string,
  ): Promise<void> {
    const current = this.#state.turn;
    if (
      this.#state.pending ||
      this.#state.availability !== "available" ||
      this.#state.busy ||
      (current !== null && !isTerminal(current.status))
    )
      return;
    await this.#begin({ kind: "create", projectId, body: structuredClone(body), key });
  }

  async cancel(key: string): Promise<void> {
    const current = this.#state.turn;
    if (this.#state.pending || this.#state.busy || !current || isTerminal(current.status)) return;
    await this.#begin({ kind: "cancel", turnId: current.id, key });
  }
  async #begin(command: AssistantCommand) {
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
      const turn =
        command.kind === "create"
          ? await this.client.createAssistantTurn(command.projectId, command.body, command.key)
          : await this.client.cancelAssistantTurn(command.turnId, command.key);
      if (generation !== this.#generation) return;
      if (command.kind === "create") {
        assert(
          turn.project_id === command.projectId && turn.input_text === command.body.input_text,
        );
        assert(
          turn.voice.id === command.body.voice.id &&
            turn.voice.revision === command.body.voice.revision,
        );
      } else {
        assert(turn.id === command.turnId);
        const current = this.#state.turn;
        if (current)
          assert(
            turn.project_id === current.project_id &&
              turn.playback_generation >= current.playback_generation,
          );
      }
      this.#settled(turn);
      try {
        this.#journal.clear();
      } catch {
        this.#set({ busy: false, blocked: true, problem: "storage" });
        return;
      }
      this.#set({ turn, pending: null, rejected: false, busy: false, problem: null });
      this.#watch(turn);
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

  #watch(turn: Contracts["AssistantTurn"]): void {
    this.#clear();
    const generation = this.#generation;
    this.#settled(turn);
    if (isTerminal(turn.status)) {
      this.#set({ watching: false });
      return;
    }
    assert(FLIGHT.has(turn.status));
    this.#timer = setTimeout(() => {
      this.#timer = null;
      void this.#poll(generation, turn.id);
    }, this.#pollMs);
    this.#set({ watching: true });
  }

  async #poll(generation: number, id: string): Promise<void> {
    if (generation !== this.#generation) return;
    try {
      const next = await this.client.assistantTurn(id);
      if (generation !== this.#generation) return;
      const previous = this.#state.turn;
      assert(
        previous !== null && next.id === previous.id && next.project_id === previous.project_id,
      );
      assert(next.playback_generation >= previous.playback_generation);
      if (isTerminal(previous.status)) return;
      this.#set({ turn: next, problem: null, busy: false });
      this.#watch(next);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ busy: false, problem: problemOf(error), watching: false });
    }
  }

  /** The contract ties an answer to the job that produced it, and one error to failure. */
  #settled(turn: Contracts["AssistantTurn"]): void {
    if (turn.status === "failed") {
      assert(turn.error !== null);
    } else {
      assert(turn.error === null);
    }
    if (turn.status === "succeeded") assert(turn.speech_job_id !== null);
    // A turn that names a job always carries the text that job produced.
    if (turn.speech_job_id !== null) assert(turn.output_text !== null);
  }

  #set(next: Partial<AssistantState>): void {
    this.#state = { ...this.#state, ...next };
    for (const listener of this.#listeners) listener();
  }
}
