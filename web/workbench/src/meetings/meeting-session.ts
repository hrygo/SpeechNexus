import { type MeetingJournal, type MeetingRecord, type MeetingCommand } from "./meeting-journal";
import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import {
  type WorkbenchClient,
  WorkbenchResponseError,
  parse,
  type Contracts,
} from "../manuscripts/workbench-client";

export type MeetingProblem =
  | "unavailable"
  | "read_failed"
  | "unsaved"
  | "unknown"
  | "invalid"
  | "conflict"
  | "missing"
  | "storage"
  | "rejected"
  | "profile";
export type MeetingSnapshot = {
  record: MeetingRecord;
  status: Contracts["WorkbenchStatus"] | null;
  projects: Contracts["ProjectList"] | null;
  transcripts: Contracts["TranscriptList"] | null;
  minutes: Contracts["MinutesJob"] | null;
  conflict: Contracts["TranscriptView"] | null;
  busy: boolean;
  blocked: boolean;
  verified: boolean;
  problem: MeetingProblem | null;
  saved: boolean;
};
function check(valid: boolean): asserts valid {
  if (!valid) throw Error("Meeting resource mismatch");
}
function freeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function merge<T extends { id: string }>(before: T[], after: T[]): T[] {
  return [...new Map([...before, ...after].map((x) => [x.id, x])).values()];
}
function owns(view: Contracts["TranscriptView"], projectId: string, id: string) {
  check(
    view.transcript.project_id === projectId &&
      view.transcript.id === id &&
      view.revision.transcript_id === id &&
      BigInt(view.revision.revision) <= BigInt(view.transcript.head_revision),
  );
}

