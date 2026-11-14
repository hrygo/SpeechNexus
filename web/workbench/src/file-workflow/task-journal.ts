import { parse, type FileContracts } from "./speech-file-client";
import {
  validUploadRecord,
  isUploadRecord,
  sameUploadIntent,
  uploadAssetFields,
  uploadJobRecord,
  type UploadRecord,
} from "./upload-record";
export { uploadJobRecord, isUploadRecord, uploadHint } from "./upload-record";
export type { UploadRecord } from "./upload-record";

/** Versioned local intent, never an identity or a cache of speech content. */
interface TaskIdentity {
  readonly model: string;
  readonly assetId: string;
  readonly workspaceId: string;
  readonly createKey: string;
  readonly cancelKey: string;
  readonly jobId: string | null;
  readonly pending: "create" | "cancel" | null;
  readonly operationId: string | null;
  readonly cursor: string | null;
  /**
   * The protocol definitively refused this attempt, so the durable intent is spent: it
   * can only be abandoned explicitly, never replayed. `pending` keeps its original
   * "a request may be in flight, so its outcome is unknown" meaning and is never
   * repurposed for the refusal; the field is written only after the refusal was
   * observed, so the crash window before any answer still reloads as unknown (#707).
   *
   * One verdict covers both submission classes this client sends: a create attempt the
   * protocol refused without producing a Job of its own (#707), and a cancel attempt it
   * refused against the known Job the attempt names (#723). Both are refusals the
   * protocol will not accept on replay, so neither may be replayed or replaced silently.
   * `validRecord` keeps the field on a still-pending intent and off any identified
   * execution; it does not spell out the class shapes the record's own invariants pin.
   *
   * A journal from before #707 counts keys strictly, so it rejects a record carrying
   * this field as `invalid` instead of misreading it; the Host then reports unavailable
   * recovery and keeps the record (see FileTaskHost). That is a safe failure, not
   * backward compatibility.
   */
  readonly settled?: "terminal";
}

export interface TtsSelection {
  model: string;
  voiceId: string;
  voiceRevision: string;
  profileId: string;
  profileRevision: string;
}

interface AsrTaskRecord extends TaskIdentity {
  readonly version: 1;
}
interface TtsTaskRecord extends TaskIdentity, Readonly<TtsSelection> {
  readonly version: 2;
  /** Additive confirmation metadata; it never changes the v2 request body. */
  readonly effectiveModelKey?: string;
}
export type TaskRecord = AsrTaskRecord | TtsTaskRecord;
export type RecoveryRecord = TaskRecord | UploadRecord;

export interface FileTaskJournal {
  load(): RecoveryRecord | null;
  save(record: RecoveryRecord): void;
}

export class TaskJournalError extends Error {
  constructor(reason: "invalid" | "changed" | "unavailable") {
    super(`task recovery storage ${reason}`);
  }
}

/** v1 fixes all output options; changing this body requires a new record version. */
export function asrCommand(intent: Pick<TaskRecord, "model" | "assetId" | "workspaceId">) {
  return parse("JobCreateRequest", {
    kind: "asr",
    workspace_id: intent.workspaceId,
    model: intent.model,
    input: { audio_asset_id: intent.assetId },
    output: { transcript_format: "json", save_artifacts: true },
  }) satisfies FileContracts["JobCreateRequest"];
}

/** v2 fixes both revisioned selections and the uploaded text asset; the body is not stored. */
function ttsCommand(record: TtsTaskRecord, text: string) {
  return parse("JobCreateRequest", {
    kind: "tts",
    model: record.model,
    workspace_id: record.workspaceId,
    // The contract accepts either inline `text` or (not yet materialized) `text_asset_id`;
    // `job_text_asset_unsupported` rejects the latter until the frozen text asset exists.
    input: { text },
    voice: { id: record.voiceId, revision: record.voiceRevision },
    profile: { id: record.profileId, revision: record.profileRevision },
    output: { audio_format: "wav", save_artifacts: true },
  });
}

