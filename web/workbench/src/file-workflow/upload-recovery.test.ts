// @vitest-environment node
import { describe, expect, it } from "vitest";
import { inputFingerprint, textUploadFile } from "./input-fingerprint";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal, type UploadRecord, uploadJobRecord } from "./task-journal";
import { createDemoTransport } from "./dev/demo-transport";
import { createTtsDemoTransport } from "./dev/tts-demo-transport";

const selection = {
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
};
const originalFile = () => new File(["synthetic audio"], "original.wav", { type: "audio/wav" });
const originalText = "合成文本，不是真实书稿。";

function setup(kind: "asr" | "tts", scenario: "lost-upload" | "staging" | "ready" = "lost-upload") {
  const backend =
    kind === "asr" ? createDemoTransport("success") : createTtsDemoTransport("success");
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const uploads: string[] = [];
  const assets = new Map<string, ReturnType<typeof parse<"Asset">>>();
  let lost = false;
  let change: Record<string, unknown> = {};
  let uploaded: () => Promise<void> = () => Promise.resolve();
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") {
      const key = request.headers.get("Idempotency-Key")!;
      uploads.push(key);
      let asset = assets.get(key);
      if (!asset) {
        asset = parse("Asset", await (await backend.fetch(request)).json());
        assets.set(key, asset);
      }
      await uploaded();
      if (scenario === "lost-upload" && !lost) {
        lost = true;
        throw new TypeError("Synthetic lost upload response");
      }
      return Response.json(
        { ...asset, status: scenario === "staging" ? "staging" : "ready" },
        { status: 201 },
      );
    }
    if (path.startsWith("/v1/nexus/assets/"))
      return Response.json({ ...assets.values().next().value, ...change, status: "ready" });
    return backend.fetch(request);
  });
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  const start = (session: FileTaskSession) =>
    kind === "asr"
      ? session.start(originalFile(), "asr-quality")
      : session.startText(originalText, selection);
  const resume = (session: FileTaskSession, wrong = false) =>
    kind === "asr"
      ? session.resumeFile(
          wrong ? new File(["other"], "original.wav", { type: "audio/wav" }) : originalFile(),
        )
      : session.resumeText(wrong ? "other text" : originalText);
  return {
    backend,
    data,
    storage,
    uploads,
    assets,
    make,
    start,
    resume,
    read: () => new SessionTaskJournal(storage, "task").load()!,
    changeAsset: (value: Record<string, unknown>) => {
      change = value;
    },
    holdUpload: (action: () => Promise<void>) => {
      uploaded = action;
    },
  };
}

describe("bounded private upload fingerprints", () => {
  it("matches the exact input while distinguishing content, filename and media type", async () => {
    const original = await inputFingerprint(originalFile());
    expect(original).toMatch(/^sha256-chain-v1:[a-f0-9]{64}$/);
    expect(await inputFingerprint(originalFile())).toBe(original);
    for (const file of [
      new File(["different"], "original.wav", { type: "audio/wav" }),
      new File(["synthetic audio"], "renamed.wav", { type: "audio/wav" }),
      new File(["synthetic audio"], "original.wav", { type: "audio/flac" }),
    ])
      expect(await inputFingerprint(file)).not.toBe(original);
    expect(await textUploadFile(originalText).text()).toBe(originalText);
  });

  it("uses bounded slices instead of reading the whole File and honours cancellation", async () => {
    const sizes: number[] = [];
    class BoundedFile extends File {
      override arrayBuffer(): Promise<ArrayBuffer> {
        throw new Error("Whole file read forbidden");
      }
      override slice(start?: number, end?: number, type?: string) {
        const blob = super.slice(start, end, type);
        sizes.push(blob.size);
        return blob;
      }
    }
    await inputFingerprint(new BoundedFile([new Uint8Array(2 * 1024 * 1024 + 7)], "large.wav"));
    expect(sizes).toEqual([1024 * 1024, 1024 * 1024, 7]);
    const abort = new AbortController();
    abort.abort();
    await expect(inputFingerprint(originalFile(), abort.signal)).rejects.toThrow();
  });
});

