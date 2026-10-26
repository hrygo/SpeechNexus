import {
  acceptedProfile,
  validateProfileIntent,
  type ProfileRequest,
  type ProfileRevision,
} from "./profile-client";

type Intent = { version: 1; key: string; body: ProfileRequest };
export type ProfileRecord = Intent &
  (
    | { phase: "prepared" | "unknown" }
    | { phase: "accepted"; result: ProfileRevision }
    | { phase: "rejected"; code: string }
  );
export const PROFILE_JOURNAL_KEY = "speechnexus.admin.profile-draft.v1";
const invalid = (): never => {
  throw new Error("profile_storage_invalid");
};
function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function validate(value: unknown): ProfileRecord | null {
  if (value === null) return null;
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const row = value as Record<string, unknown>;
  const extra = row.phase === "accepted" ? ["result"] : row.phase === "rejected" ? ["code"] : [];
  const keys = ["version", "phase", "key", "body", ...extra];
  if (
    Object.keys(row).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(row, key)) ||
    row.version !== 1 ||
    !["prepared", "unknown", "accepted", "rejected"].includes(String(row.phase)) ||
    typeof row.key !== "string"
  )
    return invalid();
  const body = row.body as ProfileRequest;
  validateProfileIntent(body, row.key);
  if (row.phase === "accepted") acceptedProfile(body, row.result);
  if (
    row.phase === "rejected" &&
    (typeof row.code !== "string" || !/^[a-z][a-z0-9_]{2,63}$/.test(row.code))
  )
    return invalid();
  return freeze(value as ProfileRecord);
}

/** Same-tab ownership checks, not a cross-process CAS or a server outcome lookup. */
export class ProfileJournal {
  #raw: string | null | undefined;
  constructor(private readonly storage: Pick<Storage, "getItem" | "setItem">) {}
  #read() {
    return this.storage.getItem(PROFILE_JOURNAL_KEY);
  }
  load(): ProfileRecord | null {
    const raw = this.#read();
    if (raw !== null && new TextEncoder().encode(raw).byteLength > 1048576) return invalid();
    const value = validate(raw === null ? null : JSON.parse(raw));
    this.#raw = raw;
    return value;
  }
  save(value: ProfileRecord | null): ProfileRecord | null {
    if (this.#raw === undefined) this.load();
    const raw = JSON.stringify(validate(JSON.parse(JSON.stringify(value))));
    if (new TextEncoder().encode(raw).byteLength > 1048576) return invalid();
    if (this.#read() !== this.#raw) throw new Error("profile_storage_changed");
    this.storage.setItem(PROFILE_JOURNAL_KEY, raw);
    if (this.#read() !== raw) throw new Error("profile_storage_changed");
    this.#raw = raw;
    return validate(JSON.parse(raw));
  }
}

export const browserProfileJournal = () =>
  new ProfileJournal({
    getItem: (key) => window.sessionStorage.getItem(key),
    setItem: (key, value) => window.sessionStorage.setItem(key, value),
  });
