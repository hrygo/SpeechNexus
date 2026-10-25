// @vitest-environment node
import { describe, expect, it } from "vitest";
import { FileTaskSession } from "./file-task-session";
import { SpeechFileClient } from "./speech-file-client";
import { createDemoTransport } from "./dev/demo-transport";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";

function fixture(scenario: Parameters<typeof createDemoTransport>[0] = "success") {
  const transport = createDemoTransport(scenario);
  const session = new FileTaskSession(new SpeechFileClient("http://127.0.0.1", transport.fetch));
  const file = new File([new Uint8Array([1, 2, 3])], "synthetic.wav");
  return { session, transport, file };
}

describe("file task recovery", () => {
  it("replays the frozen create payload even after the asset view changes", async () => {
    const transport = createDemoTransport("create-unknown");
    const bodies: string[] = [];
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      if (new URL(request.url).pathname === "/v1/nexus/assets/asset-demo") {
        const value = fixtures.examples.find((item) => item.id === "ready-audio")!.value;
        return Response.json({ ...value, status: "expired" });
      }
      if (new URL(request.url).pathname === "/v1/nexus/jobs")
        bodies.push(await request.clone().text());
      return transport.fetch(request);
    });
    const session = new FileTaskSession(client);
    await session.start(new File(["synthetic"], "synthetic.wav"), "asr-quality");
    await session.refresh();
    expect(session.getSnapshot().asset?.status).toBe("expired");
    await session.replay();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    session.dispose();
  });

  it("retains recovery identity and queries the original operation without creating another job", async () => {
    const transport = createDemoTransport("success");
    let creates = 0;
    let operationReads = 0;
    const client = new SpeechFileClient("http://127.0.0.1", async (input, init) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const path = new URL(request.url).pathname;
      if (path === "/v1/nexus/jobs") {
        creates++;
        return Response.json(
          fixtures.examples.find((item) => item.id === "recover-operation")!.value,
          { status: 409 },
        );
      }
      if (path === "/v1/nexus/operations/op-demo") {
        operationReads++;
        return Response.json(fixtures.examples.find((item) => item.id === "operation")!.value);
      }
      return transport.fetch(request);
    });
    const session = new FileTaskSession(client);
    await session.start(new File(["synthetic"], "synthetic.wav"), "asr-quality");
    expect(session.getSnapshot().unknown).toBe(true);
    await session.refresh();
    expect(operationReads).toBe(1);
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    await session.replay();
    expect(creates).toBe(1);
    session.dispose();
  });
  it("replays the original create intent after an unknown outcome without a second execution", async () => {
    const { session, transport, file } = fixture("create-unknown");
    await session.start(file, "asr-quality");
    expect(session.getSnapshot().unknown).toBe(true);
    expect(transport.stats.executions).toBe(1);
    await session.replay();
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    expect(session.getSnapshot().unknown).toBe(false);
    expect(transport.stats.executions).toBe(1);
    expect(transport.stats.createKeys[0]).toBe(transport.stats.createKeys[1]);
    session.dispose();
  });

  it("keeps cancellation and resource release separate, including a lost cancellation response", async () => {
    const { session, file } = fixture("cancel-unknown");
    await session.start(file, "asr-quality");
    await session.cancel();
    expect(session.getSnapshot().unknown).toBe(true);
    expect(session.getSnapshot().job?.cleanup_state).toBe("pending");
    await session.refresh();
    expect(session.getSnapshot().job?.status).toBe("cancelling");
    await session.replay();
    expect(session.getSnapshot().cancellation?.accepted).toBe(true);
    expect(session.getSnapshot().job?.cleanup_state).toBe("pending");
    session.dispose();
  });

  it("resumes SSE with exact decimal cursors and re-reads state after expired history", async () => {
    const { session, transport, file } = fixture("history-expired");
    await session.start(file, "asr-quality");
    await session.reconnect();
    expect(session.getSnapshot().stream).toBe("history-expired");
    expect(session.getSnapshot().notice).toContain("重新读取");
    await session.reconnect();
    expect(session.getSnapshot().cursor).toBe("9007199254740993");
    expect(session.getSnapshot().job?.status).toBe("succeeded");
    await session.reconnect();
    expect(transport.stats.lastEventIds.at(-1)).toBe("9007199254740993");
    session.dispose();
  });

  it("shows conflict and failed jobs without automatic resubmission", async () => {
    const conflict = fixture("conflict");
    await conflict.session.start(conflict.file, "asr-quality");
    expect(conflict.session.getSnapshot().notice).toContain("idempotency_conflict");
    expect(conflict.session.getSnapshot().unknown).toBe(false);
    expect(conflict.transport.stats.createKeys).toHaveLength(1);
    conflict.session.dispose();
    const failed = fixture("failed");
    await failed.session.start(failed.file, "asr-quality");
    await failed.session.reconnect();
    expect(failed.session.getSnapshot().job?.status).toBe("failed");
    expect(failed.transport.stats.executions).toBe(1);
    failed.session.dispose();
  });

  it("surfaces artifact expiry and never returns stale cached content", async () => {
    const { session, file } = fixture("artifact-expired");
    await session.start(file, "asr-quality");
    await session.reconnect();
    expect(await session.download("artifact-demo")).toBeUndefined();
    expect(session.getSnapshot().notice).toContain("artifact_expired");
    session.dispose();
  });
});