describe.each(["asr", "tts"] as const)("%s upload recovery", (kind) => {
  it("restores unknown upload without POST, rejects other input, then reuses all original keys", async () => {
    const test = setup(kind);
    const first = test.make();
    await test.start(first);
    expect(first.getSnapshot().unknown).toBe(true);
    const record = test.read();
    expect(record.pending).toBe("upload");
    expect(test.data.get("task")).not.toMatch(/original.wav|synthetic audio|合成文本|"text":/);
    first.dispose();
    const second = test.make();
    await second.restore();
    await second.replay();
    expect(second.getSnapshot().requiresInput).toBe(true);
    expect(test.uploads).toHaveLength(1);
    await test.resume(second, true);
    expect(test.uploads).toHaveLength(1);
    expect(test.read()).toEqual(record);
    await test.resume(second);
    expect(test.uploads).toHaveLength(2);
    expect(new Set(test.uploads).size).toBe(1);
    expect(test.assets.size).toBe(1);
    expect(test.backend.stats.createKeys).toEqual([record.createKey]);
    expect(test.read().cancelKey).toBe(record.cancelKey);
    expect(second.getSnapshot().job).toMatchObject({ kind, id: "job-demo" });
    expect(second.getSnapshot().requiresInput).toBe(false);
    second.dispose();
  });

  it("restores confirmed staging identity with GET and explicitly continues the original job", async () => {
    const test = setup(kind, "staging");
    const first = test.make();
    await test.start(first);
    first.dispose();
    expect(test.read().pending).toBe("validation");
    const second = test.make();
    await second.restore();
    expect(test.uploads).toHaveLength(1);
    expect(test.backend.stats.createKeys).toHaveLength(0);
    expect(second.getSnapshot().asset?.status).toBe("ready");
    await second.continueAfterValidation();
    expect(test.backend.stats.createKeys).toHaveLength(1);
    expect(second.getSnapshot().job?.kind).toBe(kind);
    second.dispose();
  });

  it("retains unknown upload identity when acknowledgement cannot be saved", async () => {
    const test = setup(kind, "ready");
    const save = test.storage.setItem;
    test.storage.setItem = (key, value) => {
      if ((JSON.parse(value) as { pending?: unknown }).pending === "validation")
        throw new Error("private");
      save(key, value);
    };
    const first = test.make();
    await test.start(first);
    first.dispose();
    expect(test.read().pending).toBe("upload");
    expect(test.backend.stats.createKeys).toHaveLength(0);
    test.storage.setItem = save;
    const second = test.make();
    await test.resume(second);
    expect(new Set(test.uploads).size).toBe(1);
    expect(test.assets.size).toBe(1);
    expect(second.getSnapshot().job?.kind).toBe(kind);
    second.dispose();
  });

  it("keeps acknowledged asset when the atomic Job handoff cannot be saved", async () => {
    const test = setup(kind, "ready");
    const save = test.storage.setItem;
    test.storage.setItem = (key, value) => {
      if ((JSON.parse(value) as { pending?: unknown }).pending === "create")
        throw new Error("private");
      save(key, value);
    };
    const first = test.make();
    await test.start(first);
    first.dispose();
    expect(test.read().pending).toBe("validation");
    expect(test.backend.stats.createKeys).toHaveLength(0);
    test.storage.setItem = save;
    const second = test.make();
    await second.restore();
    await second.continueAfterValidation();
    expect(test.uploads).toHaveLength(1);
    expect(second.getSnapshot().job?.kind).toBe(kind);
    second.dispose();
  });

  it("blocks changed acknowledged asset identity after reload", async () => {
    const test = setup(kind, "staging");
    const first = test.make();
    await test.start(first);
    first.dispose();
    const saved = test.data.get("task");
    test.changeAsset({ workspace_id: "ws-other" });
    const second = test.make();
    await second.restore();
    await second.continueAfterValidation();
    expect(test.backend.stats.createKeys).toHaveLength(0);
    expect(test.data.get("task")).toBe(saved);
    second.dispose();
  });

  it("does not POST when the initial upload journal write fails", async () => {
    const test = setup(kind);
    test.storage.setItem = () => {
      throw new Error("private");
    };
    const first = test.make();
    await test.start(first);
    expect(test.uploads).toHaveLength(0);
    expect(test.data.size).toBe(0);
    first.dispose();
  });

  it("does not reupload from a restored controller after another has completed the intent", async () => {
    const test = setup(kind);
    const first = test.make();
    await test.start(first);
    first.dispose();
    const winner = test.make();
    const stale = test.make();
    await test.resume(winner);
    const saved = test.data.get("task");
    expect(test.uploads).toHaveLength(2);
    await test.resume(stale);
    expect(test.uploads).toHaveLength(2);
    expect(test.backend.stats.createKeys).toHaveLength(1);
    expect(test.data.get("task")).toBe(saved);
    winner.dispose();
    stale.dispose();
  });

  it.each(["getItem", "setItem"] as const)(
    "does not reupload when restored storage %s becomes unavailable",
    async (method) => {
      const test = setup(kind);
      const first = test.make();
      await test.start(first);
      first.dispose();
      const restored = test.make();
      const saved = test.data.get("task");
      test.storage[method] = () => {
        throw new Error("private");
      };
      await test.resume(restored);
      expect(test.uploads).toHaveLength(1);
      expect(test.backend.stats.createKeys).toHaveLength(0);
      expect(test.data.get("task")).toBe(saved);
      restored.dispose();
    },
  );
});

