// @vitest-environment node
import { describe, expect, it } from "vitest";
import { SessionTaskJournal, TaskJournalError, type TaskRecord } from "./task-journal";

export function memoryStorage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
}

function record(): TaskRecord {
  return {
    version: 1,
    model: "asr-quality",
    assetId: "asset-demo",
    workspaceId: "ws-default",
    createKey: crypto.randomUUID(),
    cancelKey: crypto.randomUUID(),
    jobId: null,
    pending: "create",
    operationId: null,
    cursor: null,
  };
}

describe("session task journal", () => {
  it("roundtrips only bounded metadata, no audio or full wire response", () => {
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    expect(journal.load()).toBeNull();
    const original = record();
    journal.save(original);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(original);
    expect(Object.isFrozen(journal.load())).toBe(true);
    expect(storage.data.get("task")).not.toMatch(/text|filename|audio_base64/);
  });

  it.each([
    "{",
    "x".repeat(8193),
    JSON.stringify({ ...record(), version: 2 }),
    JSON.stringify({ ...record(), text: "private" }),
    JSON.stringify({ ...record(), pending: "cancel" }),
    JSON.stringify({ ...record(), pending: null }),
    JSON.stringify({ ...record(), pending: null, jobId: "job-demo", operationId: "op-demo" }),
    JSON.stringify({ ...record(), cursor: "1" }),
    JSON.stringify({ ...record(), jobId: "../private" }),
    JSON.stringify({ ...record(), model: "" }),
    JSON.stringify({ ...record(), createKey: "\ud800" }),
    JSON.stringify({ ...record(), cursor: "1e9" }),
  ])("rejects damaged or expanded records without overwriting them", (value) => {
    const storage = memoryStorage();
    storage.data.set("task", value);
    expect(() => new SessionTaskJournal(storage, "task").load()).toThrow(TaskJournalError);
    expect(storage.data.get("task")).toBe(value);
  });

  it("prevents stale instances and immutable intent changes from overwriting the original", () => {
    const storage = memoryStorage();
    const first = new SessionTaskJournal(storage, "task");
    const stale = new SessionTaskJournal(storage, "task");
    first.load();
    stale.load();
    const original = record();
    first.save(original);
    expect(() => stale.save(record())).toThrow(TaskJournalError);
    expect(() => first.save({ ...original, assetId: "other" })).toThrow(TaskJournalError);
    const accepted = { ...original, jobId: "job-demo", pending: null } as const;
    first.save(accepted);
    expect(() => first.save({ ...accepted, jobId: "other" })).toThrow(TaskJournalError);
    expect(() => first.save(original)).toThrow(TaskJournalError);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(accepted);
  });

  it("sanitizes storage failures and keeps original state on write failure", () => {
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    journal.load();
    const original = record();
    journal.save(original);
    storage.setItem = () => {
      throw new Error("private credential details");
    };
    expect(() => journal.save({ ...original, jobId: "job-demo", pending: null })).toThrow(
      "task recovery storage unavailable",
    );
    expect(journal.load()).toEqual(original);
    storage.getItem = () => {
      throw new Error("private credential details");
    };
    expect(() => journal.load()).toThrow("task recovery storage unavailable");
  });
});
