import { describe, expect, it, vi } from "vitest";
import {
  MeetingJournal,
  emptyMeetingRecord,
  meetingJournalKey,
  type MeetingRecord,
} from "./meeting-journal";
import { parse } from "../manuscripts/workbench-client";
import { sample, meeting } from "./meeting-fixtures";

function memory(raw?: string) {
  const data = new Map<string, string>(raw === undefined ? [] : [[meetingJournalKey, raw]]);
  return {
    data,
    storage: {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: vi.fn((k: string, v: string) => {
        data.set(k, v);
      }),
    },
  };
}
function selected(): MeetingRecord {
  const r = emptyMeetingRecord();
  r.project = parse("Project", sample("createProject"));
  r.view = parse("TranscriptView", sample("getTranscript"));
  r.meeting = meeting();
  r.forms.name = r.meeting.display_name;
  r.draft = { text: " e\u0301😀\n ", baseRevision: r.view.revision.revision };
  r.pending = {
    kind: "seal",
    meetingId: r.meeting.id,
    key: "original-key",
    body: { expected_transcript_revision: r.draft.baseRevision, text: r.draft.text },
  };
  return r;
}
describe("meeting durable recovery", () => {
  it("round trips original key/body and precise Unicode without shared references", () => {
    const f = memory();
    const j = new MeetingJournal(f.storage);
    expect(j.load()).toEqual(emptyMeetingRecord());
    expect(f.storage.setItem).not.toHaveBeenCalled();
    const r = selected();
    j.save(r);
    r.draft!.text = "other";
    expect(new MeetingJournal(f.storage).load().draft!.text).toBe(" e\u0301😀\n ");
    const copy = j.load();
    copy.pending!.key = "changed";
    expect(j.load().pending!.key).toBe("original-key");
  });
  it.each(["{", "null", "[]", '{"version":99}'])("fails closed on corrupt recovery %s", (raw) => {
    const f = memory(raw);
    expect(() => new MeetingJournal(f.storage)).toThrow();
    expect(f.data.get(meetingJournalKey)).toBe(raw);
    expect(f.storage.setItem).not.toHaveBeenCalled();
  });
  it.each(["project", "source", "draft", "intent", "extra", "key"])(
    "rejects inconsistent %s records without overwriting",
    (kind) => {
      const f = memory();
      const j = new MeetingJournal(f.storage);
      const r = selected();
      if (kind === "project") r.project!.id = "foreign-project";
      if (kind === "source") r.meeting!.transcript_id = "foreign-source";
      if (kind === "draft") r.draft!.baseRevision = "2";
      if (kind === "intent" && r.pending!.kind === "seal") r.pending!.body.text = "other";
      if (kind === "extra") Object.assign(r, { endpoint: "http://localhost" });
      if (kind === "key") r.pending!.key = "x".repeat(129);
      expect(() => j.save(r)).toThrow();
      expect(f.storage.setItem).not.toHaveBeenCalled();
    },
  );
  it("retains the prior journal on quota or another writer and bounds UTF-8 bytes", () => {
    const f = memory();
    const j = new MeetingJournal(f.storage);
    j.save(selected());
    const old = f.data.get(meetingJournalKey);
    f.storage.setItem.mockImplementationOnce(() => {
      throw Error("quota");
    });
    expect(() => j.save(selected())).toThrow();
    expect(f.data.get(meetingJournalKey)).toBe(old);
    f.data.set(meetingJournalKey, "other writer");
    expect(() => j.save(selected())).toThrow();
    expect(f.data.get(meetingJournalKey)).toBe("other writer");
    const g = memory();
    const next = selected();
    next.forms.openId = "😀".repeat(3 * 1024 * 1024);
    expect(() => new MeetingJournal(g.storage).save(next)).toThrow();
    expect(g.storage.setItem).not.toHaveBeenCalled();
  });
  it("refuses Minutes intents without the exact displayed seal/profile", () => {
    const f = memory();
    const j = new MeetingJournal(f.storage);
    const r = selected();
    r.meeting!.status = "sealed";
    r.meeting!.sealed_revision = "2";
    r.meeting!.ended_at = "2026-10-24T00:00:00Z";
    r.pending = {
      kind: "minutes",
      meetingId: r.meeting!.id,
      key: "original-key",
      body: { template_revision: "7", llm_model: "approved-model", expected_source_revision: "2" },
    };
    expect(() => j.save(r)).toThrow();
  });
});
