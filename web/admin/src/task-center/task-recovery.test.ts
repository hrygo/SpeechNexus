// @vitest-environment node
import { describe, expect, it } from "vitest";
import { SessionTaskRecovery, type TaskRecoveryRecord } from "./task-recovery";

const key = "admin-recovery";
const record = (): TaskRecoveryRecord => ({
  version: 1,
  selectedId: "job-new",
  cursor: "9007199254740993",
  cancellation: { jobId: "job-original", key: "9f712fc4-943f-4171-b910-9132c44927a2" },
});
function fixture(raw: string | null = null) {
  let value = raw;
  let denied = false;
  const storage = {
    getItem: () => {
      if (denied) throw new Error("synthetic-sensitive-storage");
      return value;
    },
    setItem: (_key: string, next: string) => {
      if (denied) throw new Error("synthetic-sensitive-storage");
      value = next;
    },
  };
  return {
    storage,
    journal: new SessionTaskRecovery(storage, key),
    raw: () => value,
    replace: (next: string) => {
      value = next;
    },
    deny: () => {
      denied = true;
    },
  };
}

describe("Admin metadata recovery journal", () => {
  it("round trips only bounded metadata and freezes loaded state", () => {
    const f = fixture();
    expect(f.journal.load()).toBeNull();
    f.journal.save(record());
    const restored = new SessionTaskRecovery(f.storage, key).load();
    expect(restored).toEqual(record());
    expect(Object.isFrozen(restored)).toBe(true);
    expect(Object.isFrozen(restored?.cancellation)).toBe(true);
    expect(f.raw()?.length).toBeLessThan(2048);
  });

  it.each([
    { ...record(), version: 2 },
    { ...record(), text: "synthetic-content" },
    { ...record(), selectedId: "../job" },
    { ...record(), selectedId: "x".repeat(129) },
    { ...record(), cursor: "01" },
    { ...record(), cursor: 9007199254740992 },
    { ...record(), cursor: "1".repeat(20) },
    { ...record(), selectedId: null },
    { ...record(), cancellation: { ...record().cancellation, key: "bad" } },
    { ...record(), cancellation: { ...record().cancellation, content: "synthetic-content" } },
  ])("rejects invalid local record %j without changing storage", (row) => {
    const original = JSON.stringify(row);
    const f = fixture(original);
    expect(() => f.journal.load()).toThrow("invalid");
    expect(() => f.journal.save(record())).toThrow("invalid");
    expect(f.raw()).toBe(original);
  });

  it("rejects malformed/oversized data without clearing it", () => {
    for (const raw of ["not-json", " ".repeat(2049), "[]", "null"]) {
      const f = fixture(raw);
      expect(() => f.journal.load()).toThrow("invalid");
      expect(f.raw()).toBe(raw);
    }
  });

  it("does not mutate the caller's nested record", () => {
    const f = fixture();
    const row = record();
    f.journal.save(row);
    row.cancellation!.jobId = "job-changed-by-caller";
    expect(f.journal.load()?.cancellation?.jobId).toBe("job-original");
  });

  it("refuses to overwrite a record changed by another owner", () => {
    const f = fixture();
    f.journal.load();
    const other = JSON.stringify(record());
    f.replace(other);
    expect(() => f.journal.save({ ...record(), cursor: null })).toThrow("changed");
    expect(f.raw()).toBe(other);
  });

  it("reports unavailable reads and writes without exposing storage messages", () => {
    const read = fixture();
    read.deny();
    expect(() => read.journal.load()).toThrow("unavailable");
    const write = fixture();
    write.journal.load();
    write.deny();
    expect(() => write.journal.save(record())).toThrow("unavailable");
    expect(write.raw()).toBeNull();
  });
});
