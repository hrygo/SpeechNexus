import { expect, it } from "vitest";
import { TtsJournal, ttsJournalKey } from "./tts-journal";
import { storage } from "./realtime-fixtures";
import { ttsRequest, ttsSession } from "./tts-fixtures";

it("persists exact text/original intent, rejects external rewrite, corrupt input and quota", () => {
  const s = storage(),
    j = new TtsJournal(s),
    r = {
      ...j.load(),
      text: "甲\n😀",
      request: ttsRequest(),
      pending: { kind: "create" as const, key: "original", body: ttsRequest() },
    };
  j.save(r);
  expect(new TtsJournal(s).load()).toEqual(r);
  s.setItem(ttsJournalKey, "foreign");
  expect(() => j.save(r)).toThrow();
  expect(() => new TtsJournal(s)).toThrow();
  const q = new TtsJournal({
    getItem: () => null,
    setItem: () => {
      throw Error("quota");
    },
  });
  expect(() => q.save(r)).toThrow();
});
it("rejects wire audio, ASR modality and session/voice/pending identity corruption", () => {
  const s = storage(),
    j = new TtsJournal(s),
    r = { ...j.load(), request: ttsRequest(), session: ttsSession() };
  for (const bad of [
    { ...r, audio: "AAA=" },
    {
      ...r,
      session: { ...ttsSession(), id: "other" },
      pending: { kind: "cancel", key: "k", sessionId: ttsSession().id, mode: "abort" },
    },
    {
      ...r,
      session: {
        ...ttsSession(),
        effective_configuration: {
          ...ttsSession().effective_configuration,
          voice: { id: "other", revision: "1" },
        },
      },
    },
  ])
    expect(() => j.save(bad as typeof r)).toThrow();
});
