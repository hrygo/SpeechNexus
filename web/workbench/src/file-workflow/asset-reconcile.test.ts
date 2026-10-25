// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { SpeechFileClient, SpeechUploadRecoveryError, parse } from "./speech-file-client";
import { SessionTaskJournal, type RecoveryRecord } from "./task-journal";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const originalKey = "9f712fc4-943f-4171-b910-9132c44927a2";
const file = () => new File(["synthetic"], "synthetic.txt", { type: "text/plain" });
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
function uploadRecord(tts = false): RecoveryRecord {
  const base = {
    version: 3 as const,
    model: "asr-quality",
    assetId: null,
    workspaceId: null,
    createKey: originalKey,
    cancelKey: originalKey,
    jobId: null,
    pending: "upload" as const,
    operationId: null,
    cursor: null,
    uploadKey: originalKey,
    inputFingerprint: `sha256-chain-v1:${"a".repeat(64)}`,
    assetMediaType: null,
    assetBytes: null,
    assetSha256: null,
  };
  return tts
    ? {
        ...base,
        version: 4,
        model: "tts-quality",
        voiceId: "voice-demo",
        voiceRevision: "1",
        profileId: "profile-demo",
        profileRevision: "2",
      }
    : base;
}
const envelope = (code: string) =>
  parse("ErrorResponse", {
    ...example("recover-operation"),
    error: {
      code,
      type: "api_error",
      message: "synthetic-sensitive-value",
      retryable: false,
      request_id: "req-demo",
      param: null,
    },
  });

describe("A asset upload consumer", () => {
  it("invokes the default native fetch with its global receiver", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(function (this: unknown) {
      expect(this).toBe(globalThis);
      return Promise.resolve(Response.json(example("ready-audio"), { status: 201 }));
    });
    try {
      await new SpeechFileClient("http://127.0.0.1").upload(file(), originalKey);
    } finally {
      fetch.mockRestore();
    }
  });

  it("preserves actual multipart input, original key, omitted metadata, signal and redirect policy", async () => {
    const owner = new AbortController();
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      expect(request.redirect).toBe("error");
      expect(request.headers.get("Idempotency-Key")).toBe(originalKey);
      const form = await request.formData();
      expect([...form.keys()].sort()).toEqual(["file", "purpose"]);
      const binary = form.get("file") as File;
      expect(binary.name).toBe("synthetic.txt");
      expect(binary.type).toBe("text/plain");
      expect(await binary.text()).toBe("synthetic");
      owner.abort();
      expect(request.signal.aborted).toBe(true);
      return Response.json(example("ready-audio"), { status: 201 });
    });
    await client.upload(file(), originalKey, owner.signal);
  });
  it.each([
    [409, "asset_upload_in_progress"],
    [503, "asset_upload_unknown"],
    [503, "asset_metadata_unknown"],
  ] as const)("keeps original Asset hint for %i %s", async (status, code) => {
    const client = new SpeechFileClient("http://127.0.0.1", () =>
      Promise.resolve(
        Response.json(envelope(code), {
          status,
          headers: { Location: "/v1/nexus/assets/asset-original" },
        }),
      ),
    );
    try {
      await client.upload(file(), originalKey);
      expect.fail("expected upload recovery");
    } catch (error) {
      expect(error).toBeInstanceOf(SpeechUploadRecoveryError);
      expect(error).toMatchObject({ code, status, assetId: "asset-original" });
      expect((error as Error).message).not.toContain("synthetic-sensitive-value");
    }
  });
  it.each([
    null,
    "https://external.invalid/v1/nexus/assets/id",
    "//external.invalid/v1/nexus/assets/id",
    "/v1/nexus/operations/id",
    "/v1/nexus/assets/id?token=x",
    "/v1/nexus/assets/id#fragment",
    "/v1/nexus/assets/a%2Fb",
    "/v1/nexus/assets/../id",
  ])("retains unknown without following unsafe Location %s", async (location) => {
    let calls = 0;
    const client = new SpeechFileClient("http://127.0.0.1", () => {
      calls++;
      return Promise.resolve(
        Response.json(envelope("asset_upload_unknown"), {
          status: 503,
          headers: location ? { Location: location } : {},
        }),
      );
    });
    await expect(client.upload(file(), originalKey)).rejects.toMatchObject({
      assetId: null,
      code: "asset_upload_unknown",
    });
    expect(calls).toBe(1);
  });
});

describe.each([false, true])("Asset hint journal tts=%s", (tts) => {
  it("upgrades old intent once, fixes original hint, then permits verified facts and original Job handoff", () => {
    const s = storage();
    const journal = new SessionTaskJournal(s, "task");
    const old = uploadRecord(tts);
    journal.save(old);
    const hint = { ...old, version: tts ? 6 : 5, assetHintId: "asset-original" } as RecoveryRecord;
    journal.save(hint);
    expect(new SessionTaskJournal(s, "task").load()).toEqual(hint);
    expect(() => journal.save({ ...hint, assetHintId: "other" } as RecoveryRecord)).toThrow(
      "changed",
    );
    expect(() => journal.save(old)).toThrow("changed");
    const confirmed = {
      ...hint,
      pending: "validation",
      assetId: "asset-original",
      workspaceId: "workspace-default",
      assetMediaType: tts ? "text/plain" : "audio/wav",
      assetBytes: 9,
      assetSha256: `sha256:${"a".repeat(64)}`,
    } as RecoveryRecord;
    journal.save(confirmed);
    const jobRecord = {
      version: tts ? 2 : 1,
      model: old.model,
      assetId: "asset-original",
      workspaceId: "workspace-default",
      createKey: old.createKey,
      cancelKey: old.cancelKey,
      jobId: null,
      pending: "create",
      operationId: null,
      cursor: null,
      ...(tts
        ? {
            voiceId: "voice-demo",
            voiceRevision: "1",
            profileId: "profile-demo",
            profileRevision: "2",
          }
        : {}),
    } as RecoveryRecord;
    journal.save(jobRecord);
    expect(journal.load()).toEqual(jobRecord);
  });
  it("rejects mismatched confirmation, invalid hint and unknown fields without overwriting", () => {
    const s = storage();
    const journal = new SessionTaskJournal(s, "task");
    const old = uploadRecord(tts);
    journal.save(old);
    const hint = { ...old, version: tts ? 6 : 5, assetHintId: "asset-original" } as RecoveryRecord;
    journal.save(hint);
    const raw = s.getItem("task");
    for (const row of [
      { ...hint, assetHintId: "../bad" },
      { ...hint, content: "synthetic" },
      {
        ...hint,
        pending: "validation",
        assetId: "other",
        workspaceId: "workspace-default",
        assetMediaType: "text/plain",
        assetBytes: 9,
        assetSha256: `sha256:${"a".repeat(64)}`,
      },
    ])
      expect(() => journal.save(row as RecoveryRecord)).toThrow();
    expect(s.getItem("task")).toBe(raw);
  });
});
