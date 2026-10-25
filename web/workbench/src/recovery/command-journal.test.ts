import { describe, expect, it } from "vitest";
import { CommandJournal, fields } from "./command-journal";
function storage() {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
}
function validate(input: unknown) {
  const value = fields(input, ["key", "body"]);
  if (typeof value.key !== "string" || typeof value.body !== "string")
    throw new Error("Invalid synthetic command");
  return { key: value.key, body: value.body };
}
describe("private command journal", () => {
  it("restores the complete prepared command as unknown without sending or rewriting it", () => {
    const store = storage(),
      first = new CommandJournal("synthetic", validate, store);
    first.begin({ key: "original-key", body: "合成正文" });
    const raw = store.data.get("synthetic");
    const restored = new CommandJournal("synthetic", validate, store);
    expect(restored.load()).toMatchObject({
      uncertain: true,
      rejected: false,
      command: { key: "original-key", body: "合成正文" },
    });
    expect(store.data.get("synthetic")).toBe(raw);
  });
  it("does not settle an unknown outcome from a later ingress rejection", () => {
    const journal = new CommandJournal("synthetic", validate, storage());
    journal.begin({ key: "original-key", body: "合成正文" });
    journal.failure(false);
    journal.failure(true);
    expect(journal.load()).toMatchObject({
      uncertain: true,
      rejected: false,
      command: { key: "original-key" },
    });
  });
  it("keeps a first definitive rejection distinct from an unknown outcome", () => {
    const journal = new CommandJournal("synthetic", validate, storage());
    journal.begin({ key: "original-key", body: "合成正文" });
    journal.failure(true);
    expect(journal.load()).toMatchObject({ uncertain: false, rejected: true });
  });
  it("refuses to overwrite a record changed by another owner", () => {
    const store = storage(),
      journal = new CommandJournal("synthetic", validate, store);
    store.data.set("synthetic", "external-record");
    expect(() => journal.begin({ key: "original-key", body: "合成正文" })).toThrow();
    expect(store.data.get("synthetic")).toBe("external-record");
  });
  it("refuses corrupt private data without replacing it", () => {
    const store = storage();
    store.data.set("synthetic", '{"version":1}');
    expect(() => new CommandJournal("synthetic", validate, store)).toThrow();
    expect(store.data.get("synthetic")).toBe('{"version":1}');
  });
});
