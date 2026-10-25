import {
  type Contracts,
  type WorkbenchClient,
  WorkbenchResponseError,
} from "../manuscripts/workbench-client";
import {
  type DeletionJournal,
  type DeletionRecord,
  type DeletionCommand,
  emptyDeletionRecord,
} from "./deletion-journal";
import { checkDeletion, confirmation, sameTargets, type DeletionTarget } from "./deletion-plan";

export type DeletionProblem =
  "unknown" | "rejected" | "read_failed" | "storage" | "unavailable" | "invalid" | "expired";
export type DeletionSnapshot = {
  record: DeletionRecord;
  status: Contracts["WorkbenchStatusV2"] | null;
  projects: Contracts["ProjectList"] | null;
  transcripts: Contracts["TranscriptList"] | null;
  project: Contracts["Project"] | null;
  target: DeletionTarget | null;
  current: Contracts["Deletion"] | null;
  busy: boolean;
  blocked: boolean;
  problem: DeletionProblem | null;
};
function freeze<T>(v: T): T {
  if (v && typeof v === "object" && !Object.isFrozen(v)) {
    Object.values(v).forEach(freeze);
    Object.freeze(v);
  }
  return v;
}
function merge<T extends { id: string }>(a: T[], b: T[]) {
  return [...new Map([...a, ...b].map((v) => [v.id, v])).values()];
}