/**
 * Shape-only body for validating and comparing a durable record. Submission requires the
 * real text; this placeholder is parsed but never sent, logged or persisted.
 */
const shapeText = "\u0000";

/** One durable intent's canonical body; v2 requires the text used only at submission. */
export function jobCommand(record: RecoveryRecord, text?: string) {
  if (isUploadRecord(record)) throw new TaskJournalError("invalid");
  if (record.version === 1) return asrCommand(record);
  if (text === undefined) throw new TaskJournalError("invalid");
  return ttsCommand(record, text);
}

/** Private write barrier: what a record fixes, independent of text it never stores. */
function commandShape(record: RecoveryRecord) {
  if (isUploadRecord(record)) throw new TaskJournalError("invalid");
  return record.version === 1 ? asrCommand(record) : ttsCommand(record, shapeText);
}

const fields = [
  "version",
  "model",
  "assetId",
  "workspaceId",
  "createKey",
  "cancelKey",
  "jobId",
  "pending",
  "operationId",
  "cursor",
] as const;
const ttsFields = ["voiceId", "voiceRevision", "profileId", "profileRevision"] as const;
// Fields a durable intent never changes; v2 pins the uploaded text asset that supplies its body.
const fixed = ["version", "createKey", "cancelKey", "assetId"] as const;

function validRecord(value: unknown): RecoveryRecord {
  const invalid = () => {
    throw new TaskJournalError("invalid");
  };
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if ([3, 4, 5, 6].includes(Number(row.version))) {
    try {
      const upload = validUploadRecord(row);
      // Placeholder IDs only validate unsubmitted selection fields; they are never stored or sent.
      validRecord(
        uploadJobRecord({
          ...upload,
          assetId: upload.assetId ?? "journal-asset",
          workspaceId: upload.workspaceId ?? "journal-workspace",
        }),
      );
      return upload;
    } catch {
      return invalid();
    }
  }
  if (row.version !== 1 && row.version !== 2) return invalid();
  const expected = row.version === 1 ? fields : [...fields, ...ttsFields];
  const hasModelKey = row.version === 2 && Object.hasOwn(row, "effectiveModelKey");
  const hasSettled = Object.hasOwn(row, "settled");
  if (
    Object.keys(row).length !== expected.length + Number(hasModelKey) + Number(hasSettled) ||
    expected.some((field) => !Object.hasOwn(row, field))
  )
    return invalid();
  if (
    hasModelKey &&
    (row.jobId === null ||
      typeof row.effectiveModelKey !== "string" ||
      row.effectiveModelKey.length < 1 ||
      row.effectiveModelKey.length > 256)
  )
    return invalid();
  for (const field of ["model", "assetId", "workspaceId", "createKey", "cancelKey"])
    if (typeof row[field] !== "string" || !row[field].length || row[field].length > 128)
      return invalid();
  for (const field of ["createKey", "cancelKey"])
    if (
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
        String(row[field]),
      )
    )
      return invalid();
  // Private journal bounds/path safety. Public wire validation stays in A's parser.
  for (const field of ["jobId", "operationId"])
    if (
      row[field] !== null &&
      (typeof row[field] !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(row[field]))
    )
      return invalid();
  if (row.pending !== null && row.pending !== "create" && row.pending !== "cancel")
    return invalid();
  if (row.pending === "cancel" && row.jobId === null) return invalid();
  if (row.pending === "create" && row.jobId !== null) return invalid();
  if (row.jobId === null && (row.pending !== "create" || row.cursor !== null)) return invalid();
  if (row.pending === null && row.operationId !== null) return invalid();
  if (hasSettled) {
    // The verdict rides on an intent that is still pending: the invariants above already
    // pin such a record to `pending: "create"` with no Job and no cursor, or to
    // `pending: "cancel"` with the Job that refusal named, so this field adds no shape of
    // its own and must not restate them. Its own bounds are the single value, a pending
    // (never confirmed) intent, and no identified execution: an attempt that named one
    // stays queryable and is never settled (#707 review M2). A confirmed cancel cursor is
    // not part of the verdict, because events keep advancing afterwards.
    if (row.settled !== "terminal" || row.pending === null || row.operationId !== null)
      return invalid();
  }
  if (
    row.cursor !== null &&
    (typeof row.cursor !== "string" || !/^[1-9][0-9]{0,18}$/.test(row.cursor))
  )
    return invalid();
  try {
    commandShape(row as unknown as TaskRecord);
  } catch {
    return invalid();
  }
  return Object.freeze({ ...row }) as unknown as TaskRecord;
}

