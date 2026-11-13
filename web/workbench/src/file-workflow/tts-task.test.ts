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
    expect(jobCommand(test.read(), "合成文本。")).toEqual({
      kind: "tts",
      model: "tts-quality",
      workspace_id: "ws-default",
      input: { text: "合成文本。" },
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
      expect(() =>
        jobCommand({ ...record(), voiceRevision: revision } as TaskRecord, "合成文本。"),
      ).toThrow();
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

const sentinel = "哨兵-685-SENTINEL-正文不得入库";

function contractBody(text: string) {
  return {
    kind: "tts",
    model: selection().model,
    workspace_id: "ws-default",
    input: { text },
    voice: { id: selection().voiceId, revision: selection().voiceRevision },
    profile: { id: selection().profileId, revision: selection().profileRevision },
    output: { audio_format: "wav", save_artifacts: true },
  };
}

function failure(code: string, status: number) {
  const envelope = structuredClone(
    fixtures.examples.find((entry) => entry.id === "recover-operation")!.value,
  ) as { error: { code: string } };
  envelope.error.code = code;
  return Response.json(parse("ErrorResponse", envelope), { status });
}

/** Mirrors public_job_admission: `text_asset_id` is a 422 until the frozen text material exists. */
function admitting(test: ReturnType<typeof setup>) {
  const bodies: { input?: Record<string, unknown> }[] = [];
  const statuses: number[] = [];
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const isJobCreate =
      new URL(request.url).pathname === "/v1/nexus/jobs" && request.method === "POST";
    if (isJobCreate) {
      const body = (await request.clone().json()) as { input?: Record<string, unknown> };
      bodies.push(body);
      if (body.input && Object.hasOwn(body.input, "text_asset_id")) {
        statuses.push(422);
        return failure("job_text_asset_unsupported", 422);
      }
    }
    const response = await test.transport.fetch(request);
    if (isJobCreate) statuses.push(response.status);
    return response;
  });
  return { client, bodies, statuses };
}

describe("workbench TTS submit shape (#685)", () => {
  it("submits the contract-supported inline text that admission accepts", async () => {
    const test = setup();
    const admission = admitting(test);
    const first = new FileTaskSession(
      admission.client,
      new SessionTaskJournal(test.storage, "tts"),
    );
    await first.startText(sentinel, selection());
    const body = admission.bodies[0]!;
    // Rollback guard: this is exactly the field the deployment answers 422 for.
    expect(body.input).toEqual({ text: sentinel });
    expect(admission.statuses).toEqual([202]);
    expect(first.getSnapshot().job?.id).toBe("job-demo");
    expect(test.transport.stats.executions).toBe(1);
    first.dispose();
  });

  it("keeps the sentinel body out of every durable journal record it replays", async () => {
    const test = setup("create-unknown");
    const first = test.make();
    await first.startText(sentinel, selection());
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    const saved = test.data.get("tts")!;
    expect(saved).not.toContain(sentinel);
    expect(saved).not.toContain("正文不得入库");
    expect(Object.keys(test.read())).not.toContain("text");
    const firstBody = parse("JobCreateRequest", JSON.parse(test.transport.stats.bodies[0]!));
    expect("text" in firstBody.input ? firstBody.input.text : undefined).toBe(sentinel);
    first.dispose();
    const second = test.make();
    await second.restore();
    expect(test.data.get("tts")).toBe(saved);
    await second.replay();
    // The replay body is reconstructed from the immutable text asset, not local storage.
    expect(test.transport.stats.bodies[1]).toBe(test.transport.stats.bodies[0]);
    expect(test.data.get("tts")).not.toContain(sentinel);
    expect(test.read().pending).toBeNull();
    second.dispose();
  });

  it("accepts the inline shape at the type layer and rejects shapes the contract excludes", () => {
    const legal = contractBody(sentinel);
    expect(parse("JobCreateRequest", legal)).toEqual(legal);
    for (const broken of [
      { ...legal, input: {} },
      { ...legal, input: { text: "" } },
      { ...legal, input: { text: sentinel, text_asset_id: "asset-text" } },
      { ...legal, input: { text: "x".repeat(200001) } },
      { ...legal, voice: undefined },
    ])
      expect(() => parse("JobCreateRequest", broken)).toThrow();
    // Declared on purpose until the frozen text-asset material exists: the type layer keeps
    // the branch, while admission rejects it with 422 (contracts/speech-openapi.yaml:826-828).
    expect(
      parse("JobCreateRequest", { ...legal, input: { text_asset_id: "asset-text" } }),
    ).toBeTruthy();
  });

  it("rejects a legacy text_asset_id submit exactly like the deployment", async () => {
    const test = setup();
    const admission = admitting(test);
    const legacy = { ...contractBody(sentinel), input: { text_asset_id: "asset-text" } };
    await expect(admission.client.create(legacy, crypto.randomUUID())).rejects.toMatchObject({
      status: 422,
      code: "job_text_asset_unsupported",
    });
  });

  it("keeps a lost create replayable only under its original key and identical body", async () => {
    const test = setup("create-unknown");
    const first = test.make();
    await first.startText(sentinel, selection());
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    first.dispose();
    const second = test.make();
    await second.restore();
    expect(test.transport.stats.createKeys).toHaveLength(1);
    await second.replay();
    expect(test.transport.stats.createKeys).toHaveLength(2);
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.transport.stats.bodies[1]).toBe(test.transport.stats.bodies[0]);
    expect(test.transport.stats.executions).toBe(1);
    expect(test.read().pending).toBeNull();
    second.dispose();
  });

  it("never replays a create that returned an identified original operation", async () => {
    const test = setup();
    let creates = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (new URL(request.url).pathname === "/v1/nexus/jobs") {
        creates++;
        return failure("operation_in_progress", 409);
      }
      return test.transport.fetch(request, init);
    });
    const first = new FileTaskSession(client, new SessionTaskJournal(test.storage, "tts"));
    await first.startText(sentinel, selection());
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: false });
    expect(test.read().operationId).toBe("op-demo");
    await first.replay();
    expect(creates).toBe(1);
    expect(first.getSnapshot().canReplay).toBe(false);
    first.dispose();
  });
});

