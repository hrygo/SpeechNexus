import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "./workbench-client";

export const journalKey = "speechnexus.workbench.manuscripts.v1";
export type EditorStorage = Pick<Storage, "getItem" | "setItem">;
export type PendingCommand =
  | { kind: "project"; key: string; body: Contracts["ProjectCreateRequest"] }
  | {
      kind: "transcript";
      key: string;
      projectId: string;
      body: Contracts["TranscriptCreateRequest"];
    }
  | {
      kind: "revise";
      key: string;
      transcriptId: string;
      body: Contracts["TranscriptRevisionCreateRequest"];
    };
export type EditorRecord = {
  version: 1;
  forms: { projectName: string; transcriptName: string; newText: string };
  project: Contracts["Project"] | null;
  view: Contracts["TranscriptView"] | null;
  draft: { text: string; baseRevision: string; sourceRevision: string } | null;
  pending: PendingCommand | null;
};

export function emptyRecord(): EditorRecord {
  return {
    version: 1,
    forms: { projectName: "", transcriptName: "", newText: "" },
    project: null,
    view: null,
    draft: null,
    pending: null,
  };
}

export class EditorStorageError extends Error {
  constructor() {
    super("Editor recovery unavailable");
  }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new EditorStorageError();
  return value as Record<string, unknown>;
}
function check(valid: boolean): asserts valid {
  if (!valid) throw new EditorStorageError();
}
function fields(value: Record<string, unknown>, names: string[]) {
  check(
    Object.keys(value).length === names.length && names.every((name) => Object.hasOwn(value, name)),
  );
}

function validate(value: unknown): EditorRecord {
  const record = object(value);
  fields(record, ["version", "forms", "project", "view", "draft", "pending"]);
  check(record.version === 1);
  const forms = object(record.forms);
  fields(forms, ["projectName", "transcriptName", "newText"]);
  check(Object.values(forms).every((item) => typeof item === "string"));
  const project = record.project === null ? null : parse("Project", record.project);
  const view = record.view === null ? null : parse("TranscriptView", record.view);
  if (view !== null) {
    check(project !== null && view.transcript.project_id === project.id);
    check(view.revision.transcript_id === view.transcript.id);
    check(BigInt(view.revision.revision) <= BigInt(view.transcript.head_revision));
  }
  check((view === null) === (record.draft === null));
  const draft = record.draft === null ? null : object(record.draft);
  if (draft !== null) {
    fields(draft, ["text", "baseRevision", "sourceRevision"]);
    check(typeof draft.text === "string");
    parse("TranscriptRevisionCreateRequest", {
      text: "",
      expected_revision: draft.baseRevision,
      source_revision: draft.sourceRevision,
    });
    check(BigInt(draft.baseRevision as string) <= BigInt(view!.transcript.head_revision));
    check(BigInt(draft.sourceRevision as string) <= BigInt(draft.baseRevision as string));
  }
  if (record.pending !== null) {
    const pending = object(record.pending);
    const headers = { "Idempotency-Key": pending.key };
    if (pending.kind === "project") {
      fields(pending, ["kind", "key", "body"]);
      validateRequest("createProject", { headers, body: pending.body });
      check(parse("ProjectCreateRequest", pending.body).name === forms.projectName);
    } else if (pending.kind === "transcript") {
      fields(pending, ["kind", "key", "projectId", "body"]);
      validateRequest("createTranscript", {
        headers,
        path: { project_id: pending.projectId },
        body: pending.body,
      });
      const body = parse("TranscriptCreateRequest", pending.body);
      check(project !== null && project.id === pending.projectId);
      check(
        body.display_name === forms.transcriptName &&
          body.source.kind === "manual" &&
          body.source.text === forms.newText,
      );
    } else if (pending.kind === "revise") {
      fields(pending, ["kind", "key", "transcriptId", "body"]);
      validateRequest("createTranscriptRevision", {
        headers,
        path: { transcript_id: pending.transcriptId },
        body: pending.body,
      });
      const body = parse("TranscriptRevisionCreateRequest", pending.body);
      check(view !== null && draft !== null && pending.transcriptId === view.transcript.id);
      check(
        body.text === draft.text &&
          body.expected_revision === draft.baseRevision &&
          body.source_revision === draft.sourceRevision,
      );
    } else throw new EditorStorageError();
  }
  return structuredClone(record) as EditorRecord;
}

/** Compare-and-save protects another mounted writer; failures never clear prior recovery. */
export class EditorJournal {
  #raw: string | null;
  #record: EditorRecord;
  constructor(private readonly storage: EditorStorage) {
    try {
      this.#raw = storage.getItem(journalKey);
      if (this.#raw !== null && this.#raw.length > 8 * 1024 * 1024) throw new EditorStorageError();
      this.#record =
        this.#raw === null ? emptyRecord() : validate(JSON.parse(this.#raw) as unknown);
    } catch {
      throw new EditorStorageError();
    }
  }

  load(): EditorRecord {
    return structuredClone(this.#record);
  }

  save(record: EditorRecord): void {
    try {
      const next = validate(record);
      const raw = JSON.stringify(next);
      if (raw.length > 8 * 1024 * 1024 || this.storage.getItem(journalKey) !== this.#raw)
        throw new EditorStorageError();
      this.storage.setItem(journalKey, raw);
      this.#raw = raw;
      this.#record = next;
    } catch {
      throw new EditorStorageError();
    }
  }
}
