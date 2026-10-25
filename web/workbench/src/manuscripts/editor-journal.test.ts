import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { EditorJournal, emptyRecord, journalKey, type EditorRecord } from "./editor-journal";
import { parse } from "./workbench-client";

function example(operation: string) {
  return structuredClone(
    fixtures.examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === "response" &&
        e.valid &&
        typeof e.status === "number" &&
        [200, 201].includes(e.status),
    )!.value.body,
  );
}
function memory(initial: string | null = null) {
  const data = new Map<string, string>();
  if (initial !== null) data.set(journalKey, initial);
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: vi.fn((k: string, v: string) => {
      data.set(k, v);
    }),
  };
  return { data, storage };
}
function edited(): EditorRecord {
  const record = emptyRecord();
  record.project = parse("Project", example("createProject"));
  record.view = parse("TranscriptView", example("getTranscript"));
  record.draft = { text: " e\u0301😀\n ", baseRevision: "1", sourceRevision: "1" };
  record.pending = {
    kind: "revise",
    key: "original-key",
    transcriptId: "transcript-demo",
    body: { text: record.draft.text, expected_revision: "1", source_revision: "1" },
  };
  return record;
}

describe("closed editor recovery record", () => {
  it("starts empty without writes and round-trips exact pending text/key", () => {
    const f = memory();
    const journal = new EditorJournal(f.storage);
    expect(journal.load()).toEqual(emptyRecord());
    expect(f.storage.setItem).not.toHaveBeenCalled();
    const record = edited();
    journal.save(record);
    expect(new EditorJournal(f.storage).load()).toEqual(record);
    record.draft!.text = "changed later";
    expect(journal.load().draft!.text).toBe(" e\u0301😀\n ");
    const loaded = journal.load();
    loaded.pending!.key = "different-key";
    expect(journal.load().pending!.key).toBe("original-key");
  });

  it.each(["{", "null", "[]", '{"version":99}', '{"version":1}'])(
    "fails closed on corrupt/unknown shape without clearing it: %s",
    (raw) => {
      const f = memory(raw);
      expect(() => new EditorJournal(f.storage)).toThrow("Editor recovery unavailable");
      expect(f.data.get(journalKey)).toBe(raw);
      expect(f.storage.setItem).not.toHaveBeenCalled();
    },
  );

  it("rejects cross-project/forged-view/inconsistent-intent and extra fields", () => {
    const records = [edited(), edited(), edited(), edited(), edited()];
    records[0]!.view!.transcript.project_id = "other-project";
    records[1]!.view!.revision.transcript_id = "other-transcript";
    (records[2]!.pending as { body: { text: string } }).body.text = "different intent";
    records[3]!.pending!.key = "short";
    Object.assign(records[4]!, { unknown: true });
    for (const record of records) {
      const raw = JSON.stringify(record);
      const f = memory(raw);
      expect(() => new EditorJournal(f.storage)).toThrow("Editor recovery unavailable");
      expect(f.storage.setItem).not.toHaveBeenCalled();
    }
  });

  it("rejects invalid generated request bodies and does not replace prior data", () => {
    const f = memory();
    const journal = new EditorJournal(f.storage);
    const valid = edited();
    journal.save(valid);
    const before = f.data.get(journalKey);
    const invalid = edited();
    (invalid.pending as { body: { expected_revision: unknown } }).body.expected_revision = 1;
    expect(() => journal.save(invalid)).toThrow("Editor recovery unavailable");
    expect(f.data.get(journalKey)).toBe(before);
  });

  it("detects another writer and preserves that writer's record", () => {
    const f = memory();
    const one = new EditorJournal(f.storage);
    const two = new EditorJournal(f.storage);
    one.save(edited());
    const stored = f.data.get(journalKey);
    expect(() => two.save(emptyRecord())).toThrow("Editor recovery unavailable");
    expect(f.data.get(journalKey)).toBe(stored);
  });

  it("does not advance its expected record after quota failures", () => {
    const f = memory();
    const journal = new EditorJournal(f.storage);
    f.storage.setItem.mockImplementationOnce(() => {
      throw new Error("private-synthetic");
    });
    expect(() => journal.save(edited())).toThrow("Editor recovery unavailable");
    expect(journal.load()).toEqual(emptyRecord());
    journal.save(edited());
    expect(journal.load()).toEqual(edited());
  });
});
