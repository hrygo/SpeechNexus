// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient, parse } from "./speech-file-client";
import { SessionTaskJournal } from "./task-journal";
import { createDemoTransport } from "./dev/demo-transport";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

function setup(scenario: Parameters<typeof createDemoTransport>[0] = "success") {
  const transport = createDemoTransport(scenario);
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const bodies: string[] = [];
  const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    if (new URL(request.url).pathname === "/v1/nexus/jobs" && request.method === "POST")
      bodies.push(await request.clone().text());
    return transport.fetch(request);
  });
  const session = () => new FileTaskSession(client, new SessionTaskJournal(storage, "task"));
  return {
    transport,
    bodies,
    read: () => new SessionTaskJournal(storage, "task").load()!,
    data,
    storage,
    client,
    session,
    file: new File(["synthetic audio"], "private-name.wav"),
  };
}

describe("durable file task identities", () => {
  it("keeps the confirmed cursor if storage fails while handling expired event history", async () => {
    const test = setup("history-expired");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    const before = test.data.get("task");
    test.storage.setItem = () => {
      throw new Error("private");
    };
    await expect(first.reconnect()).resolves.toBeUndefined();
    expect(test.data.get("task")).toBe(before);
    expect(first.getSnapshot().stream).toBe("interrupted");
    expect(first.getSnapshot().notice).not.toContain("private");
    first.dispose();
  });
  it("restores an unknown create without POST, then explicitly replays the same key/body/workspace", async () => {
    const test = setup("create-unknown");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    first.dispose();
    const saved = test.data.get("task")!;
    expect(saved).not.toMatch(/private-name|synthetic audio|transcript.*text/);
    const second = test.session();
    await second.restore();
    expect(test.transport.stats.createKeys).toHaveLength(1);
    expect(second.getSnapshot().canReplay).toBe(true);
    await second.replay();
    expect(test.transport.stats.createKeys).toHaveLength(2);
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.bodies[1]).toBe(test.bodies[0]);
    expect(parse("JobCreateRequest", JSON.parse(test.bodies[0]!) as unknown).workspace_id).toBe(
      "ws-default",
    );
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    const record = test.read();
    expect(record.workspaceId).toBe("ws-default");
    expect(record.pending).toBeNull();
    second.dispose();
  });

  it("restores cancellation against the original job/key without mistaking pending cleanup for release", async () => {
    const test = setup("cancel-unknown");
    const first = test.session();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    first.dispose();
    const before = test.read();
    const second = test.session();
    await second.restore();
    expect(second.getSnapshot().unknown).toBe(true);
    await second.replay();
    const after = test.read();
    expect(after.cancelKey).toBe(before.cancelKey);
    expect(after.jobId).toBe(before.jobId);
    expect(test.transport.stats.executions).toBe(1);
    expect(second.getSnapshot().cancellation?.accepted).toBe(true);
    expect(second.getSnapshot().job?.cleanup_state).toBe("pending");
    second.dispose();
  });

  it("reads an acknowledged job and resumes exact event cursors after reload", async () => {
    const test = setup();
    const first = test.session();
    await first.start(test.file, "asr-quality");
    await first.reconnect();
    first.dispose();
    const second = test.session();
    await second.restore();
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    expect(second.getSnapshot().cursor).toBe("9007199254740993");
    await second.reconnect();
    expect(test.transport.stats.lastEventIds.at(-1)).toBe("9007199254740993");
    expect(test.transport.stats.createKeys).toHaveLength(1);
    second.dispose();
  });

  it("does not POST when the initial journal write fails", async () => {
    const test = setup();
    test.storage.setItem = () => {
      throw new Error("private");
    };
    const first = test.session();
    await first.start(test.file, "asr-quality");
    expect(test.transport.stats.createKeys).toHaveLength(0);
    expect(first.getSnapshot().notice).not.toContain("private");
    first.dispose();
  });

  it("keeps original pending identity if storage fails after the server accepts create", async () => {
    const test = setup();
    const original = test.storage.setItem;
    test.storage.setItem = (key, value) => {
      const record = JSON.parse(value) as { pending: string | null };
      if (record.pending === null) throw new Error("private");
      original(key, value);
    };
    const first = test.session();
    await first.start(test.file, "asr-quality");
    first.dispose();
    expect(test.read().pending).toBe("create");
    test.storage.setItem = original;
    const second = test.session();
    await second.restore();
    await second.replay();
    expect(new Set(test.transport.stats.createKeys).size).toBe(1);
    expect(test.transport.stats.executions).toBe(1);
    second.dispose();
  });

  it("restores operation recovery identity using GET only", async () => {
    const test = setup();
    let creates = 0;
    let operations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/jobs") {
        creates++;
        return Response.json(fixtures.examples.find((e) => e.id === "recover-operation")!.value, {
          status: 409,
        });
      }
      if (path === "/v1/nexus/operations/op-demo") {
        operations++;
        return Response.json(fixtures.examples.find((e) => e.id === "operation")!.value);
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    first.dispose();
    const second = make();
    await second.restore();
    await second.replay();
    expect(creates).toBe(1);
    expect(operations).toBe(1);
    expect(second.getSnapshot().job?.id).toBe("job-demo");
    second.dispose();
  });

  it("keeps cancellation pending when operation recovery only reads its associated job", async () => {
    const test = setup();
    let cancellations = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (
        new URL(request.url).pathname === "/v1/nexus/jobs/job-demo/cancellations" &&
        request.method === "POST"
      ) {
        cancellations++;
        return Response.json(fixtures.examples.find((e) => e.id === "recover-operation")!.value, {
          status: 409,
        });
      }
      return test.transport.fetch(request);
    });
    const make = () => new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const first = make();
    await first.start(test.file, "asr-quality");
    await first.cancel();
    const before = test.read();
    expect(before.pending).toBe("cancel");
    expect(before.operationId).toBe("op-demo");
    await first.refresh();
    expect(test.read()).toEqual(before);
    expect(first.getSnapshot()).toMatchObject({
      unknown: true,
      canReplay: false,
      job: { id: "job-demo", status: "queued" },
    });
    expect(first.getSnapshot().cancellation).toBeUndefined();
    first.dispose();
    const second = make();
    await second.restore();
    await second.replay();
    expect(test.read()).toEqual(before);
    expect(second.getSnapshot()).toMatchObject({ unknown: true, canReplay: false });
    expect(second.getSnapshot().cancellation).toBeUndefined();
    expect(cancellations).toBe(1);
    expect(test.transport.stats.createKeys).toHaveLength(1);
    second.dispose();
  });

  it("blocks a stale controller from submitting a different intent", async () => {
    const test = setup();
    const first = test.session();
    const stale = test.session();
    await first.start(test.file, "asr-quality");
    await stale.start(test.file, "asr-other");
    expect(test.transport.stats.createKeys).toHaveLength(1);
    expect(test.read().model).toBe("asr-quality");
    first.dispose();
    stale.dispose();
  });

  it("does not write late acknowledged responses after disposal", async () => {
    const test = setup();
    let release: () => void = () => undefined;
    const wait = new Promise<void>((resolve) => {
      release = resolve;
    });
    let reached: () => void = () => undefined;
    const ready = new Promise<void>((resolve) => {
      reached = resolve;
    });
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const response = await test.transport.fetch(request);
      if (new URL(request.url).pathname === "/v1/nexus/jobs") {
        reached();
        await wait;
      }
      return response;
    });
    const first = new FileTaskSession(client, new SessionTaskJournal(test.storage, "task"));
    const start = first.start(test.file, "asr-quality");
    await ready;
    const before = test.data.get("task");
    first.dispose();
    release();
    await start;
    expect(test.data.get("task")).toBe(before);
    expect(test.read().pending).toBe("create");
  });
});
