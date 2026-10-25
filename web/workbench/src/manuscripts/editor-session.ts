import { type EditorJournal, type EditorRecord, type PendingCommand } from "./editor-journal";
import {
  type WorkbenchClient,
  WorkbenchResponseError,
  parse,
  type Contracts,
} from "./workbench-client";

export type EditorProblem =
  | "unavailable"
  | "read_failed"
  | "unsaved"
  | "unknown"
  | "invalid"
  | "conflict"
  | "missing"
  | "storage"
  | "rejected";
export type EditorSnapshot = {
  record: EditorRecord;
  status: Contracts["WorkbenchStatus"] | null;
  projects: Contracts["ProjectList"] | null;
  transcripts: Contracts["TranscriptList"] | null;
  conflict: Contracts["TranscriptView"] | null;
  busy: boolean;
  blocked: boolean;
  verified: boolean;
  problem: EditorProblem | null;
  saved: boolean;
};

function freeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function belongs(view: Contracts["TranscriptView"], projectId: string) {
  if (view.transcript.project_id !== projectId) throw new Error("Workbench resource mismatch");
}
function merge<T extends { id: string }>(before: T[], after: T[]): T[] {
  const rows = new Map(before.map((value) => [value.id, value]));
  for (const row of after) rows.set(row.id, row);
  return [...rows.values()];
}

/** All mutations cross the durable original-intent barrier, only on explicit actions. */
export class EditorSession {
  #state: EditorSnapshot;
  #listeners = new Set<() => void>();
  #generation = 0;
  #abort: AbortController | null = null;
  #disposed = false;
  #started = false;

