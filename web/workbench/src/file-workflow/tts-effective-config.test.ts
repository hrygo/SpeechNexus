// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient, parse, type FileContracts } from "./speech-file-client";
import {
  SessionTaskJournal,
  TaskJournalError,
  jobCommand,
  type TtsSelection,
} from "./task-journal";
import { createTtsDemoTransport } from "./dev/tts-demo-transport";

type Effective = FileContracts["Job"]["effective_configuration"];
const selection: TtsSelection = {
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
};
const effective: Effective = {
  voice: { id: selection.voiceId, revision: selection.voiceRevision },
  profile: { id: selection.profileId, revision: selection.profileRevision },
  // A request alias need not equal the resolved model's immutable identity.
  models: { tts: { id: "model-tts-fixed", revision: "9007199254740993" } },
  policy_revision: "1",
  catalog_revision: "1",
};
const mismatches: { name: string; change: (value: Effective) => Effective }[] = [
  ...(["voice", "profile"] as const).flatMap((field) => [
    {
      name: `missing ${field}`,
      change: (value: Effective) => {
        const copy = { ...value };
        delete copy[field];
        return copy;
      },
    },
    { name: `null ${field}`, change: (value: Effective) => ({ ...value, [field]: null }) },
    {
      name: `wrong ${field} identity`,
      change: (value: Effective) => ({ ...value, [field]: { ...value[field], id: "other" } }),
    },
    {
      name: `wrong ${field} revision`,
      change: (value: Effective) => ({ ...value, [field]: { ...value[field], revision: "9" } }),
    },
  ]),
  {
    name: "missing TTS model",
    change: (value) => ({ ...value, models: { asr: { id: "model-asr", revision: "1" } } }),
  },
];
const modelChanges = [
  {
    name: "changed model identity",
    change: (value: Effective) => ({
      ...value,
      models: { tts: { ...value.models.tts!, id: "model-other" } },
    }),
  },
  {
    name: "changed model revision",
    change: (value: Effective) => ({
      ...value,
      models: { tts: { ...value.models.tts!, revision: "9007199254740994" } },
    }),
  },
];

function setup() {
  const transport = createTtsDemoTransport("success");
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  let change = (value: Effective) => value;
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const response = await transport.fetch(input, init);
    if (!response.headers.get("content-type")?.includes("application/json")) return response;
    const value: unknown = await response.clone().json();
    if (!value || typeof value !== "object" || !("object" in value) || value.object !== "nexus.job")
      return response;
    // Keep all negative replies schema-valid: these tests cover semantic identity checks.
    const job = parse("Job", { ...value, effective_configuration: change(effective) });
    return Response.json(job, { status: response.status });
  });
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "tts"));
  return {
    ...transport,
    storage,
    data,
    make,
    change: (next: typeof change) => {
      change = next;
    },
    saved: () => data.get("tts")!,
    record: () => new SessionTaskJournal(storage, "tts").load()!,
  };
}

