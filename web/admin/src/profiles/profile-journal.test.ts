import { expect, it } from "vitest";
import { ProfileJournal, PROFILE_JOURNAL_KEY, type ProfileRecord } from "./profile-journal";
import { draft, request, storage } from "./profile-test-fixtures";

const record = (): ProfileRecord => ({
  version: 1,
  phase: "unknown",
  key: "original-profile-key",
  body: request(),
});

it("rejects an accepted cache with an extra optional definition field", () => {
  const body = request();
  delete body.description;
  const result = { ...draft(body), description: "server-injected" };
  expect(() =>
    new ProfileJournal(storage()).save({
      version: 1,
      phase: "accepted",
      key: "original-profile-key",
      body,
      result,
    }),
  ).toThrow();
});

it("preserves the full original intent and large decimal CAS across reload", () => {
  const s = storage();
  const j = new ProfileJournal(s);
  expect(j.load()).toBe(null);
  const value = { ...record(), body: request({ expected_latest_revision: "9999999999999999998" }) };
  j.save(value);
  expect(new ProfileJournal(s).load()).toEqual(value);
  j.save(null);
  expect(new ProfileJournal(s).load()).toBe(null);
});

it("fails closed on a foreign owner change, invalid record and unavailable storage", () => {
  const s = storage();
  const a = new ProfileJournal(s),
    b = new ProfileJournal(s);
  a.load();
  b.load();
  a.save(record());
  expect(() => b.save(null)).toThrow();
  s.setItem(PROFILE_JOURNAL_KEY, '{"version":2}');
  expect(() => new ProfileJournal(s).load()).toThrow();
  expect(() =>
    new ProfileJournal({
      getItem() {
        throw new Error("private");
      },
      setItem() {},
    }).load(),
  ).toThrow();
});

it("rejects an accepted journal record for a different intent and freezes nested request fields", () => {
  const s = storage(),
    j = new ProfileJournal(s);
  const result = { ...draft(), profile_id: "foreign" };
  expect(() => j.save({ ...record(), phase: "accepted", result })).toThrow();
  j.save(record());
  const loaded = j.load()!;
  expect(Object.isFrozen(loaded.body.parameters)).toBe(true);
  expect(() => Object.assign(loaded.body.parameters, { output: {} })).toThrow();
});
