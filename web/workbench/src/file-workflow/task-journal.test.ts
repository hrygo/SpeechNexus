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

/** A cancel intent names the Job it was refused against. */
function cancelRecord(): TaskRecord {
  return { ...record(), jobId: "job-demo", pending: "cancel" };
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

  it("roundtrips an observed terminal refusal and never lets it be un-settled", () => {
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    journal.load();
    const spent = { ...record(), settled: "terminal" } as const;
    journal.save(spent);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(spent);
    // Dropping the field would rebuild the refused intent as an unconfirmed submission on
    // the next reload, which is exactly the defect #707 removes.
    const revived = Object.fromEntries(
      Object.entries(spent).filter(([field]) => field !== "settled"),
    ) as unknown as TaskRecord;
    expect(() => journal.save(revived)).toThrow(TaskJournalError);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(spent);
  });

  it("roundtrips an unreplayable 413 verdict and never lets it be un-settled or re-labelled", () => {
    // #729: the request-bytes refusal is a distinct persisted verdict. It shares the
    // abandon-only shape with `terminal`, but a reader must be able to keep the difference
    // between "no execution proven" and "proven no execution".
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    journal.load();
    const voided = { ...record(), settled: "unreplayable" } as const;
    journal.save(voided);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(voided);
    const revived = Object.fromEntries(
      Object.entries(voided).filter(([field]) => field !== "settled"),
    ) as unknown as TaskRecord;
    expect(() => journal.save(revived)).toThrow(TaskJournalError);
    // Re-labelling would change what the page may claim about an earlier attempt.
    expect(() => journal.save({ ...voided, settled: "terminal" })).toThrow(TaskJournalError);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(voided);
  });

  it("carries the settled verdict through a later rewrite instead of erasing it", () => {
    // No session write follows a settled refusal today, so this guards the write path a
    // future one would use: saving the record again must carry `settled` rather than
    // silently rebuilding the refused intent (#707 review carry case).
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    journal.load();
    const spent = { ...record(), settled: "terminal" } as const;
    journal.save(spent);
    journal.save({ ...spent });
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(spent);
  });

  it.each([
    JSON.stringify({ ...record(), settled: "unknown" }),
    JSON.stringify({ ...record(), settled: "later" }),
    JSON.stringify({ ...record(), settled: true }),
    JSON.stringify({ ...record(), settled: "terminal", operationId: "op-demo" }),
    JSON.stringify({ ...record(), settled: "terminal", jobId: "job-demo", pending: null }),
    JSON.stringify({ ...record(), settled: "terminal", operationId: null, cursor: "1" }),
    JSON.stringify({ ...record(), settled: "terminal", futureApi: 1 }),
    // A settled verdict only rides on a still-pending intent that names no identified
    // execution (#707 M2); the class shapes themselves come from the earlier invariants
    // (#723 keeps them shared instead of restating them here).
    JSON.stringify({ ...cancelRecord(), settled: "terminal", operationId: "op-demo" }),
    JSON.stringify({ ...cancelRecord(), settled: "terminal", jobId: null }),
    JSON.stringify({ ...cancelRecord(), settled: "terminal", pending: null }),
  ])("rejects a settled verdict the protocol never produces", (value) => {
    const storage = memoryStorage();
    storage.data.set("task", value);
    expect(() => new SessionTaskJournal(storage, "task").load()).toThrow(TaskJournalError);
    expect(storage.data.get("task")).toBe(value);
  });

  it("roundtrips a settled cancel attempt whose Job and cursor stay readable", () => {
    const storage = memoryStorage();
    const journal = new SessionTaskJournal(storage, "task");
    journal.load();
    const spent = { ...cancelRecord(), settled: "terminal" } as const;
    journal.save(spent);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(spent);
    // The confirmed event cursor is not part of the verdict: events keep advancing while
    // the refusal is recorded, and a cancel record may already carry one.
    const advanced = { ...spent, cursor: "7" } as const;
    journal.save(advanced);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(advanced);
    // The verdict names the attempt it spends, so neither its class nor its Job can be
    // re-pointed, and dropping the field would revive the refused cancellation.
    expect(() => journal.save({ ...advanced, jobId: "job-other" })).toThrow(TaskJournalError);
    expect(() =>
      journal.save({ ...advanced, pending: "create", jobId: null, cursor: null }),
    ).toThrow(TaskJournalError);
    const revived = Object.fromEntries(
      Object.entries(advanced).filter(([field]) => field !== "settled"),
    ) as unknown as TaskRecord;
    expect(() => journal.save(revived)).toThrow(TaskJournalError);
    expect(new SessionTaskJournal(storage, "task").load()).toEqual(advanced);
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
