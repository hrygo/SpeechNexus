import {
  acceptedProfile,
  validateProfileIntent,
  ProfileHttpError,
  type ProfileApi,
  type ProfileRequest,
  type ProfileRevision,
} from "./profile-client";
import type { ProfileJournal, ProfileRecord } from "./profile-journal";

interface ProfileSnapshot {
  record: ProfileRecord | null;
  revision: ProfileRevision | null;
  readCurrent: boolean;
  receiptCurrent: boolean;
  busy: "read" | "write" | null;
  blocked: boolean;
  active: boolean;
  problem: string | null;
}

/** A local timeout/hide/read never changes the authority of a retained write. */
export class ProfileSession {
  #state: ProfileSnapshot = {
    record: null,
    revision: null,
    readCurrent: false,
    receiptCurrent: false,
    busy: null,
    blocked: false,
    active: true,
    problem: null,
  };
  #listeners = new Set<() => void>();
  #epoch = 0;
  #controller: AbortController | null = null;
  #disposed = false;
  constructor(
    private readonly api: ProfileApi,
    private readonly journal: ProfileJournal,
    private readonly key: () => string = () => crypto.randomUUID(),
  ) {
    try {
      this.#state.record = journal.load();
      if (this.#state.record?.phase === "rejected") this.#state.problem = this.#state.record.code;
    } catch {
      this.#state.blocked = true;
      this.#state.problem = "profile_storage_unavailable";
    }
  }
  snapshot = () => this.#state;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #update(value: Partial<ProfileSnapshot>) {
    if (this.#disposed) return;
    this.#state = { ...this.#state, ...value };
    this.#listeners.forEach((listener) => listener());
  }
  #available() {
    return !this.#disposed && this.#state.active && !this.#state.busy && !this.#state.blocked;
  }
  #save(record: ProfileRecord | null): boolean {
    try {
      this.#update({ record: this.journal.save(record) });
      return true;
    } catch {
      this.#update({
        blocked: true,
        receiptCurrent: false,
        problem: "profile_storage_unavailable",
      });
      return false;
    }
  }
  prepare(body: ProfileRequest): boolean {
    if (!this.#available() || this.#state.record) return false;
    const key = this.key();
    try {
      validateProfileIntent(body, key);
    } catch {
      this.#update({ problem: "profile_input_invalid" });
      return false;
    }
    if (!this.#save({ version: 1, phase: "prepared", key, body })) return false;
    this.#update({ problem: null, receiptCurrent: false });
    return true;
  }
  async confirm(): Promise<void> {
    const record = this.#state.record;
    if (!this.#available() || !record || !["prepared", "unknown"].includes(record.phase)) return;
    const wasUnknown = record.phase === "unknown";
    // Once this durable state exists, a crash/reload must treat the send as unknown.
    if (!this.#save({ version: 1, phase: "unknown", key: record.key, body: record.body })) return;
    const current = this.#state.record!;
    const token = ++this.#epoch;
    const controller = (this.#controller = new AbortController());
    this.#update({ busy: "write", problem: null, receiptCurrent: false });
    try {
      const result = acceptedProfile(
        current.body,
        await this.api.create(current.body, current.key, controller.signal),
      );
      if (!this.#owns(token)) return;
      if (this.#save({ ...current, phase: "accepted", result }))
        this.#update({ receiptCurrent: true });
    } catch (error) {
      if (!this.#owns(token)) return;
      const conflict =
        error instanceof ProfileHttpError &&
        error.status === 409 &&
        ["configuration_changed", "idempotency_conflict"].includes(error.code);
      const refused =
        error instanceof ProfileHttpError &&
        !wasUnknown &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status);
      if (error instanceof ProfileHttpError && (conflict || refused)) {
        if (this.#save({ ...current, phase: "rejected", code: error.code }))
          this.#update({ problem: error.code });
      } else this.#update({ problem: "profile_outcome_unknown" });
    } finally {
      if (this.#owns(token)) {
        this.#controller = null;
        this.#update({ busy: null });
      }
    }
  }
  #owns(token: number) {
    return !this.#disposed && this.#state.active && token === this.#epoch;
  }
  async read(id: string, revision: string): Promise<void> {
    if (!this.#available()) return;
    const token = ++this.#epoch;
    const controller = (this.#controller = new AbortController());
    this.#update({ busy: "read", readCurrent: false, problem: null });
    try {
      const result = await this.api.read(id, revision, controller.signal);
      if (!this.#owns(token)) return;
      if (result.profile_id !== id || result.revision !== revision)
        throw new Error("profile_read_mismatch");
      this.#update({ revision: result, readCurrent: true });
    } catch (error) {
      if (this.#owns(token))
        this.#update({
          problem:
            error instanceof ProfileHttpError && error.status === 404
              ? "profile_not_found"
              : "profile_read_unavailable",
        });
    } finally {
      if (this.#owns(token)) {
        this.#controller = null;
        this.#update({ busy: null });
      }
    }
  }
  finish(): boolean {
    if (!this.#available() || !this.#state.record) return false;
    if (this.#state.record.phase === "unknown") {
      this.#update({ problem: "profile_outcome_unknown" });
      return false;
    }
    if (!this.#save(null)) return false;
    this.#update({ problem: null, receiptCurrent: false });
    return true;
  }
  setActive(active: boolean) {
    if (this.#disposed || active === this.#state.active) return;
    if (!active) {
      ++this.#epoch;
      this.#controller?.abort();
      this.#controller = null;
    }
    this.#update({ active, busy: null, readCurrent: false, receiptCurrent: false });
  }
  dispose() {
    this.setActive(false);
    this.#disposed = true;
    this.#listeners.clear();
  }
}