  constructor(
    private readonly client: WorkbenchClient,
    private readonly journal: EditorJournal,
  ) {
    const record = journal.load();
    this.#state = freeze({
      record,
      status: null,
      projects: null,
      transcripts: null,
      conflict: null,
      busy: false,
      blocked: false,
      verified: false,
      problem: record.pending ? "unknown" : null,
      saved: false,
    });
  }
  getSnapshot = () => this.#state;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #publish(change: Partial<EditorSnapshot>) {
    if (this.#disposed) return;
    this.#state = freeze({ ...this.#state, ...change });
    for (const listener of this.#listeners) listener();
  }
  #persist(record: EditorRecord, change: Partial<EditorSnapshot> = {}): boolean {
    if (this.#disposed) return false;
    try {
      this.journal.save(record);
      this.#publish({ ...change, record, blocked: false });
      return true;
    } catch {
      // Keep unsaved memory visible for copying; the durable prior record is untouched.
      this.#publish({ ...change, record, blocked: true, problem: "storage", saved: false });
      return false;
    }
  }
  hasUnsaved(): boolean {
    const { draft, view, forms } = this.#state.record;
    return (
      (!!draft && draft.text !== view?.revision.text) ||
      forms.newText !== "" ||
      forms.transcriptName !== ""
    );
  }
  #navigate(): boolean {
    if (this.#disposed || this.#state.blocked || this.#state.record.pending) return false;
    if (this.hasUnsaved()) {
      this.#publish({ problem: "unsaved" });
      return false;
    }
    return true;
  }
  #newCommand(): boolean {
    return (
      !this.#disposed &&
      !this.#state.busy &&
      !this.#state.blocked &&
      this.#state.record.pending === null &&
      this.#state.status?.workbench_enabled === true
    );
  }
  async #read(task: (signal: AbortSignal) => Promise<() => void>) {
    if (this.#disposed) return;
    this.#abort?.abort();
    const abort = new AbortController();
    this.#abort = abort;
    const generation = ++this.#generation;
    this.#publish({ busy: true, saved: false });
    try {
      const commit = await task(abort.signal);
      if (!this.#disposed && generation === this.#generation) commit();
    } catch (error) {
      if (!this.#disposed && generation === this.#generation)
        this.#publish({
          problem:
            error instanceof WorkbenchResponseError && error.status === 404
              ? "missing"
              : "read_failed",
        });
    } finally {
      if (!this.#disposed && generation === this.#generation) this.#publish({ busy: false });
    }
  }

  async start() {
    if (this.#started || this.#disposed) return;
    this.#started = true;
    const projectId = this.#state.record.project?.id;
    await this.discover();
    if (
      !this.#disposed &&
      !this.#state.busy &&
      !this.#state.record.pending &&
      this.#state.record.project?.id === projectId &&
      projectId !== undefined
    )
      await this.checkCurrent();
  }
  async discover() {
    if (this.#state.busy && this.#state.record.pending) return;
    await this.#read(async (signal) => {
      const status = await this.client.status(signal);
      const projects = status.workbench_enabled
        ? await this.client.projects(undefined, signal)
        : null;
      return () =>
        this.#publish({
          status,
          projects,
          problem: status.workbench_enabled
            ? this.#state.record.pending
              ? "unknown"
              : null
            : "unavailable",
        });
    });
  }
  async moreProjects() {
    const current = this.#state.projects;
    if (!current?.next_cursor || this.#state.record.pending || this.#state.busy) return;
    await this.#read(async (signal) => {
      const page = await this.client.projects(current.next_cursor!, signal);
      if (page.next_cursor === current.next_cursor) throw new Error("Repeated pagination cursor");
      return () => this.#publish({ projects: { ...page, data: merge(current.data, page.data) } });
    });
  }
  async moreTranscripts() {
    const current = this.#state.transcripts;
    const project = this.#state.record.project;
    if (!current?.next_cursor || !project || this.#state.record.pending || this.#state.busy) return;
    await this.#read(async (signal) => {
      const page = await this.client.transcripts(project.id, current.next_cursor!, signal);
      if (page.next_cursor === current.next_cursor) throw new Error("Repeated pagination cursor");
      return () =>
        this.#publish({ transcripts: { ...page, data: merge(current.data, page.data) } });
    });
  }
  async selectProject(id: string) {
    if (!this.#navigate()) return;
    await this.#read(async (signal) => {
      const [project, transcripts] = await Promise.all([
        this.client.project(id, signal),
        this.client.transcripts(id, undefined, signal),
      ]);
      return () => {
        const record = structuredClone(this.#state.record);
        record.project = project;
        record.view = null;
        record.draft = null;
        this.#persist(record, { transcripts, conflict: null, verified: false, problem: null });
      };
    });
  }
  async selectTranscript(id: string, revision?: string) {
    const project = this.#state.record.project;
    if (!project || !this.#navigate()) return;
    await this.#read(async (signal) => {
      const view = await this.client.view(id, revision, signal);
      belongs(view, project.id);
      return () => {
        const record = structuredClone(this.#state.record);
        record.view = view;
        record.draft = {
          text: view.revision.text,
          baseRevision: view.transcript.head_revision,
          sourceRevision: view.revision.revision,
        };
        this.#persist(record, { verified: true, conflict: null, problem: null });
      };
    });
  }
  async checkCurrent() {
    const record = structuredClone(this.#state.record);
    if (!record.project || this.#state.record.pending || this.#state.blocked) return;
    const projectId = record.project.id;
    this.#publish({ verified: false });
    await this.#read(async (signal) => {
      const [project, transcripts] = await Promise.all([
        this.client.project(projectId, signal),
        this.client.transcripts(projectId, undefined, signal),
      ]);
      const view = record.view
        ? await this.client.view(record.view.transcript.id, record.view.revision.revision, signal)
        : null;
      if (view) belongs(view, projectId);
      const advanced =
        view !== null && view.transcript.head_revision !== record.draft?.baseRevision;
      const remote = advanced
        ? await this.client.view(view.transcript.id, undefined, signal)
        : null;
      if (remote) belongs(remote, projectId);
      return () => {
        // Editing is disabled while this read is busy; the captured draft remains exact.
        record.project = project;
        if (view && !advanced) record.view = view;
        this.#persist(record, {
          transcripts,
          conflict: remote,
          verified: view !== null && !advanced,
          problem: advanced ? "conflict" : null,
        });
      };
    });
  }
  edit(text: string) {
    if (
      this.#disposed ||
      this.#state.busy ||
      this.#state.record.pending ||
      !this.#state.record.draft
    )
      return;
    const record = structuredClone(this.#state.record);
    record.draft!.text = text;
    this.#persist(record);
    this.#publish({ saved: false });
  }
  form(field: keyof EditorRecord["forms"], value: string) {
    if (this.#disposed || this.#state.busy || this.#state.record.pending) return;
    const record = structuredClone(this.#state.record);
    record.forms[field] = value;
    this.#persist(record);
    this.#publish({ saved: false });
  }
  retryStorage() {
    if (!this.#state.blocked || this.#state.busy) return;
    if (this.#persist(structuredClone(this.#state.record)))
      this.#publish({
        problem: this.#state.record.pending ? "unknown" : this.#state.conflict ? "conflict" : null,
      });
  }
  discard() {
    if (this.#state.record.pending || this.#state.busy || this.#state.blocked) return;
    const record = structuredClone(this.#state.record);
    record.view = null;
    record.draft = null;
    record.forms = { projectName: "", transcriptName: "", newText: "" };
    this.#persist(record, { conflict: null, verified: false, problem: null, saved: false });
  }
  rebase() {
    const remote = this.#state.conflict;
    if (
      !remote ||
      remote.transcript.id !== this.#state.record.view?.transcript.id ||
      remote.transcript.project_id !== this.#state.record.project?.id ||
      remote.revision.revision !== remote.transcript.head_revision ||
      !this.#state.record.draft ||
      this.#state.record.pending ||
      this.#state.busy ||
      this.#state.blocked
    )
      return;
    const record = structuredClone(this.#state.record);
    record.view = remote;
    record.draft!.baseRevision = remote.transcript.head_revision;
    this.#persist(record, { conflict: null, verified: true, problem: null });
  }
  async #submit(command: PendingCommand) {
    const record = structuredClone(this.#state.record);
    record.pending = command;
    if (this.#persist(record)) await this.#execute();
  }
  async createProject() {
    if (!this.#newCommand() || !this.#navigate()) return;
    try {
      const body = parse("ProjectCreateRequest", { name: this.#state.record.forms.projectName });
      await this.#submit({ kind: "project", key: crypto.randomUUID(), body });
    } catch {
      this.#publish({ problem: "invalid" });
    }
  }
  async createTranscript() {
    const project = this.#state.record.project;
    if (!project || !this.#newCommand()) return;
    if (
      this.#state.record.draft &&
      this.#state.record.draft.text !== this.#state.record.view?.revision.text
    ) {
      this.#publish({ problem: "unsaved" });
      return;
    }
    try {
      const body = parse("TranscriptCreateRequest", {
        display_name: this.#state.record.forms.transcriptName,
        source: { kind: "manual", text: this.#state.record.forms.newText },
      });
      await this.#submit({
        kind: "transcript",
        projectId: project.id,
        key: crypto.randomUUID(),
        body,
      });
    } catch {
      this.#publish({ problem: "invalid" });
    }
  }
  async save() {
    const { view, draft } = this.#state.record;
    if (!view || !draft || !this.#newCommand() || !this.#state.verified || this.#state.conflict)
      return;
    try {
      const body = parse("TranscriptRevisionCreateRequest", {
        expected_revision: draft.baseRevision,
        source_revision: draft.sourceRevision,
        text: draft.text,
      });
      await this.#submit({
        kind: "revise",
        transcriptId: view.transcript.id,
        key: crypto.randomUUID(),
        body,
      });
    } catch {
      this.#publish({ problem: "invalid" });
    }
  }
  async replay() {
    if (this.#disposed || this.#state.busy || this.#state.blocked || !this.#state.record.pending)
      return;
    await this.#execute();
  }
  async #execute() {
    const command = this.#state.record.pending;
    if (!command) return;
    this.#publish({ busy: true, problem: null, saved: false });
    try {
      const record = structuredClone(this.#state.record);
      let conflict: Contracts["TranscriptView"] | null = null;
      if (command.kind === "project") {
        const project = await this.client.createProject(command.body, command.key);
        record.project = project;
        record.view = null;
        record.draft = null;
        record.forms.projectName = "";
      } else if (command.kind === "transcript") {
        const view = await this.client.createTranscript(
          command.projectId,
          command.body,
          command.key,
        );
        belongs(view, command.projectId);
        record.view = view;
        record.draft = {
          text: view.revision.text,
          baseRevision: view.transcript.head_revision,
          sourceRevision: view.revision.revision,
        };
        record.forms.transcriptName = "";
        record.forms.newText = "";
      } else {
        const revision = await this.client.revise(command.transcriptId, command.body, command.key);
        if (this.#disposed) return;
        const view = await this.client.view(command.transcriptId, revision.revision);
        belongs(view, record.project!.id);
        if (view.revision.id !== revision.id || view.revision.text !== revision.text)
          throw new Error("Workbench receipt mismatch");
        record.view = view;
        record.draft = {
          text: revision.text,
          baseRevision: revision.revision,
          sourceRevision: revision.revision,
        };
        if (view.transcript.head_revision !== revision.revision) {
          conflict = await this.client.view(command.transcriptId);
          belongs(conflict, record.project!.id);
        }
      }
      if (this.#disposed) return;
      record.pending = null;
      this.#persist(record, {
        conflict,
        verified: record.view !== null && conflict === null,
        problem: conflict ? "conflict" : null,
        saved: true,
        projects:
          this.#state.projects && record.project
            ? {
                ...this.#state.projects,
                data: merge(
                  [record.project],
                  this.#state.projects.data.filter((p) => p.id !== record.project!.id),
                ),
              }
            : this.#state.projects,
        transcripts:
          command.kind === "project"
            ? null
            : this.#state.transcripts && record.view
              ? {
                  ...this.#state.transcripts,
                  data: merge(
                    [record.view.transcript],
                    this.#state.transcripts.data.filter((t) => t.id !== record.view!.transcript.id),
                  ),
                }
              : this.#state.transcripts,
      });
    } catch (error) {
      if (this.#disposed) return;
      if (
        error instanceof WorkbenchResponseError &&
        [400, 404, 409, 410, 413, 415, 422].includes(error.status)
      ) {
        const record = structuredClone(this.#state.record);
        record.pending = null;
        if (!this.#persist(record)) return;
        const conflict = command.kind === "revise" && error.code === "revision_conflict";
        this.#publish({
          problem: conflict ? "conflict" : error.status === 404 ? "missing" : "rejected",
          verified: error.status === 404 || conflict ? false : this.#state.verified,
        });
        if (conflict && command.kind === "revise") {
          try {
            const remote = await this.client.view(command.transcriptId);
            belongs(remote, record.project!.id);
            this.#publish({ conflict: remote });
          } catch {
            /* The original draft remains; explicit checkCurrent may try another read. */
          }
        }
      } else this.#publish({ problem: "unknown" });
    } finally {
      this.#publish({ busy: false });
    }
  }
  dispose() {
    this.#disposed = true;
    ++this.#generation;
    this.#abort?.abort();
    this.#listeners.clear();
  }
}
