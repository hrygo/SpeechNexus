import { expect, it, vi } from "vitest";
import { ProfileHttpError } from "./profile-client";
import { ProfileJournal, PROFILE_JOURNAL_KEY } from "./profile-journal";
import { ProfileSession } from "./profile-session";
import { draft, request, storage } from "./profile-test-fixtures";

function setup(s = storage()) {
  const api = {
    create: vi.fn().mockResolvedValue(draft()),
    read: vi.fn().mockResolvedValue(draft()),
  };
  const session = new ProfileSession(api, new ProfileJournal(s), () => "original-profile-key");
  return { api, session, s };
}

it("requires explicit preparation and confirmation and persists unknown before the POST", async () => {
  const { api, session, s } = setup();
  expect(api.create).not.toHaveBeenCalled();
  expect(session.prepare(request())).toBe(true);
  expect(session.snapshot().record?.phase).toBe("prepared");
  expect(api.create).not.toHaveBeenCalled();
  api.create.mockImplementation(() => {
    expect(JSON.parse(s.getItem(PROFILE_JOURNAL_KEY)!)).toMatchObject({
      phase: "unknown",
      body: request(),
      key: "original-profile-key",
    });
    return Promise.resolve(draft());
  });
  await session.confirm();
  expect(session.snapshot()).toMatchObject({
    record: { phase: "accepted", result: draft() },
    receiptCurrent: true,
    blocked: false,
  });
  expect(session.finish()).toBe(true);
  expect(session.snapshot().record).toBe(null);
});

it("reloads an unknown write without auto-sending, reads without settling it, then explicitly replays exactly", async () => {
  const { api, session, s } = setup();
  session.prepare(request());
  api.create.mockRejectedValueOnce(new ProfileHttpError(503, "profile_result_unknown"));
  await session.confirm();
  expect(session.snapshot().record?.phase).toBe("unknown");
  const next = new ProfileSession(api, new ProfileJournal(s), () => "replacement-must-not-be-used");
  expect(api.create).toHaveBeenCalledTimes(1);
  expect(next.prepare(request({ display_name: "changed" }))).toBe(false);
  expect(next.finish()).toBe(false);
  await next.read(request().profile_id, "1");
  expect(next.snapshot()).toMatchObject({ readCurrent: true, record: { phase: "unknown" } });
  await next.confirm();
  expect(api.create).toHaveBeenCalledTimes(2);
  expect(api.create.mock.calls[0]!.slice(0, 2)).toEqual(api.create.mock.calls[1]!.slice(0, 2));
  expect(next.snapshot().record?.phase).toBe("accepted");
});

it("an ingress refusal after an unknown attempt does not establish absence", async () => {
  const { api, session } = setup();
  session.prepare(request());
  api.create.mockRejectedValueOnce(new ProfileHttpError(503, "profile_result_unknown"));
  await session.confirm();
  api.create.mockRejectedValueOnce(new ProfileHttpError(403, "forbidden"));
  await session.confirm();
  expect(session.snapshot().record?.phase).toBe("unknown");
  expect(session.finish()).toBe(false);
});

it.each(["configuration_changed", "idempotency_conflict"])(
  "records the durable %s rejection without silently rebasing",
  async (code) => {
    const { api, session } = setup();
    session.prepare(request());
    api.create.mockRejectedValueOnce(new ProfileHttpError(409, code));
    await session.confirm();
    expect(session.snapshot().record).toMatchObject({ phase: "rejected", code, body: request() });
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(session.finish()).toBe(true);
  },
);

it("permits correction after a first-attempt authoritative definition refusal", async () => {
  const { api, session } = setup();
  session.prepare(request());
  api.create.mockRejectedValueOnce(new ProfileHttpError(422, "invalid_profile_definition"));
  await session.confirm();
  expect(session.snapshot().record?.phase).toBe("rejected");
  expect(session.finish()).toBe(true);
});

it("blocks writes when recovery storage is unavailable, full or changed by another owner", async () => {
  const { api, session, s } = setup();
  session.prepare(request());
  s.setItem(PROFILE_JOURNAL_KEY, "null");
  await session.confirm();
  expect(session.snapshot().blocked).toBe(true);
  expect(api.create).not.toHaveBeenCalled();
  const denied = new ProfileSession(
    api,
    new ProfileJournal({
      getItem() {
        throw new Error("private");
      },
      setItem() {},
    }),
  );
  expect(denied.snapshot().blocked).toBe(true);
  expect(denied.prepare(request())).toBe(false);
  const quota = new ProfileSession(
    api,
    new ProfileJournal({
      getItem: () => null,
      setItem() {
        throw new Error("quota");
      },
    }),
  );
  expect(quota.prepare(request())).toBe(false);
  expect(api.create).not.toHaveBeenCalled();
});

it("keeps the original unknown intent if the response cannot be journalled", async () => {
  const base = storage();
  let writes = 0;
  const { api, session } = setup({
    ...base,
    setItem(k, v) {
      if (++writes === 3) throw new Error("quota");
      base.setItem(k, v);
    },
  });
  session.prepare(request());
  await session.confirm();
  expect(session.snapshot()).toMatchObject({
    blocked: true,
    receiptCurrent: false,
    record: { phase: "unknown" },
  });
  expect(api.create).toHaveBeenCalledTimes(1);
});

it("ignores a late write after hiding and keeps the original key for explicit recovery", async () => {
  const { api, session } = setup();
  let resolve!: (value: ReturnType<typeof draft>) => void;
  api.create.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  session.prepare(request());
  const pending = session.confirm();
  session.setActive(false);
  expect((api.create.mock.calls[0]![2] as AbortSignal).aborted).toBe(true);
  resolve(draft());
  await pending;
  expect(session.snapshot()).toMatchObject({
    record: { phase: "unknown" },
    receiptCurrent: false,
    busy: null,
  });
  session.setActive(true);
  expect(api.create).toHaveBeenCalledTimes(1);
  await session.confirm();
  expect(session.snapshot().record?.phase).toBe("accepted");
});

it("does not report a schema-valid response for a different intent as accepted", async () => {
  const { api, session } = setup();
  session.prepare(request());
  api.create.mockResolvedValueOnce(draft(request({ profile_id: "other-profile" })));
  await session.confirm();
  expect(session.snapshot().record?.phase).toBe("unknown");
});

it("keeps successful reads as historical after read failure and late responses cannot replace them", async () => {
  const { api, session } = setup();
  await session.read(request().profile_id, "1");
  api.read.mockRejectedValueOnce(new ProfileHttpError(503, "profile_unavailable"));
  await session.read(request().profile_id, "1");
  expect(session.snapshot()).toMatchObject({ revision: draft(), readCurrent: false });
  let resolve!: (value: ReturnType<typeof draft>) => void;
  api.read.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const pending = session.read(request().profile_id, "1");
  session.setActive(false);
  resolve(draft(request({ profile_id: "other" })));
  await pending;
  expect(session.snapshot().revision?.profile_id).toBe(request().profile_id);
});