describe("TTS effective resource confirmation", () => {
  it.each(mismatches)("keeps create unknown for $name", async ({ change }) => {
    const test = setup();
    test.change(change);
    const session = test.make();
    await session.startText("Synthetic test.", selection);
    expect(session.getSnapshot().job).toBeUndefined();
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(test.record()).toMatchObject({ pending: "create", jobId: null });
    expect(test.stats.executions).toBe(1);
    session.dispose();
  });

  it("recovers the accepted original request without changing its key or body", async () => {
    const test = setup();
    test.change(mismatches[0]!.change);
    const first = test.make();
    await first.startText("Synthetic test.", selection);
    first.dispose();
    const session = test.make();
    await session.restore();
    expect(session.getSnapshot().unknown).toBe(true);
    test.change((value) => value);
    await session.replay();
    expect(test.stats.createKeys).toHaveLength(2);
    expect(new Set(test.stats.createKeys).size).toBe(1);
    expect(test.stats.bodies[1]).toBe(test.stats.bodies[0]);
    expect(test.stats.executions).toBe(1);
    expect(session.getSnapshot()).toMatchObject({ unknown: false, job: { kind: "tts" } });
    expect(test.record()).toMatchObject({
      pending: null,
      effectiveModelKey: JSON.stringify(["model-tts-fixed", "9007199254740993"]),
    });
    session.dispose();
  });

  for (const action of ["refresh", "restore"] as const) {
    it.each([...mismatches, ...modelChanges])(
      `rejects $name on ${action} without overwriting confirmed facts`,
      async ({ change }) => {
        const test = setup();
        let session = test.make();
        await session.startText("Synthetic test.", selection);
        const previous = session.getSnapshot().job;
        const saved = test.saved();
        if (action === "restore") {
          session.dispose();
          session = test.make();
        }
        test.change(change);
        await session[action]();
        expect(session.getSnapshot().job).toBe(action === "restore" ? undefined : previous);
        expect(test.saved()).toBe(saved);
        expect(test.stats.createKeys).toHaveLength(1);
        session.dispose();
      },
    );
  }

  it("does not advance the event cursor for a different model revision", async () => {
    const test = setup();
    const session = test.make();
    await session.startText("Synthetic test.", selection);
    const previous = session.getSnapshot().job;
    const saved = test.saved();
    test.change(modelChanges[1]!.change);
    await session.reconnect();
    expect(session.getSnapshot().job).toBe(previous);
    expect(session.getSnapshot().stream).toBe("interrupted");
    expect(session.getSnapshot().cursor).toBeUndefined();
    expect(test.saved()).toBe(saved);
    session.dispose();
  });

  it("does not confirm cancellation from a mismatched effective profile", async () => {
    const test = setup();
    const session = test.make();
    await session.startText("Synthetic test.", selection);
    const previous = session.getSnapshot().job;
    test.change(mismatches.find((item) => item.name === "wrong profile revision")!.change);
    await session.cancel();
    expect(session.getSnapshot().job).toBe(previous);
    expect(session.getSnapshot().cancellation).toBeUndefined();
    expect(session.getSnapshot().unknown).toBe(true);
    expect(test.record()).toMatchObject({ pending: "cancel", jobId: "job-demo" });
    session.dispose();
  });

  it("adds model confirmation to a legacy v2 record only after a valid read", async () => {
    const test = setup();
    const first = test.make();
    await first.startText("Synthetic test.", selection);
    first.dispose();
    const record = test.record();
    if (record.version !== 2) throw new Error("Expected a TTS task record");
    const legacy = { ...record };
    delete legacy.effectiveModelKey;
    test.storage.setItem("tts", JSON.stringify(legacy));
    const command = jobCommand(test.record());
    const session = test.make();
    await session.restore();
    expect(test.record()).toHaveProperty("effectiveModelKey");
    expect(jobCommand(test.record())).toEqual(command);
    session.dispose();
  });

  it("keeps the original create pending when model confirmation cannot be stored", async () => {
    const test = setup();
    const write = test.storage.setItem;
    test.storage.setItem = (key, value) => {
      const parsed: unknown = JSON.parse(value);
      if (parsed && typeof parsed === "object" && Object.hasOwn(parsed, "effectiveModelKey"))
        throw new Error("private");
      write(key, value);
    };
    const session = test.make();
    await session.startText("Synthetic test.", selection);
    expect(session.getSnapshot().job).toBeUndefined();
    expect(session.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(session.getSnapshot().notice).not.toContain("private");
    expect(test.record()).toMatchObject({ pending: "create", jobId: null });
    test.storage.setItem = write;
    await session.replay();
    expect(session.getSnapshot().unknown).toBe(false);
    expect(new Set(test.stats.createKeys).size).toBe(1);
    expect(test.stats.executions).toBe(1);
    session.dispose();
  });

  it("cannot change or remove a confirmed model pin in the durable journal", async () => {
    const test = setup();
    const session = test.make();
    await session.startText("Synthetic test.", selection);
    const journal = new SessionTaskJournal(test.storage, "tts");
    const confirmed = journal.load()!;
    if (confirmed.version !== 2) throw new Error("Expected a TTS task record");
    const saved = test.saved();
    const removed = { ...confirmed };
    delete removed.effectiveModelKey;
    expect(() => journal.save(removed)).toThrow(TaskJournalError);
    expect(() => journal.save({ ...confirmed, effectiveModelKey: "changed" })).toThrow(
      TaskJournalError,
    );
    expect(test.saved()).toBe(saved);
    session.dispose();
  });

  it.each(["", "x".repeat(257), null, 1])(
    "rejects damaged private model confirmation metadata",
    async (effectiveModelKey) => {
      const test = setup();
      const session = test.make();
      await session.startText("Synthetic test.", selection);
      const raw = JSON.stringify({ ...test.record(), effectiveModelKey });
      test.storage.setItem("tts", raw);
      expect(() => new SessionTaskJournal(test.storage, "tts").load()).toThrow(TaskJournalError);
      expect(test.saved()).toBe(raw);
      session.dispose();
    },
  );
});
