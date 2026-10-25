// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal, jobCommand, type TaskRecord, type TtsSelection } from "./task-journal";
import { createTtsDemoTransport } from "./dev/tts-demo-transport";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

const selection = (): TtsSelection => ({
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
});
function record(): TaskRecord {
  return {
    ...selection(),
    version: 2,
    assetId: "asset-text",
    workspaceId: "ws-default",
    createKey: crypto.randomUUID(),
    cancelKey: crypto.randomUUID(),
    jobId: null,
    pending: "create",
    operationId: null,
    cursor: null,
  };
}
function setup(scenario: Parameters<typeof createTtsDemoTransport>[0] = "success") {
  const transport = createTtsDemoTransport(scenario);
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const client = new SpeechFileClient("http://127.0.0.1", transport.fetch);
  const make = () => new FileTaskSession(client, new SessionTaskJournal(storage, "tts"));
  const read = () => new SessionTaskJournal(storage, "tts").load()!;
  return { transport, storage, data, client, make, read };
}

describe("fixed text synthesis intent", () => {
  function stagedSession(kind: "tts" | "asr", change: Record<string, unknown>) {
    const test = setup();
    const audio = fixtures.examples.find((entry) => entry.id === "ready-audio")!.value;
    const asset = parse("Asset", {
      ...audio,
      ...(kind === "tts"
        ? {
            purpose: "text_input",
            media_type: "text/plain",
            duration_ms: null,
            sample_rate_hz: null,
            channels: null,
          }
        : {}),
      status: "staging",
    });
    const ready = parse("Asset", { ...asset, ...change, status: "ready" });
    const commands: ReturnType<typeof parse<"JobCreateRequest">>[] = [];
    const client = new SpeechFileClient("http://127.0.0.1", async (input) => {
      const request = input as Request;
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/assets") return Response.json(asset, { status: 201 });
      if (path === "/v1/nexus/assets/asset-demo") return Response.json(ready);
      if (path === "/v1/nexus/jobs") {
        const command = parse("JobCreateRequest", await request.json());
        commands.push(command);
        return Response.json(
          parse("Job", {
            ...fixtures.examples.find((e) => e.id === "queued-job")!.value,
            kind,
            workspace_id: command.workspace_id,
            ...(command.kind === "tts"
              ? {
                  effective_configuration: {
                    models: { tts: { id: "model-tts-fixed", revision: "1" } },
                    voice: command.voice,
                    profile: command.profile ?? null,
                    policy_revision: "1",
                    catalog_revision: "1",
                  },
                }
              : {}),
          }),
          { status: 202 },
        );
      }
      throw new Error("Unexpected request");
    });
    const session = new FileTaskSession(client, new SessionTaskJournal(test.storage, "tts"));
    const start = () =>
      kind === "tts"
        ? session.startText("Synthetic test.", selection())
        : session.start(new File(["synthetic audio"], "synthetic.wav"), "asr-quality");
    return { ...test, session, start, commands };
  }

  it.each(["tts", "asr"] as const)(
    "allows %s validation readiness without changing the uploaded asset identity",
    async (kind) => {
      const test = stagedSession(kind, {});
      await test.start();
      expect(test.commands).toHaveLength(0);
      await test.session.refresh();
      await test.session.continueAfterValidation();
      expect(test.commands).toHaveLength(1);
      expect(test.commands[0]).toMatchObject({ kind, workspace_id: "ws-default" });
      expect(test.session.getSnapshot().job).toMatchObject({ kind, workspace_id: "ws-default" });
      test.session.dispose();
    },
  );

  for (const kind of ["tts", "asr"] as const) {
    it.each([
      ["workspace", { workspace_id: "ws-other" }],
      [
        "purpose",
        { purpose: "voice_reference", duration_ms: 1000, sample_rate_hz: 24000, channels: 1 },
      ],
      ["media type", { media_type: "application/octet-stream" }],
      ["size", { bytes: 4097 }],
      ["content digest", { sha256: "sha256:" + "b".repeat(64) }],
    ] as const)(
      `rejects changed %s on a staged ${kind} asset before creating any job`,
      async (_label, change) => {
        const test = stagedSession(kind, change);
        await test.start();
        const saved = test.data.get("tts");
        await test.session.refresh();
        await test.session.continueAfterValidation();
        expect(test.commands).toHaveLength(0);
        expect(test.session.getSnapshot().asset).toMatchObject({
          workspace_id: "ws-default",
          status: "staging",
        });
        expect(test.session.getSnapshot().job).toBeUndefined();
        expect(test.session.getSnapshot().unknown).toBe(false);
        expect(test.data.get("tts")).toBe(saved);
        expect(test.read()).toMatchObject({ pending: "validation", workspaceId: "ws-default" });
        test.session.dispose();
      },
    );
  }

  it("roundtrips v2 metadata and reconstructs the exact text/voice/profile references", () => {
    const test = setup();
    const journal = new SessionTaskJournal(test.storage, "tts");
    const original = record();
    journal.save(original);
    expect(test.read()).toEqual(original);
    expect(jobCommand(test.read())).toEqual({
      kind: "tts",
      model: "tts-quality",
      workspace_id: "ws-default",
      input: { text_asset_id: "asset-text" },
      voice: { id: "voice-demo", revision: "1" },
      profile: { id: "profile-demo", revision: "2" },
      output: { audio_format: "wav", save_artifacts: true },
    });
    expect(test.data.get("tts")).not.toContain('"text":');
  });

  it.each(["voiceRevision", "profileRevision", "assetId"] as const)(
    "rejects changing pinned %s",
    (field) => {
      const test = setup();
      const journal = new SessionTaskJournal(test.storage, "tts");
      const original = record();
      journal.save(original);
      expect(() => journal.save({ ...original, [field]: "9" })).toThrow();
      expect(test.read()).toEqual(original);
    },
  );

  it.each(["latest", "0", "10000000000000000000"])(
    "rejects invalid voice revision %s through A validation",
    (revision) => {
      expect(() => jobCommand({ ...record(), voiceRevision: revision } as TaskRecord)).toThrow();
    },
  );

  it("uploads exact UTF-8 text in a text_input multipart file", async () => {
    const test = setup();
    const client = new SpeechFileClient("http://127.0.0.1", async (input) => {
      const request = input as Request;
      const form = await request.clone().formData();
      expect(form.get("purpose")).toBe("text_input");
      const file = form.get("file") as File;
      expect(file.type).toBe("text/plain");
      expect(await file.text()).toBe("合成文本。\nSecond line.");
      return test.transport.fetch(request);
    });
    await expect(client.uploadText("合成文本。\nSecond line.", "text-key")).resolves.toMatchObject({
      purpose: "text_input",
      status: "ready",
    });
  });

  it("restores unknown TTS with original key/body/asset/revisions and no text in storage", async () => {
    const test = setup("create-unknown");
    const first = test.make();
    const selected = selection();
    await first.startText("合成演练，不是真实书稿。", selected);
    selected.voiceRevision = "9";
    selected.profileRevision = "8";
    await first.startText("another intent", selection());
    expect(first.getSnapshot().unknown).toBe(true);
    first.dispose();
    const saved = test.data.get("tts")!;
    expect(saved).not.toMatch(/合成演练|another intent|"text":/);
    const second = test.make();
    await second.restore();
    expect(test.transport.stats.createKeys).toHaveLength(1);
    await second.replay();
    expect(test.transport.stats.createKeys).toHaveLength(2);
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.transport.stats.bodies[1]).toBe(test.transport.stats.bodies[0]);
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().job?.kind).toBe("tts");
    expect(test.read().pending).toBeNull();
    second.dispose();
  });

  it("restores TTS cancellation with the original key and keeps cleanup pending", async () => {
    const test = setup("cancel-unknown");
    const first = test.make();
    await first.startText("Synthetic test.", selection());
    await first.cancel();
    first.dispose();
    const second = test.make();
    await second.restore();
    expect(second.getSnapshot().unknown).toBe(true);
    await second.replay();
    expect(new Set(test.transport.stats.cancelKeys).size).toBe(1);
    expect(test.transport.stats.cancelKeys).toHaveLength(2);
    expect(second.getSnapshot().cancellation?.accepted).toBe(true);
    expect(second.getSnapshot().job?.cleanup_state).toBe("pending");
    second.dispose();
  });

  it("restores confirmed TTS event cursor and downloads only its audio artifact", async () => {
    const test = setup();
    const first = test.make();
    await first.startText("Synthetic test.", selection());
    await first.reconnect();
    const result = await first.download("artifact-audio");
    expect(result?.blob.type).toBe("audio/wav");
    expect(new TextDecoder().decode((await result!.blob.arrayBuffer()).slice(0, 4))).toBe("RIFF");
    first.dispose();
    const second = test.make();
    await second.restore();
    await second.reconnect();
    expect(test.transport.stats.lastEventIds.at(-1)).toBe("9007199254740993");
    expect(test.transport.stats.createKeys).toHaveLength(1);
    second.dispose();
  });

  it("rejects invalid selections before uploading or storing text", async () => {
    const test = setup();
    const first = test.make();
    await first.startText("private", { ...selection(), voiceRevision: "latest" });
    expect(test.transport.stats.uploads).toBe(0);
    expect(test.transport.stats.createKeys).toHaveLength(0);
    expect(test.data.size).toBe(0);
    expect(first.getSnapshot().notice).not.toContain("private");
    expect(first.getSnapshot().unknown).toBe(false);
    expect(first.getSnapshot().canReplay).toBe(false);
    first.dispose();
  });

  it("rejects text that would be changed by UTF-8 encoding before upload", async () => {
    const test = setup();
    const first = test.make();
    await first.startText("broken\ud800text", selection());
    expect(test.transport.stats.uploads).toBe(0);
    expect(test.transport.stats.createKeys).toHaveLength(0);
    expect(first.getSnapshot().unknown).toBe(false);
    expect(test.data.size).toBe(0);
    first.dispose();
  });

  it("does not confirm an ASR response as a TTS job", async () => {
    const test = setup();
    const client = new SpeechFileClient("http://127.0.0.1", async (input) => {
      if (new URL((input as Request).url).pathname === "/v1/nexus/jobs")
        return Response.json(
          parse("Job", fixtures.examples.find((e) => e.id === "queued-job")!.value),
        );
      return test.transport.fetch(input);
    });
    const first = new FileTaskSession(client, new SessionTaskJournal(test.storage, "tts"));
    await first.startText("Synthetic test.", selection());
    expect(first.getSnapshot().job).toBeUndefined();
    expect(first.getSnapshot().unknown).toBe(true);
    expect(test.read().pending).toBe("create");
    first.dispose();
  });

  it("blocks TTS creation when journal storage fails", async () => {
    const test = setup();
    test.storage.setItem = () => {
      throw new Error("private");
    };
    const first = test.make();
    await first.startText("Synthetic test.", selection());
    expect(test.transport.stats.createKeys).toHaveLength(0);
    expect(first.getSnapshot().notice).not.toContain("private");
    first.dispose();
  });
});