/** One tab/intent. Host injects sessionStorage; library never chooses global storage. */
export class SessionTaskJournal implements FileTaskJournal {
  #raw: string | null | undefined;
  #record: RecoveryRecord | null = null;
  constructor(
    private readonly storage: Pick<Storage, "getItem" | "setItem">,
    private readonly key: string,
  ) {
    if (!key || key.length > 128) throw new TaskJournalError("invalid");
  }

  #read() {
    try {
      return this.storage.getItem(this.key);
    } catch {
      throw new TaskJournalError("unavailable");
    }
  }

  load() {
    const raw = this.#read();
    let record: RecoveryRecord | null = null;
    if (raw !== null) {
      if (raw.length > 8192) throw new TaskJournalError("invalid");
      try {
        record = validRecord(JSON.parse(raw));
      } catch {
        throw new TaskJournalError("invalid");
      }
    }
    this.#raw = raw;
    this.#record = record;
    return record;
  }

  save(value: RecoveryRecord) {
    if (this.#raw === undefined) this.load();
    if (this.#read() !== this.#raw) throw new TaskJournalError("changed");
    const record = validRecord(value);
    const before = this.#record;
    const beforeUpload = isUploadRecord(before) ? before : null;
    const afterUpload = isUploadRecord(record) ? record : null;
    if (beforeUpload) {
      if (afterUpload) {
        if (
          !sameUploadIntent(beforeUpload, afterUpload) ||
          (beforeUpload.pending === "validation" &&
            uploadAssetFields.some((field) => beforeUpload[field] !== afterUpload[field]))
        )
          throw new TaskJournalError("changed");
      } else {
        if (
          beforeUpload.pending !== "validation" ||
          record.pending !== "create" ||
          record.jobId !== null ||
          record.operationId !== null ||
          record.cursor !== null ||
          record.createKey !== beforeUpload.createKey ||
          record.cancelKey !== beforeUpload.cancelKey ||
          // The create record must reference the acknowledged upload's own text asset.
          record.assetId !== beforeUpload.assetId ||
          JSON.stringify(commandShape(uploadJobRecord(beforeUpload))) !==
            JSON.stringify(commandShape(record))
        )
          throw new TaskJournalError("changed");
      }
    } else if (
      before &&
      (afterUpload ||
        fixed.some((field) => before[field] !== record[field]) ||
        JSON.stringify(commandShape(before)) !== JSON.stringify(commandShape(record)) ||
        (before.jobId !== null && before.jobId !== record.jobId) ||
        (before.version === 2 &&
          before.effectiveModelKey !== undefined &&
          (record.version !== 2 || record.effectiveModelKey !== before.effectiveModelKey)) ||
        // A settled intent cannot be un-settled: losing the field would make the next
        // reload rebuild it as an unconfirmed submission, which is the defect #707 fixes.
        // It also cannot be re-pointed: the verdict names the submission class that was
        // refused and, for a cancel, the Job that refusal named (#723).
        (before.settled === "terminal" &&
          (record.settled !== "terminal" ||
            record.pending !== before.pending ||
            record.jobId !== before.jobId)))
    )
      throw new TaskJournalError("changed");
    const raw = JSON.stringify(record);
    if (raw.length > 8192) throw new TaskJournalError("invalid");
    try {
      this.storage.setItem(this.key, raw);
    } catch {
      throw new TaskJournalError("unavailable");
    }
    this.#raw = raw;
    this.#record = record;
  }
}
