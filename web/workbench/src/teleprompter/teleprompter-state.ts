import {
  WorkbenchResponseError,
  type Contracts,
  type WorkbenchClient,
} from "../manuscripts/workbench-client";

export type TeleprompterProblem = "missing" | "conflict" | "rejected" | "unavailable" | "unknown";

export interface TeleprompterServerState {
  readonly projectId: string | null;
  readonly state: Contracts["TeleprompterState"] | null;
  readonly busy: boolean;
  readonly problem: TeleprompterProblem | null;
  readonly accepted: boolean;
}

function problemOf(error: unknown): TeleprompterProblem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.code === "revision_conflict" || error.code === "idempotency_conflict")
      return "conflict";
    if (error.status >= 400 && error.status < 500) return "rejected";
    return "unavailable";
  }
  // A transport failure is not evidence that the write was rejected.
  if (error instanceof TypeError) return "unavailable";
  return "unknown";
}

/**
 * The deployment's teleprompter state for one project: script revision, visible position
 * and manual takeover. The script text is not part of it, and an unsaved project is a
 * 404 — never a successful zero position with an invented revision.
 */
export class TeleprompterStateSession {
  #state: TeleprompterServerState = {
    projectId: null,
    state: null,
    busy: false,
    problem: null,
    accepted: false,
  };
  #listeners = new Set<() => void>();
  #generation = 0;

  constructor(private readonly client?: WorkbenchClient) {}

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getSnapshot = (): TeleprompterServerState => this.#state;

  async load(projectId: string, signal?: AbortSignal): Promise<void> {
    if (this.client === undefined || this.#state.busy) return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null, accepted: false });
    try {
      const state = await this.client.teleprompterState(projectId, signal);
      if (generation !== this.#generation) return;
      this.#set({ projectId, state, busy: false, problem: null });
    } catch (error) {
      if (generation !== this.#generation) return;
      // A missing state leaves the position unknown; it is reported, not filled in.
      this.#set({
        projectId,
        state: null,
        busy: false,
        problem: problemOf(error),
        accepted: false,
      });
    }
  }

  async save(
    projectId: string,
    body: Contracts["TeleprompterUpdateRequest"],
    key: string,
  ): Promise<void> {
    if (this.client === undefined || this.#state.busy) return;
    const generation = this.#generation;
    this.#set({ busy: true, problem: null, accepted: false });
    try {
      const state = await this.client.updateTeleprompter(projectId, body, key);
      if (generation !== this.#generation) return;
      this.#set({ projectId, state, busy: false, problem: null, accepted: true });
    } catch (error) {
      if (generation !== this.#generation) return;
      // A conflict keeps the previous reading of the state; nothing is retried here.
      this.#set({ busy: false, problem: problemOf(error), accepted: false });
    }
  }

  dispose(): void {
    this.#generation += 1;
    this.#listeners.clear();
  }

  #set(next: Partial<TeleprompterServerState>): void {
    this.#state = { ...this.#state, ...next };
    for (const listener of this.#listeners) listener();
  }
}
