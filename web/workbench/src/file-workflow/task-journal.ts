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

/** v2 fixes the asset and both revisioned selections; no text is stored locally. */
export function jobCommand(record: RecoveryRecord) {
  if (isUploadRecord(record)) throw new TaskJournalError("invalid");
  if (record.version === 1) return asrCommand(record);
  return parse("JobCreateRequest", {
    kind: "tts",
    model: record.model,
    workspace_id: record.workspaceId,
    input: { text_asset_id: record.assetId },
    voice: { id: record.voiceId, revision: record.voiceRevision },
    profile: { id: record.profileId, revision: record.profileRevision },
    output: { audio_format: "wav", save_artifacts: true },
  });
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
const fixed = ["version", "createKey", "cancelKey"] as const;

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
  if (
    Object.keys(row).length !== expected.length ||
    expected.some((field) => !Object.hasOwn(row, field))
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
  if (
    row.cursor !== null &&
    (typeof row.cursor !== "string" || !/^[1-9][0-9]{0,18}$/.test(row.cursor))
  )
    return invalid();
  try {
    jobCommand(row as unknown as TaskRecord);
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
          JSON.stringify(jobCommand(uploadJobRecord(beforeUpload))) !==
            JSON.stringify(jobCommand(record))
        )
          throw new TaskJournalError("changed");
      }
    } else if (
      before &&
      (afterUpload ||
        fixed.some((field) => before[field] !== record[field]) ||
        JSON.stringify(jobCommand(before)) !== JSON.stringify(jobCommand(record)) ||
        (before.jobId !== null && before.jobId !== record.jobId))
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
