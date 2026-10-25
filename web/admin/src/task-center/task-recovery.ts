/** Private, versioned metadata. Never a Job cache, user identity, or speech-content store. */
export interface TaskRecoveryRecord {
  version: 1;
  selectedId: string | null;
  cursor: string | null;
  cancellation: { jobId: string; key: string } | null;
}
export interface TaskRecoveryJournal {
  load(): TaskRecoveryRecord | null;
  save(record: TaskRecoveryRecord): void;
}
class RecoveryError extends Error {
  constructor(reason: "invalid" | "changed" | "unavailable") {
    super(`Admin recovery storage ${reason}`);
  }
}
const invalid = (): never => {
  throw new RecoveryError("invalid");
};
const object = (value: unknown, fields: string[]): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  if (Object.keys(row).length !== fields.length || fields.some((f) => !Object.hasOwn(row, f)))
    return invalid();
  return row;
};
// Private path/size bounds; generated parsers still own every public wire object.
const identifier = (value: unknown): string =>
  typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)
    ? value
    : invalid();
function validate(value: unknown): TaskRecoveryRecord {
  const row = object(value, ["version", "selectedId", "cursor", "cancellation"]);
  if (row.version !== 1) return invalid();
  const selectedId = row.selectedId === null ? null : identifier(row.selectedId);
  const cursor = row.cursor;
  if (
    cursor !== null &&
    (selectedId === null || typeof cursor !== "string" || !/^[1-9][0-9]{0,18}$/.test(cursor))
  )
    return invalid();
  let cancellation: TaskRecoveryRecord["cancellation"] = null;
  if (row.cancellation !== null) {
    const pending = object(row.cancellation, ["jobId", "key"]);
    if (
      typeof pending.key !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(pending.key)
    )
      return invalid();
    cancellation = Object.freeze({ jobId: identifier(pending.jobId), key: pending.key });
  }
  return Object.freeze({ version: 1, selectedId, cursor, cancellation });
}

/** Synchronous same-tab journal; compare-before-write detects known owner changes, not cross-process CAS. */
export class SessionTaskRecovery implements TaskRecoveryJournal {
  #raw: string | null | undefined;
  constructor(
    private readonly storage: Pick<Storage, "getItem" | "setItem">,
    private readonly key: string,
  ) {
    if (!key || key.length > 128) invalid();
  }
  #read() {
    try {
      return this.storage.getItem(this.key);
    } catch {
      throw new RecoveryError("unavailable");
    }
  }
  load(): TaskRecoveryRecord | null {
    const raw = this.#read();
    let record: TaskRecoveryRecord | null = null;
    if (raw !== null) {
      if (raw.length > 2048) return invalid();
      try {
        record = validate(JSON.parse(raw));
      } catch {
        return invalid();
      }
    }
    this.#raw = raw;
    return record;
  }
  save(value: TaskRecoveryRecord): void {
    if (this.#raw === undefined) this.load();
    if (this.#read() !== this.#raw) throw new RecoveryError("changed");
    const raw = JSON.stringify(validate(value));
    if (raw.length > 2048) invalid();
    try {
      this.storage.setItem(this.key, raw);
    } catch {
      throw new RecoveryError("unavailable");
    }
    this.#raw = raw;
  }
}

export const ADMIN_RECOVERY_KEY = "speechnexus.admin.task-recovery.v1";
/** Access is lazy so denied sessionStorage is handled by the owning session, not a render crash. */
export const browserTaskRecovery = (): TaskRecoveryJournal =>
  new SessionTaskRecovery(
    {
      getItem: (key) => window.sessionStorage.getItem(key),
      setItem: (key, value) => window.sessionStorage.setItem(key, value),
    },
    ADMIN_RECOVERY_KEY,
  );
