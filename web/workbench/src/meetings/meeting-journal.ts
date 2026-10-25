import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "../manuscripts/workbench-client";

export const meetingJournalKey = "speechnexus.workbench.meetings.v1";
export type MeetingStorage = Pick<Storage, "getItem" | "setItem">;
export type MeetingCommand =
  | { kind: "create"; projectId: string; key: string; body: Contracts["MeetingCreateRequest"] }
  | { kind: "seal"; meetingId: string; key: string; body: Contracts["MeetingSealRequest"] }
  | { kind: "minutes"; meetingId: string; key: string; body: Contracts["MinutesCreateRequest"] };
export type MeetingRecord = {
  version: 1;
  forms: { name: string; openId: string };
  project: Contracts["Project"] | null;
  view: Contracts["TranscriptView"] | null;
  meeting: Contracts["Meeting"] | null;
  draft: { text: string; baseRevision: string } | null;
  pending: MeetingCommand | null;
};
export function emptyMeetingRecord(): MeetingRecord {
  return {
    version: 1,
    forms: { name: "", openId: "" },
    project: null,
    view: null,
    meeting: null,
    draft: null,
    pending: null,
  };
}
export class MeetingStorageError extends Error {
  constructor() {
    super("Meeting recovery unavailable");
  }
}
function check(valid: boolean): asserts valid {
  if (!valid) throw new MeetingStorageError();
}
function object(value: unknown): Record<string, unknown> {
  check(!!value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function fields(value: Record<string, unknown>, names: string[]) {
  check(Object.keys(value).length === names.length && names.every((n) => Object.hasOwn(value, n)));
}
function validate(input: unknown): MeetingRecord {
  const r = object(input);
  fields(r, ["version", "forms", "project", "view", "meeting", "draft", "pending"]);
  check(r.version === 1);
  const forms = object(r.forms);
  fields(forms, ["name", "openId"]);
  check(typeof forms.name === "string" && typeof forms.openId === "string");
  const project = r.project === null ? null : parse("Project", r.project);
  const view = r.view === null ? null : parse("TranscriptView", r.view);
  const meeting = r.meeting === null ? null : parse("Meeting", r.meeting);
  if (view) {
    check(
      project !== null &&
        view.transcript.project_id === project.id &&
        view.revision.transcript_id === view.transcript.id,
    );
    check(BigInt(view.revision.revision) <= BigInt(view.transcript.head_revision));
  }
  check((view === null) === (r.draft === null));
  const draft = r.draft === null ? null : object(r.draft);
  if (draft) {
    fields(draft, ["text", "baseRevision"]);
    check(typeof draft.text === "string" && draft.baseRevision === view!.revision.revision);
  }
  if (meeting)
    check(
      project !== null &&
        view !== null &&
        meeting.project_id === project.id &&
        meeting.transcript_id === view.transcript.id,
    );
  if (r.pending !== null) {
    const p = object(r.pending);
    const headers = { "Idempotency-Key": p.key };
    if (p.kind === "create") {
      fields(p, ["kind", "projectId", "key", "body"]);
      validateRequest("createMeeting", {
        headers,
        path: { project_id: p.projectId },
        body: p.body,
      });
      const body = parse("MeetingCreateRequest", p.body);
      check(
        project !== null &&
          view !== null &&
          meeting === null &&
          p.projectId === project.id &&
          body.transcript_id === view.transcript.id &&
          body.display_name === forms.name &&
          body.save_audio !== true,
      );
    } else if (p.kind === "seal") {
      fields(p, ["kind", "meetingId", "key", "body"]);
      validateRequest("sealMeeting", { headers, path: { meeting_id: p.meetingId }, body: p.body });
      const body = parse("MeetingSealRequest", p.body);
      check(
        meeting !== null &&
          draft !== null &&
          p.meetingId === meeting.id &&
          body.expected_transcript_revision === draft.baseRevision &&
          body.text === draft.text,
      );
    } else if (p.kind === "minutes") {
      fields(p, ["kind", "meetingId", "key", "body"]);
      validateRequest("createMeetingMinutes", {
        headers,
        path: { meeting_id: p.meetingId },
        body: p.body,
      });
      const body = parse("MinutesCreateRequest", p.body);
      check(
        meeting !== null &&
          view !== null &&
          draft !== null &&
          p.meetingId === meeting.id &&
          body.expected_source_revision === meeting.sealed_revision &&
          meeting.sealed_revision === view.revision.revision &&
          draft.text === view.revision.text &&
          typeof body.llm_model === "string",
      );
    } else throw new MeetingStorageError();
  }
  return structuredClone(r) as MeetingRecord;
}
function bounded(raw: string) {
  check(new TextEncoder().encode(raw).byteLength <= 8 * 1024 * 1024);
}
/** One tab, one writer. Never replace corrupt or concurrently changed recovery. */
export class MeetingJournal {
  #raw: string | null;
  #record: MeetingRecord;
  constructor(private readonly storage: MeetingStorage) {
    try {
      this.#raw = storage.getItem(meetingJournalKey);
      if (this.#raw !== null) bounded(this.#raw);
      this.#record =
        this.#raw === null ? emptyMeetingRecord() : validate(JSON.parse(this.#raw) as unknown);
    } catch {
      throw new MeetingStorageError();
    }
  }
  load(): MeetingRecord {
    return structuredClone(this.#record);
  }
  save(record: MeetingRecord): void {
    try {
      const next = validate(record);
      const raw = JSON.stringify(next);
      bounded(raw);
      check(this.storage.getItem(meetingJournalKey) === this.#raw);
      this.storage.setItem(meetingJournalKey, raw);
      this.#raw = raw;
      this.#record = next;
    } catch {
      throw new MeetingStorageError();
    }
  }
}
