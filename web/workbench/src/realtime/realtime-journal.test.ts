import { describe, expect, it, vi } from "vitest";
import { RealtimeJournal, emptyRealtimeRecord, realtimeJournalKey } from "./realtime-journal";
import { request, session, storage } from "./realtime-fixtures";
describe("realtime durable intent, never PCM", () => {
  it("roundtrips exact original creation/cancellation and rejects cross-session or extra fields", () => {
    const s = storage(),
      j = new RealtimeJournal(s),
      r = {
        ...emptyRealtimeRecord(),
        request: request(),
        pending: { kind: "create" as const, key: "create-key", body: request() },
      };
    j.save(r);
    expect(new RealtimeJournal(s).load()).toEqual(r);
    const cancel = {
      ...r,
      session: session(),
      pending: {
        kind: "cancel" as const,
        key: "cancel-key",
        sessionId: "session-demo",
        mode: "abort" as const,
      },
    };
    j.save(cancel);
    expect(new RealtimeJournal(s).load()).toEqual(cancel);
    expect(() =>
      j.save({ ...cancel, pending: { ...cancel.pending, sessionId: "other" } }),
    ).toThrow();
    expect(() => j.save({ ...cancel, pcm: "never" } as typeof cancel)).toThrow();
    expect(() => j.save({ ...r, session: session() })).toThrow();
  });
  it("fails closed on corrupt, concurrent or unavailable storage without replacing it", () => {
    const s = storage();
    s.setItem(realtimeJournalKey, "broken");
    expect(() => new RealtimeJournal(s)).toThrow();
    const good = storage(),
      j = new RealtimeJournal(good);
    good.setItem(realtimeJournalKey, JSON.stringify(emptyRealtimeRecord()));
    expect(() => j.save(emptyRealtimeRecord())).toThrow();
    const fail = vi.fn(() => {
      throw Error("quota");
    });
    expect(() =>
      new RealtimeJournal({ ...storage(), setItem: fail }).save(emptyRealtimeRecord()),
    ).toThrow();
  });
});