/** Explicit commands cross the durable intent barrier before any network mutation. */
export class MeetingSession {
  #state: MeetingSnapshot;
  #listeners = new Set<() => void>();
  #disposed = false;
  #started = false;
  #generation = 0;
  #abort: AbortController | null = null;
  constructor(
    private readonly client: WorkbenchClient,
    private readonly journal: MeetingJournal,
  ) {
    const record = journal.load();
    this.#state = freeze({
      record,
      status: null,
      projects: null,
      transcripts: null,
      minutes: null,
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
  #publish(change: Partial<MeetingSnapshot>) {
    if (this.#disposed) return;
    this.#state = freeze({ ...this.#state, ...change });
    for (const listener of this.#listeners) listener();
  }
  #persist(record: MeetingRecord, change: Partial<MeetingSnapshot> = {}): boolean {
    if (this.#disposed) return false;
    try {
      this.journal.save(record);
      this.#publish({ ...change, record, blocked: false });
      return true;
    } catch {
      this.#publish({ ...change, record, blocked: true, problem: "storage", saved: false });
      return false;
    }
  }
  #editable() {
    return !this.#disposed && !this.#state.busy && !this.#state.record.pending;
  }
  #newCommand() {
    return (
      this.#editable() &&
      !this.#state.blocked &&
      this.#state.status?.workbench_enabled === true &&
      this.#state.verified
    );
  }
  hasUnsaved() {
    const { draft, view } = this.#state.record;
    return !!draft && draft.text !== view?.revision.text;
  }
  #navigate() {
    if (!this.#editable() || this.#state.blocked) return false;
    if (this.hasUnsaved()) {
      this.#publish({ problem: "unsaved" });
      return false;
    }
    return true;
  }
  async #read(task: (signal: AbortSignal) => Promise<() => void>) {
    if (this.#disposed || (this.#state.busy && this.#state.record.pending)) return;
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
          verified: false,
          minutes: null,
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
    await this.discover();
    if (!this.#state.record.pending && this.#state.record.view) await this.refresh();
  }
  async discover() {
    await this.#read(async (signal) => {
      const status = await this.client.status(signal);
      const projects = status.workbench_enabled
        ? await this.client.projects(undefined, signal)
        : null;
      return () =>
        this.#publish({
          status,
          projects,
          problem: this.#state.record.pending
            ? "unknown"
            : status.workbench_enabled
              ? null
              : "unavailable",
        });
    });
  }
  async moreProjects() {
    const p = this.#state.projects;
    if (!p?.next_cursor || !this.#editable()) return;
    await this.#read(async (signal) => {
      const next = await this.client.projects(p.next_cursor!, signal);
      check(next.next_cursor !== p.next_cursor);
      return () => this.#publish({ projects: { ...next, data: merge(p.data, next.data) } });
    });
  }
  async moreTranscripts() {
    const p = this.#state.transcripts;
    const project = this.#state.record.project;
    if (!p?.next_cursor || !project || !this.#editable()) return;
    await this.#read(async (signal) => {
      const next = await this.client.transcripts(project.id, p.next_cursor!, signal);
      check(
        next.next_cursor !== p.next_cursor && next.data.every((v) => v.project_id === project.id),
      );
      return () => this.#publish({ transcripts: { ...next, data: merge(p.data, next.data) } });
    });
  }
  async selectProject(id: string) {
    if (!this.#navigate()) return;
    await this.#read(async (signal) => {
      const [project, transcripts] = await Promise.all([
        this.client.project(id, signal),
        this.client.transcripts(id, undefined, signal),
      ]);
      check(project.id === id && transcripts.data.every((v) => v.project_id === id));
      return () => {
        const record = structuredClone(this.#state.record);
        Object.assign(record, { project, view: null, meeting: null, draft: null });
        this.#persist(record, {
          transcripts,
          minutes: null,
          conflict: null,
          verified: false,
          problem: null,
        });
      };
    });
  }
  async selectTranscript(id: string) {
    if (!this.#navigate()) return;
    const project = this.#state.record.project;
    if (!project) return;
    await this.#read(async (signal) => {
      const view = await this.client.view(id, undefined, signal);
      owns(view, project.id, id);
      check(view.revision.revision === view.transcript.head_revision);
      return () => {
        const record = structuredClone(this.#state.record);
        Object.assign(record, {
          view,
          meeting: null,
          draft: { text: view.revision.text, baseRevision: view.revision.revision },
        });
        this.#persist(record, {
          minutes: null,
          conflict: null,
          verified: true,
          problem: null,
          saved: false,
        });
      };
    });
  }
  setForm(field: "name" | "openId", text: string) {
    if (!this.#editable()) return;
    const record = structuredClone(this.#state.record);
    record.forms[field] = text;
    this.#persist(record, { problem: null, saved: false });
  }
  edit(text: string) {
    if (!this.#editable() || !this.#state.record.draft) return;
    const record = structuredClone(this.#state.record);
    record.draft!.text = text;
    this.#persist(record, { problem: null, saved: false });
  }
  discardDraft() {
    if (!this.#editable() || !this.#state.record.draft) return;
    const record = structuredClone(this.#state.record);
    record.draft!.text = record.view!.revision.text;
    this.#persist(record, { problem: null, saved: false });
  }
  retryStorage() {
    if (this.#disposed || this.#state.busy) return;
    this.#persist(structuredClone(this.#state.record), {
      problem: this.#state.record.pending ? "unknown" : null,
    });
  }
  dismissRejected() {
    if (!this.#editable() && this.#state.problem !== "rejected") return;
    if (this.#state.problem !== "rejected" || this.#state.busy || this.#state.blocked) return;
    const record = structuredClone(this.#state.record);
    record.pending = null;
    this.#persist(record, { problem: null, verified: false, saved: false });
  }
  useLatest() {
    if (!this.#editable() || this.#state.blocked || !this.#state.conflict) return;
    const record = structuredClone(this.#state.record);
    const view = this.#state.conflict;
    record.view = structuredClone(view);
    record.draft = { text: record.draft!.text, baseRevision: view.revision.revision };
    this.#persist(record, { conflict: null, verified: true, problem: null, saved: false });
  }
  async #facts(id: string, signal?: AbortSignal) {
    const meeting = await this.client.getMeeting(id, signal);
    check(meeting.id === id);
    const project = await this.client.project(meeting.project_id, signal);
    check(project.id === meeting.project_id);
    const latest = await this.client.view(meeting.transcript_id, undefined, signal);
    owns(latest, project.id, meeting.transcript_id);
    check(latest.revision.revision === latest.transcript.head_revision);
    const selected =
      meeting.sealed_revision === null || meeting.sealed_revision === latest.revision.revision
        ? latest
        : await this.client.view(meeting.transcript_id, meeting.sealed_revision, signal);
    owns(selected, project.id, meeting.transcript_id);
    check(selected.revision.revision === (meeting.sealed_revision ?? latest.revision.revision));
    const minutes = meeting.minutes_id
      ? await this.client.getMinutes(id, meeting.minutes_id, signal)
      : null;
    if (minutes)
      check(
        minutes.meeting_id === id &&
          minutes.id === meeting.minutes_id &&
          minutes.source_revision === meeting.sealed_revision,
      );
    return { meeting, project, latest, selected, minutes };
  }
  async open() {
    if (!this.#navigate()) return;
    const id = this.#state.record.forms.openId;
    try {
      validateRequest("getMeeting", { path: { meeting_id: id } });
    } catch {
      this.#publish({ problem: "invalid" });
      return;
    }
    await this.#read(async (signal) => {
      const facts = await this.#facts(id, signal);
      return () => {
        const record = structuredClone(this.#state.record);
        record.project = facts.project;
        record.meeting = facts.meeting;
        record.view = facts.selected;
        record.draft = {
          text: facts.selected.revision.text,
          baseRevision: facts.selected.revision.revision,
        };
        record.forms.name = facts.meeting.display_name;
        const conflict =
          facts.latest.revision.revision !== record.draft.baseRevision ? facts.latest : null;
        this.#persist(record, {
          transcripts: null,
          minutes: facts.minutes,
          conflict,
          verified: true,
          problem: conflict ? "conflict" : null,
        });
      };
    });
  }
  async refresh() {
    if (!this.#editable() || !this.#state.record.view) return;
    this.#publish({ verified: false });
    const before = this.#state.record;
    await this.#read(async (signal) => {
      if (before.meeting) {
        const facts = await this.#facts(before.meeting.id, signal);
        check(
          facts.meeting.project_id === before.project!.id &&
            facts.meeting.transcript_id === before.view!.transcript.id,
        );
        return () => {
          const record = structuredClone(this.#state.record);
          record.meeting = facts.meeting;
          record.project = facts.project;
          record.view!.transcript = facts.latest.transcript;
          const conflict =
            facts.latest.revision.revision !== record.draft!.baseRevision ? facts.latest : null;
          this.#persist(record, {
            minutes: facts.minutes,
            conflict,
            verified: true,
            problem: conflict ? "conflict" : null,
          });
        };
      }
      const view = await this.client.view(before.view!.transcript.id, undefined, signal);
      owns(view, before.project!.id, before.view!.transcript.id);
      return () => {
        const record = structuredClone(this.#state.record);
        record.view!.transcript = view.transcript;
        const conflict = view.revision.revision !== record.draft!.baseRevision ? view : null;
        this.#persist(record, { conflict, verified: true, problem: conflict ? "conflict" : null });
      };
    });
  }
  async create() {
    if (!this.#newCommand()) return;
    const r = this.#state.record;
    if (!r.project || !r.view || r.meeting) return;
    try {
      const body = parse("MeetingCreateRequest", {
        display_name: r.forms.name,
        transcript_id: r.view.transcript.id,
      });
      await this.#command({
        kind: "create",
        projectId: r.project.id,
        key: crypto.randomUUID(),
        body,
      });
    } catch {
      this.#publish({ problem: "invalid" });
    }
  }
  async seal() {
    if (!this.#newCommand() || this.#state.conflict) return;
    const r = this.#state.record;
    if (!r.meeting || !r.draft) return;
    try {
      const body = parse("MeetingSealRequest", {
        expected_transcript_revision: r.draft.baseRevision,
        text: r.draft.text,
      });
      await this.#command({
        kind: "seal",
        meetingId: r.meeting.id,
        key: crypto.randomUUID(),
        body,
      });
    } catch {
      this.#publish({ problem: "invalid" });
    }
  }
  async createMinutes() {
    if (!this.#newCommand()) return;
    const r = this.#state.record;
    const profile = this.#state.status?.minutes_profile;
    if (!this.#state.status?.llm_enabled || !profile) {
      this.#publish({ problem: "profile" });
      return;
    }
    if (
      !r.meeting ||
      !r.view ||
      r.meeting.sealed_revision !== r.view.revision.revision ||
      this.hasUnsaved() ||
      this.#state.conflict
    ) {
      this.#publish({ problem: "conflict" });
      return;
    }
    if (this.#state.minutes && ["queued", "running"].includes(this.#state.minutes.status)) return;
    const body = parse("MinutesCreateRequest", {
      template_revision: profile.template_revision,
      llm_model: profile.llm_model,
      expected_source_revision: r.meeting.sealed_revision,
    });
    await this.#command({
      kind: "minutes",
      meetingId: r.meeting.id,
      key: crypto.randomUUID(),
      body,
    });
  }
  async #command(command: MeetingCommand) {
    const record = structuredClone(this.#state.record);
    record.pending = command;
    if (this.#persist(record, { problem: "unknown", saved: false })) await this.replay();
  }
  async replay() {
    const command = this.#state.record.pending;
    if (this.#disposed || !command || this.#state.busy || this.#state.blocked) return;
    this.#abort?.abort();
    ++this.#generation;
    this.#publish({ busy: true, saved: false, problem: "unknown", minutes: null });
    const record = structuredClone(this.#state.record);
    let accepted = false;
    try {
      let id: string;
      if (command.kind === "create") {
        const result = await this.client.createMeeting(
          command.projectId,
          command.body,
          command.key,
        );
        check(
          result.project_id === record.project!.id &&
            result.transcript_id === record.view!.transcript.id,
        );
        id = result.id;
        accepted = true;
      } else if (command.kind === "seal") {
        const result = await this.client.sealMeeting(command.meetingId, command.body, command.key);
        check(
          result.id === command.meetingId &&
            result.project_id === record.project!.id &&
            result.transcript_id === record.view!.transcript.id,
        );
        id = result.id;
        accepted = true;
        const view = await this.client.view(result.transcript_id, result.sealed_revision!);
        owns(view, result.project_id, result.transcript_id);
        check(view.revision.revision === result.sealed_revision);
        record.view = view;
        record.draft = { text: view.revision.text, baseRevision: view.revision.revision };
      } else {
        const result = await this.client.createMinutes(
          command.meetingId,
          command.body,
          command.key,
        );
        check(
          result.meeting_id === command.meetingId &&
            result.source_revision === command.body.expected_source_revision &&
            result.template_revision === command.body.template_revision &&
            result.llm_model === command.body.llm_model,
        );
        id = command.meetingId;
        accepted = true;
      }
      if (this.#disposed) return;
      const facts = await this.#facts(id);
      check(
        facts.meeting.project_id === record.project!.id &&
          facts.meeting.transcript_id === record.view!.transcript.id,
      );
      record.meeting = facts.meeting;
      record.project = facts.project;
      record.view!.transcript = facts.latest.transcript;
      record.pending = null;
      const conflict =
        facts.latest.revision.revision !== record.draft!.baseRevision ? facts.latest : null;
      this.#persist(record, {
        minutes: facts.minutes,
        conflict,
        verified: true,
        problem: conflict ? "conflict" : null,
        saved: true,
      });
    } catch (error) {
      const rejected =
        !accepted &&
        error instanceof WorkbenchResponseError &&
        ((error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) ||
          error.code === "llm_disabled");
      this.#publish({ verified: false, problem: rejected ? "rejected" : "unknown" });
    } finally {
      this.#publish({ busy: false });
    }
  }
  dispose() {
    this.#disposed = true;
    this.#abort?.abort();
    ++this.#generation;
    this.#listeners.clear();
  }
}
