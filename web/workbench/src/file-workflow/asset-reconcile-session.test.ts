// @vitest-environment node
import { describe, expect, it } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal, type RecoveryRecord } from "./task-journal";
const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const selection = {
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
};
const text = "synthetic original text";
const file = () => new File(["synthetic audio"], "original.wav", { type: "audio/wav" });
function setup(kind: "asr" | "tts", location: string | null = "/v1/nexus/assets/asset-original") {
  let raw: string | null = null;
  let denied = false;
  let ready = false;
  let missing = 0;
  let invalid: Record<string, unknown> = {};
  let pending = true;
  const calls: { path: string; method: string; key: string | null }[] = [];
  const storage = {
    getItem: () => raw,
    setItem: (_key: string, value: string) => {
      if (denied) throw new Error("synthetic-storage-denied");
      raw = value;
    },
  };
  const error = (code: string) =>
    parse("ErrorResponse", {
      error: {
        code,
        type: "api_error",
        message: "synthetic-sensitive",
        retryable: false,
        request_id: "req-demo",
        param: null,
      },
    });
  const asset = () =>
    parse("Asset", {
      ...example("ready-audio"),
      id: "asset-original",
      purpose: kind === "asr" ? "asr_input" : "text_input",
      media_type: kind === "asr" ? "audio/wav" : "text/plain",
      ...(kind === "tts" ? { duration_ms: null, sample_rate_hz: null, channels: null } : {}),
      status: ready ? "ready" : "staging",
      ...invalid,
    });
  const client = new SpeechFileClient("http://127.0.0.1", (input) => {
    const request = input as Request;
    const path = new URL(request.url).pathname;
    calls.push({ path, method: request.method, key: request.headers.get("Idempotency-Key") });
    if (path === "/v1/nexus/assets")
      return Promise.resolve(
        pending
          ? Response.json(error("asset_upload_unknown"), {
              status: 503,
              headers: location ? { Location: location } : {},
            })
          : Response.json(asset(), { status: 201 }),
      );
    if (path.startsWith("/v1/nexus/assets/"))
      return Promise.resolve(
        missing
          ? Response.json(
              error(missing === 410 ? "asset_unavailable" : "asset_service_unavailable"),
              { status: missing },
            )
          : Response.json(asset()),
      );
    if (path === "/v1/nexus/jobs")
      return Promise.resolve(
        Response.json(
          parse("Job", {
            ...example("queued-job"),
            kind,
            ...(kind === "tts"
              ? {
                  effective_configuration: {
                    models: { tts: { id: "model-tts-fixed", revision: "1" } },
                    voice: { id: selection.voiceId, revision: selection.voiceRevision },
                    profile: { id: selection.profileId, revision: selection.profileRevision },
                    policy_revision: "1",
                    catalog_revision: "1",
                  },
                }
              : {}),
          }),
          { status: 201 },
        ),
      );
    return Promise.reject(new Error("Unexpected route"));
  });
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  const start = (s: FileTaskSession) =>
    kind === "asr" ? s.start(file(), "asr-quality") : s.startText(text, selection);
  return {
    make,
    start,
    calls,
    storage,
    client,
    row: () => JSON.parse(raw!) as RecoveryRecord,
    raw: () => raw,
    setReady: () => {
      ready = true;
    },
    deny: () => {
      denied = true;
    },
    replace: (value: string) => {
      raw = value;
    },
    missing: (status: number) => {
      missing = status;
    },
    invalid: (value: Record<string, unknown>) => {
      invalid = value;
    },
    accept: () => {
      pending = false;
      ready = true;
    },
    resume: (s: FileTaskSession) => (kind === "asr" ? s.resumeFile(file()) : s.resumeText(text)),
  };
}
describe.each(["asr", "tts"] as const)("%s original Asset recovery", (kind) => {
  it("persists hint, restores without requests, reads original metadata then explicitly submits one fixed Job", async () => {
    const f = setup(kind);
    const first = f.make();
    await f.start(first);
    expect(first.getSnapshot()).toMatchObject({
      unknown: true,
      canReplay: false,
      requiresInput: false,
      assetRecoveryId: "asset-original",
    });
    expect(f.row()).toMatchObject({
      version: kind === "asr" ? 5 : 6,
      assetHintId: "asset-original",
      pending: "upload",
      assetId: null,
    });
    const original = f.row();
    first.dispose();
    const count = f.calls.length;
    const restored = f.make();
    expect(f.calls).toHaveLength(count);
    await restored.continueAfterValidation();
    expect(f.calls).toHaveLength(count);
    await restored.restore();
    expect(f.calls.at(-1)).toMatchObject({
      method: "GET",
      path: "/v1/nexus/assets/asset-original",
    });
    expect(restored.getSnapshot()).toMatchObject({
      unknown: false,
      requiresInput: false,
      asset: { status: "staging" },
    });
    f.setReady();
    await restored.refresh();
    expect(restored.getSnapshot().asset?.status).toBe("ready");
    expect(f.calls.filter((c) => c.path === "/v1/nexus/jobs")).toHaveLength(0);
    await restored.continueAfterValidation();
    expect(f.calls.filter((c) => c.path === "/v1/nexus/jobs")).toEqual([
      { method: "POST", path: "/v1/nexus/jobs", key: original.createKey },
    ]);
    expect(
      f.calls.filter((c) => c.method === "POST" && c.path === "/v1/nexus/assets"),
    ).toHaveLength(1);
    expect(f.calls.some((c) => c.path.includes("operations"))).toBe(false);
    expect(f.row()).toMatchObject({
      version: kind === "asr" ? 1 : 2,
      assetId: "asset-original",
      pending: null,
      createKey: original.createKey,
      cancelKey: original.cancelKey,
    });
  });
  it.each([404, 410, 503])(
    "retains original hint on metadata %i without resending input or creating Job",
    async (status) => {
      const f = setup(kind);
      const first = f.make();
      await f.start(first);
      const before = f.raw();
      first.dispose();
      f.missing(status);
      const restored = f.make();
      await restored.restore();
      await restored.continueAfterValidation();
      await restored.replay();
      expect(restored.getSnapshot()).toMatchObject({
        unknown: true,
        canReplay: false,
        assetRecoveryId: "asset-original",
      });
      expect(f.raw()).toBe(before);
      expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    },
  );
  it.each([{ id: "other" }, { purpose: kind === "asr" ? "text_input" : "asr_input" }])(
    "rejects mismatched original Asset %j",
    async (change) => {
      const f = setup(kind);
      const s = f.make();
      await f.start(s);
      f.invalid(change);
      await s.refresh();
      await s.continueAfterValidation();
      expect(s.getSnapshot().asset).toBeUndefined();
      expect(s.getSnapshot().unknown).toBe(true);
      expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    },
  );
  it("keeps original key when hint persistence fails and cannot create a Job", async () => {
    const f = setup(kind);
    const s = f.make();
    const save = f.storage.setItem;
    f.storage.setItem = (key, value) => {
      if ((JSON.parse(value) as { version: number }).version >= 5)
        throw new Error("synthetic-denied");
      save(key, value);
    };
    await f.start(s);
    expect(s.getSnapshot().unknown).toBe(true);
    expect(s.getSnapshot().canReplay).toBe(false);
    expect(f.row()).toMatchObject({ version: kind === "asr" ? 3 : 4, pending: "upload" });
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
  it("preserves old no-Location intents and restores the same original upload key with original input", async () => {
    const f = setup(kind, null);
    const first = f.make();
    await f.start(first);
    expect(first.getSnapshot().unknown).toBe(true);
    const original = f.row();
    first.dispose();
    const restored = f.make();
    expect(restored.getSnapshot().requiresInput).toBe(true);
    f.accept();
    await f.resume(restored);
    const uploads = f.calls.filter((c) => c.path === "/v1/nexus/assets");
    expect(uploads).toHaveLength(2);
    expect(uploads[1]!.key).toBe(uploads[0]!.key);
    expect(
      f.calls.filter((c) => c.path === "/v1/nexus/jobs")[0]?.key,
      JSON.stringify({
        notice: restored.getSnapshot().notice,
        phase: f.row().pending,
        version: f.row().version,
      }),
    ).toBe(original.createKey);
  });
  it("does not acknowledge metadata or submit Job after owner replacement", async () => {
    const f = setup(kind);
    const s = f.make();
    await f.start(s);
    const other = JSON.stringify({ ...f.row(), assetHintId: "other" });
    f.replace(other);
    f.setReady();
    await s.refresh();
    await s.continueAfterValidation();
    expect(f.raw()).toBe(other);
    expect(s.getSnapshot().asset).toBeUndefined();
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
  it("ignores late original metadata after dispose", async () => {
    const f = setup(kind);
    const s = f.make();
    await f.start(s);
    const before = f.raw();
    const original = f.client.getAsset.bind(f.client);
    f.setReady();
    const asset = await original("asset-original");
    let resolve!: (value: typeof asset) => void;
    f.client.getAsset = () =>
      new Promise((r) => {
        resolve = r;
      });
    const reading = s.refresh();
    s.dispose();
    resolve(asset);
    await reading;
    expect(f.raw()).toBe(before);
    expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});