describe("workbench TTS text readback (#688 review M1)", () => {
  type AssetFault = "404" | "410" | "503" | "invalid-utf8" | "oversize";

  /** The readback is the only new network surface; every failure must keep the intent as-is. */
  function guarded(test: ReturnType<typeof setup>) {
    let fault: AssetFault | null = null;
    const creates: string[] = [];
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/assets/asset-text/content" && fault !== null) {
        if (fault === "404") return failure("asset_not_found", 404);
        if (fault === "410") return failure("asset_expired", 410);
        if (fault === "503") return failure("service_unavailable", 503);
        return new Response(
          fault === "invalid-utf8"
            ? // "fo" + 0x80 (continuation byte, never valid) + "o"
              new Uint8Array([0x66, 0x6f, 0x80, 0x6f])
            : new Uint8Array(4 * 1024 * 1024 + 1),
          { headers: { "content-type": "application/octet-stream" } },
        );
      }
      if (path === "/v1/nexus/jobs" && request.method === "POST")
        creates.push(await request.clone().text());
      return test.transport.fetch(request);
    });
    return {
      client,
      fail: (next: AssetFault | null) => {
        fault = next;
      },
      creates,
    };
  }

  /** Submit once, lose the create response, then restore the pending intent in a new session. */
  async function staged() {
    const test = setup("create-unknown");
    const guard = guarded(test);
    const first = new FileTaskSession(guard.client, new SessionTaskJournal(test.storage, "tts"));
    await first.startText(sentinel, selection());
    expect(first.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
    expect(guard.creates).toHaveLength(1);
    first.dispose();
    const saved = test.data.get("tts")!;
    const second = new FileTaskSession(guard.client, new SessionTaskJournal(test.storage, "tts"));
    await second.restore();
    return { test, guard, saved, second, originalBody: guard.creates[0]! };
  }

  it.each(["404", "410", "503", "invalid-utf8", "oversize"] as const)(
    "keeps the pending intent and sends nothing when readback fails with %s",
    async (fault) => {
      const { test, guard, saved, second } = await staged();
      guard.fail(fault);
      await second.replay();
      expect(guard.creates).toHaveLength(1);
      expect(test.data.get("tts")).toBe(saved);
      expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
      expect(second.getSnapshot().job).toBeUndefined();
      expect(second.getSnapshot().notice).toContain("原文本素材暂不可读取");
      second.dispose();
    },
  );

  it("replays the identical original body once the asset is readable again", async () => {
    const { test, guard, saved, second, originalBody } = await staged();
    guard.fail("503");
    await second.replay();
    expect(guard.creates).toHaveLength(1);
    expect(test.data.get("tts")).toBe(saved);
    guard.fail(null);
    await second.replay();
    expect(guard.creates).toHaveLength(2);
    expect(guard.creates[1]).toBe(originalBody);
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.transport.stats.bodies[1]).toBe(test.transport.stats.bodies[0]);
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    expect(test.read().pending).toBeNull();
    second.dispose();
  });

  it.each(["invalid-utf8", "oversize"] as const)(
    "never fabricates a body from %s content",
    async (fault) => {
      const { test, guard, saved, second, originalBody } = await staged();
      guard.fail(fault);
      await second.replay();
      expect(guard.creates).toHaveLength(1);
      expect(test.data.get("tts")).toBe(saved);
      expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: true });
      guard.fail(null);
      await second.replay();
      expect(guard.creates).toHaveLength(2);
      // Not a placeholder, empty string or truncated prefix: the exact original body.
      expect(guard.creates[1]).toBe(originalBody);
      const body = parse("JobCreateRequest", JSON.parse(guard.creates[1]!));
      expect("text" in body.input ? body.input.text : undefined).toBe(sentinel);
      expect(originalBody).not.toContain("\\u0000");
      second.dispose();
    },
  );
});