/** A new mutation never crosses the network before the exact durable intent barrier. */
export class DeletionSession {
  #state: DeletionSnapshot;
  #listeners = new Set<() => void>();
  #disposed = false;
  #started = false;
  #generation = 0;
  #abort: AbortController | null = null;
  constructor(
    private readonly client: WorkbenchClient,
    private readonly journal: DeletionJournal,
  ) {
    const record = journal.load();
    this.#state = freeze({
      record,
      status: null,
      projects: null,
      transcripts: null,
      project: null,
      target: record.request?.target ?? null,
      current: null,
      busy: false,
      blocked: false,
      problem: record.pending ? "unknown" : null,
    });
  }
  getSnapshot = () => this.#state;
  subscribe = (f: () => void) => {
    this.#listeners.add(f);
    return () => {
      this.#listeners.delete(f);
    };
  };
  #publish(change: Partial<DeletionSnapshot>) {
    if (this.#disposed) return;
    this.#state = freeze({ ...this.#state, ...change });
    this.#listeners.forEach((f) => f());
  }
  #persist(record: DeletionRecord, change: Partial<DeletionSnapshot> = {}) {
    if (this.#disposed) return false;
    try {
      this.journal.save(record);
      this.#publish({ ...change, record, blocked: false });
      return true;
    } catch {
      this.#publish({ ...change, record, blocked: true, problem: "storage" });
      return false;
    }
  }
  #idle() {
    return !this.#disposed && !this.#state.busy && !this.#state.blocked;
  }
  #selectable() {
    return this.#idle() && !this.#state.record.request && !this.#state.record.pending;
  }
  async #read(task: (signal: AbortSignal) => Promise<Partial<DeletionSnapshot>>) {
    if (this.#disposed || this.#state.busy) return;
    const generation = ++this.#generation,
      abort = new AbortController();
    this.#abort = abort;
    this.#publish({ busy: true });
    try {
      const change = await task(abort.signal);
      if (!this.#disposed && generation === this.#generation) this.#publish(change);
    } catch {
      if (!this.#disposed && generation === this.#generation)
        this.#publish({ current: null, problem: "read_failed" });
    } finally {
      if (!this.#disposed && generation === this.#generation) this.#publish({ busy: false });
    }
  }
  async start() {
    if (this.#started || this.#disposed) return;
    this.#started = true;
    await this.discover();
    if (!this.#disposed && this.#state.record.accepted) await this.refresh();
  }
  async discover() {
    await this.#read(async (signal) => {
      const [status, projects] = await Promise.all([
        this.client.status(signal),
        this.client.projects(undefined, signal),
      ]);
      return {
        status,
        projects,
        problem: this.#state.blocked
          ? "storage"
          : this.#state.record.pending
            ? "unknown"
            : !status.workbench_enabled
              ? "unavailable"
              : null,
      };
    });
  }
  async moreProjects() {
    if (!this.#selectable() || !this.#state.projects?.next_cursor) return;
    const before = this.#state.projects;
    await this.#read(async (signal) => {
      const page = await this.client.projects(before.next_cursor!, signal);
      return { projects: { ...page, data: merge(before.data, page.data) } };
    });
  }
  async selectProject(id: string) {
    if (!this.#selectable()) return;
    await this.#read(async (signal) => {
      const [project, transcripts] = await Promise.all([
        this.client.project(id, signal),
        this.client.transcripts(id, undefined, signal),
      ]);
      return {
        project,
        transcripts,
        target: {
          owner: "workbench",
          kind: "project",
          resource: { id: project.id, revision: String(project.metadata_revision) },
        },
        problem: null,
      };
    });
  }
  async moreTranscripts() {
    if (!this.#selectable() || !this.#state.project || !this.#state.transcripts?.next_cursor)
      return;
    const before = this.#state.transcripts,
      id = this.#state.project.id;
    await this.#read(async (signal) => {
      const page = await this.client.transcripts(id, before.next_cursor!, signal);
      return { transcripts: { ...page, data: merge(before.data, page.data) } };
    });
  }
  async selectTranscript(id: string) {
    if (!this.#selectable() || !this.#state.project) return;
    const projectId = this.#state.project.id;
    await this.#read(async (signal) => {
      const view = await this.client.view(id, undefined, signal);
      if (view.transcript.project_id !== projectId) throw Error("Transcript scope mismatch");
      return {
        target: {
          owner: "workbench",
          kind: "transcript",
          resource: { id, revision: view.transcript.head_revision },
        },
        problem: null,
      };
    });
  }
  async preview(mode: Contracts["DeletionPlanRequest"]["mode"]) {
    if (!this.#selectable() || !this.#state.target || !this.#state.status?.workbench_enabled)
      return;
    const request = { target: this.#state.target, mode };
    const pending: DeletionCommand = { kind: "preview", key: crypto.randomUUID(), body: request };
    if (
      this.#persist(
        { ...emptyDeletionRecord(), request, pending },
        { problem: null, current: null },
      )
    )
      await this.replay();
  }
  async confirm(targets: DeletionTarget[]) {
    const { plan, pending, accepted } = this.#state.record;
    if (!this.#idle() || !plan || pending || accepted || !this.#state.status?.workbench_enabled)
      return;
    if (Date.parse(plan.expires_at) <= Date.now()) {
      this.#publish({ problem: "expired" });
      return;
    }
    let body: Contracts["DeletionCreateRequest"];
    try {
      body = confirmation(plan);
      if (!sameTargets(targets, body.confirm_targets)) throw Error("Incomplete confirmation");
    } catch {
      this.#publish({ problem: "invalid" });
      return;
    }
    if (
      this.#persist(
        { ...this.#state.record, pending: { kind: "accept", key: crypto.randomUUID(), body } },
        { problem: null, current: null },
      )
    )
      await this.replay();
  }
  async replay() {
    const p = this.#state.record.pending;
    if (!this.#idle() || !p) return;
    if (!this.#persist(this.#state.record)) return;
    this.#publish({ busy: true, current: null, problem: null });
    let readCurrent = false;
    try {
      if (p.kind === "preview") {
        const plan = await this.client.previewDeletion(p.body, p.key);
        this.#persist({ ...this.#state.record, plan, pending: null }, { problem: null });
      } else {
        const accepted = await this.client.acceptDeletion(p.body, p.key);
        checkDeletion(accepted, p.body, this.#state.record.plan!);
        readCurrent = this.#persist(
          { ...this.#state.record, accepted, pending: null },
          { problem: null },
        );
      }
    } catch (error) {
      const rejected =
        error instanceof WorkbenchResponseError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status);
      this.#publish({ problem: rejected ? "rejected" : "unknown" });
    } finally {
      this.#publish({ busy: false });
    }
    if (readCurrent && !this.#disposed) await this.refresh();
  }
  async refresh() {
    const { accepted, plan } = this.#state.record;
    if (!accepted || !plan || this.#state.blocked || this.#state.record.pending) return;
    // Hide the old GET fact as soon as a fresh read starts; the original ack remains separate.
    this.#publish({ current: null });
    await this.#read(async (signal) => {
      const current = await this.client.deletion(accepted.id, signal);
      if (current.id !== accepted.id) throw Error("Deletion identity mismatch");
      checkDeletion(current, confirmation(plan), plan);
      return { current, problem: null };
    });
  }
  retryStorage() {
    if (!this.#disposed && !this.#state.busy)
      this.#persist(this.#state.record, { problem: this.#state.record.pending ? "unknown" : null });
  }
  dismissRejected() {
    if (this.#idle() && this.#state.problem === "rejected" && this.#state.record.pending)
      this.#persist(emptyDeletionRecord(), {
        current: null,
        problem: null,
        target: null,
        project: null,
        transcripts: null,
      });
  }
  reset() {
    if (this.#idle() && !this.#state.record.pending)
      this.#persist(emptyDeletionRecord(), {
        current: null,
        problem: null,
        target: null,
        project: null,
        transcripts: null,
      });
  }
  dispose() {
    this.#disposed = true;
    this.#generation++;
    this.#abort?.abort();
    this.#listeners.clear();
  }
}