describe("upload journal transitions", () => {
  it("does not overwrite a later intent from a stale controller", async () => {
    const test = setup("asr", "ready");
    const first = test.make();
    const stale = test.make();
    await test.start(first);
    const saved = test.data.get("task");
    await stale.start(new File(["other"], "different.wav"), "asr-quality");
    expect(test.uploads).toHaveLength(1);
    expect(test.data.get("task")).toBe(saved);
    first.dispose();
    stale.dispose();
  });

  it("never persists or POSTs after disposal during a fingerprint read", async () => {
    const test = setup("asr", "ready");
    let release = () => undefined as void;
    const wait = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reached = () => undefined as void;
    const ready = new Promise<void>((resolve) => {
      reached = resolve;
    });
    class DelayedBlob extends Blob {
      override async arrayBuffer() {
        reached();
        await wait;
        return super.arrayBuffer();
      }
    }
    class DelayedFile extends File {
      override slice(start?: number, end?: number, type?: string) {
        return new DelayedBlob([super.slice(start, end, type)]);
      }
    }
    const first = test.make();
    const action = first.start(new DelayedFile(["synthetic"], "original.wav"), "asr-quality");
    await ready;
    first.dispose();
    release();
    await action;
    expect(test.data.size).toBe(0);
    expect(test.uploads).toHaveLength(0);
  });

  it("keeps the original pending record when an upload acknowledges after disposal", async () => {
    const test = setup("asr", "ready");
    let release = () => undefined as void;
    const wait = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reached = () => undefined as void;
    const ready = new Promise<void>((resolve) => {
      reached = resolve;
    });
    test.holdUpload(async () => {
      reached();
      await wait;
    });
    const first = test.make();
    const action = test.start(first);
    await ready;
    const saved = test.data.get("task");
    first.dispose();
    release();
    await action;
    expect(test.data.get("task")).toBe(saved);
    expect(test.read().pending).toBe("upload");
    expect(test.backend.stats.createKeys).toHaveLength(0);
  });

  it("rejects changed keys/fingerprint and permits only acknowledged original intent handoff", async () => {
    const test = setup("tts", "staging");
    const session = test.make();
    await test.start(session);
    session.dispose();
    const journal = new SessionTaskJournal(test.storage, "task");
    const saved = journal.load() as UploadRecord;
    expect(() => journal.save({ ...saved, uploadKey: crypto.randomUUID() })).toThrow();
    expect(() =>
      journal.save({ ...saved, inputFingerprint: "sha256-chain-v1:" + "b".repeat(64) }),
    ).toThrow();
    expect(() => journal.save({ ...saved, assetBytes: saved.assetBytes! + 1 })).toThrow();
    expect(() => journal.save({ ...saved, assetSha256: "sha256:" + "b".repeat(64) })).toThrow();
    const command = uploadJobRecord(saved);
    expect(() => journal.save({ ...command, createKey: crypto.randomUUID() })).toThrow();
    journal.save(command);
    expect(() => journal.save(saved)).toThrow();
    expect(journal.load()).toEqual(command);
  });

  it("rejects corrupt upload records without overwriting the stored input identity", async () => {
    const test = setup("asr");
    const first = test.make();
    await test.start(first);
    first.dispose();
    const saved = test.read();
    for (const value of [
      { ...saved, text: "private" },
      { ...saved, inputFingerprint: "bad" },
      { ...saved, assetId: "asset-other" },
      { ...saved, version: 99 },
    ]) {
      const raw = JSON.stringify(value);
      test.data.set("task", raw);
      expect(() => test.make()).toThrow();
      expect(test.data.get("task")).toBe(raw);
    }
  });
});
