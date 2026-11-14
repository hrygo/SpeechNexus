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
/** The shape this repository's previous bundle wrote: one refusal bit, no category (#798). */
function legacyRecord(rejected: boolean) {
  return JSON.stringify({
    version: 1,
    command: { key: "original-key", body: "合成正文" },
    uncertain: false,
    rejected,
  });
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
      refused: null,
      command: { key: "original-key", body: "合成正文" },
    });
    expect(store.data.get("synthetic")).toBe(raw);
  });
  it("does not settle an unknown outcome from a later ingress rejection", () => {
    const journal = new CommandJournal("synthetic", validate, storage());
    journal.begin({ key: "original-key", body: "合成正文" });
    journal.failure(null);
    journal.failure("terminal");
    expect(journal.load()).toMatchObject({
      uncertain: true,
      refused: null,
      command: { key: "original-key" },
    });
  });
  it("keeps a first definitive rejection distinct from an unknown outcome", () => {
    const journal = new CommandJournal("synthetic", validate, storage());
    journal.begin({ key: "original-key", body: "合成正文" });
    journal.failure("terminal");
    expect(journal.load()).toMatchObject({ uncertain: false, refused: "terminal" });
  });
  it("persists the category of a retryable rejection, so a reload reads the same one", () => {
    // #798: the record has to carry the classification, not only the fact of a refusal,
    // or the next page cannot tell a 429 from a spent key.
    const store = storage(),
      first = new CommandJournal("synthetic", validate, store);
    first.begin({ key: "original-key", body: "合成正文" });
    first.failure("retryable");
    expect(first.load()).toMatchObject({ uncertain: false, refused: "retryable" });
    const restored = new CommandJournal("synthetic", validate, store);
    expect(restored.load()).toMatchObject({ uncertain: false, refused: "retryable" });
  });
  it("keeps the unknown outcome when a classified refusal follows an unobserved attempt", () => {
    // The earlier attempt may have created a Job under this key: a refusal that only says
    // the server would not start another one cannot settle it (#754).
    const journal = new CommandJournal("synthetic", validate, storage());
    journal.begin({ key: "original-key", body: "合成正文" });
    journal.failure("retryable");
    const first = journal.load();
    expect(first).toMatchObject({ uncertain: false, refused: "retryable" });
    // A later transport failure is still the unknown outcome, whatever came before.
    journal.failure(null);
    expect(journal.load()).toMatchObject({ uncertain: true, refused: null });
  });
  it("reads a record written before #798 as an unclassified refusal, deterministically", () => {
    // The legacy bit says "a refusal was observed" and cannot say whether its key may be
    // re-sent. The only determinate reading is `unclassified`: never retryable, so no
    // reader can re-offer a key a terminal refusal already spent.
    const store = storage();
    store.data.set("synthetic", legacyRecord(true));
    const journal = new CommandJournal("synthetic", validate, store);
    expect(journal.load()).toEqual({
      version: 1,
      command: { key: "original-key", body: "合成正文" },
      uncertain: false,
      refused: "unclassified",
    });
    // Reading does not rewrite the stored bytes: only a save migrates the shape.
    expect(store.data.get("synthetic")).toBe(legacyRecord(true));
  });
  it("migrates a restored legacy record to the current shape when it is saved", () => {
    const store = storage();
    store.data.set("synthetic", legacyRecord(true));
    const journal = new CommandJournal("synthetic", validate, store);
    journal.save(journal.load());
    const migrated = JSON.parse(store.data.get("synthetic")!) as Record<string, unknown>;
    expect(migrated).toEqual({
      version: 1,
      command: { key: "original-key", body: "合成正文" },
      uncertain: false,
      refused: "unclassified",
    });
    expect(Object.hasOwn(migrated, "rejected")).toBe(false);
  });
  it("reads a legacy record without a refusal as the unknown outcome", () => {
    const store = storage();
    store.data.set("synthetic", legacyRecord(false));
    const journal = new CommandJournal("synthetic", validate, store);
    expect(journal.load()).toMatchObject({ uncertain: true, refused: null });
  });
  it("refuses a record that states its refusal twice, or not all", () => {
    const both = storage();
    both.data.set(
      "synthetic",
      JSON.stringify({
        version: 1,
        command: { key: "original-key", body: "合成正文" },
        uncertain: false,
        refused: "terminal",
        rejected: true,
      }),
    );
    expect(() => new CommandJournal("synthetic", validate, both)).toThrow();
    const neither = storage();
    neither.data.set(
      "synthetic",
      JSON.stringify({
        version: 1,
        command: { key: "original-key", body: "合成正文" },
        uncertain: false,
      }),
    );
    expect(() => new CommandJournal("synthetic", validate, neither)).toThrow();
  });
  it("refuses an unknown refusal category or a legacy bit that is not a boolean", () => {
    const unknownCategory = storage();
    unknownCategory.data.set(
      "synthetic",
      JSON.stringify({
        version: 1,
        command: { key: "original-key", body: "合成正文" },
        uncertain: false,
        refused: "maybe",
      }),
    );
    expect(() => new CommandJournal("synthetic", validate, unknownCategory)).toThrow();
    const looseBit = storage();
    looseBit.data.set(
      "synthetic",
      JSON.stringify({
        version: 1,
        command: { key: "original-key", body: "合成正文" },
        uncertain: false,
        rejected: "yes",
      }),
    );
    expect(() => new CommandJournal("synthetic", validate, looseBit)).toThrow();
  });
  it("refuses to save the legacy shape: it is restored, never written", () => {
    const store = storage(),
      journal = new CommandJournal("synthetic", validate, store);
    expect(() =>
      journal.save({
        version: 1,
        command: { key: "original-key", body: "合成正文" },
        uncertain: false,
        rejected: true,
      } as unknown as Parameters<typeof journal.save>[0]),
    ).toThrow();
    expect(store.data.has("synthetic")).toBe(false);
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
