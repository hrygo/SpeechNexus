import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";

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
export type AssistantProblem = "missing" | "conflict" | "rejected" | "unavailable" | "unknown";

export interface AssistantState {
  readonly availability: AssistantAvailability;
  readonly turn: Contracts["AssistantTurn"] | null;
  readonly voices: Contracts["VoiceList"] | null;
  readonly busy: boolean;
  readonly problem: AssistantProblem | null;
  readonly watching: boolean;
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
    voices: null,
    busy: false,
    problem: null,
    watching: false,
  };
  #listeners = new Set<() => void>();
  #timer: ReturnType<typeof setTimeout> | null = null;
  #generation = 0;
  readonly #pollMs: number;

  constructor(
    private readonly client: WorkbenchClient,
    options: { pollMs?: number } = {},
  ) {
    this.#pollMs = options.pollMs ?? 1500;
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
      this.#set({ availability, busy: false, problem: null });
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ availability: "unknown", busy: false, problem: problemOf(error) });
      return;
    }
    if (availability !== "available") return;
    try {
      const voices = await this.client.voices(signal);
      if (generation !== this.#generation) return;
      this.#set({ voices, busy: false, problem: null });
    } catch (error) {
      if (generation !== this.#generation) return;
      // The capability stands; only the catalogue read failed.
      this.#set({ voices: null, busy: false, problem: problemOf(error) });
    }
  }

  async start(
    projectId: string,
    body: Contracts["AssistantTurnCreateRequest"],
    key: string,
  ): Promise<void> {
    const current = this.#state.turn;
    if (
      this.#state.availability !== "available" ||
      this.#state.busy ||
      (current !== null && !isTerminal(current.status))
    )
      return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null });
    try {
      const turn = await this.client.createAssistantTurn(projectId, body, key);
      if (generation !== this.#generation) return;
      this.#set({ turn, busy: false, problem: null });
      this.#watch(turn);
    } catch (error) {
      if (generation !== this.#generation) return;
      this.#set({ busy: false, problem: problemOf(error), watching: false });
    }
  }

  async cancel(key: string): Promise<void> {
    const current = this.#state.turn;
    if (this.#state.busy || current === null || isTerminal(current.status)) return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null });
    try {
      const turn = await this.client.cancelAssistantTurn(current.id, key);
      if (generation !== this.#generation) return;
      assert(turn.id === current.id && turn.project_id === current.project_id);
      // A repeated cancellation is accepted without doing work.
      assert(turn.playback_generation >= current.playback_generation);
      this.#set({ turn, busy: false, problem: null });
      this.#watch(turn);
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
