// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { AdminJobClient } from "./job-client";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const event = () => example("job.queued") as { id: string; type: string; job_id: string };
const collect = async (source: AsyncIterable<unknown>) => {
  const values = [];
  for await (const value of source) values.push(value);
  return values;
};
const sse = (body: string, type = "text/event-stream; charset=utf-8", status = 200) =>
  new Response(body, { status, headers: { "Content-Type": type } });
const frame = (value = event()) =>
  `id: ${value.id}\nevent: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`;

describe("Admin generated JobEvent consumer", () => {
  it("uses the public route and opaque large resume sequence, validates and yields events", async () => {
    let request: Request | undefined;
    const value = { ...event(), id: "9007199254740993" };
    const client = new AdminJobClient("http://127.0.0.1", (input, init) => {
      request = new Request(input, init);
      return Promise.resolve(sse(frame(value)));
    });
    expect(await collect(client.events(value.job_id, "9007199254740992"))).toEqual([value]);
    expect(request?.method).toBe("GET");
    expect(new URL(request!.url).pathname).toBe(`/v1/nexus/jobs/${value.job_id}/events`);
    expect(request?.headers.get("Last-Event-ID")).toBe("9007199254740992");
    expect(request?.redirect).toBe("error");
    expect(request?.cache).toBe("no-store");
  });

  it.each(["payload", "id", "type", "job", "mime", "status"])(
    "rejects invalid %s without yielding",
    async (kind) => {
      const value = event();
      let body = frame(value);
      if (kind === "payload") body = `id: 1\ndata: {"id":"1"}\n\n`;
      if (kind === "id") body = body.replace(`id: ${value.id}`, "id: 999");
      if (kind === "type") body = body.replace(`event: ${value.type}`, "event: job.failed");
      if (kind === "job") body = frame({ ...value, job_id: "another-job" });
      const client = new AdminJobClient("http://127.0.0.1", () =>
        Promise.resolve(
          sse(
            body,
            kind === "mime" ? "text/event-stream-invalid" : undefined,
            kind === "status" ? 206 : 200,
          ),
        ),
      );
      await expect(collect(client.events(value.job_id))).rejects.toThrow();
    },
  );

  it("exposes only safe error status/code and does not retry an expired history", async () => {
    let calls = 0;
    const error = example("recover-operation") as { error: Record<string, unknown> };
    error.error.code = "event_history_expired";
    error.error.message = "synthetic-sensitive-value";
    const client = new AdminJobClient("http://127.0.0.1", () => {
      calls++;
      return Promise.resolve(Response.json(error, { status: 410 }));
    });
    const pending = collect(client.events(event().job_id));
    await expect(pending).rejects.toMatchObject({ status: 410, code: "event_history_expired" });
    await expect(pending).rejects.not.toThrow("synthetic-sensitive-value");
    expect(calls).toBe(1);
  });

  it("cancels a pending stream read when the owner stops", async () => {
    let cancelled = false;
    const client = new AdminJobClient("http://127.0.0.1", () =>
      Promise.resolve(
        new Response(
          new ReadableStream({
            cancel() {
              cancelled = true;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      ),
    );
    const owner = new AbortController();
    const pending = collect(client.events(event().job_id, undefined, owner.signal));
    await vi.waitFor(() => expect(owner.signal.aborted).toBe(false));
    await new Promise((resolve) => setTimeout(resolve, 0));
    owner.abort();
    await expect(pending).rejects.toThrow();
    expect(cancelled).toBe(true);
  });

  it("times out only establishment and clears the timer once the response is established", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | undefined;
      const client = new AdminJobClient("http://127.0.0.1", (input, init) => {
        signal = new Request(input, init).signal;
        return Promise.resolve(
          new Response(new ReadableStream(), { headers: { "Content-Type": "text/event-stream" } }),
        );
      });
      const owner = new AbortController();
      const pending = collect(client.events(event().job_id, undefined, owner.signal));
      const settled = pending.catch(() => undefined);
      await vi.advanceTimersByTimeAsync(15_001);
      expect(signal?.aborted).toBe(false);
      expect(vi.getTimerCount()).toBe(0);
      owner.abort();
      await settled;
      const stalled = new AdminJobClient("http://127.0.0.1", (input, init) => {
        const request = new Request(input, init);
        return new Promise<Response>((_resolve, reject) =>
          request.signal.addEventListener("abort", () => reject(new Error("aborted")), {
            once: true,
          }),
        );
      });
      const failure = expect(collect(stalled.events(event().job_id))).rejects.toThrow("aborted");
      await vi.advanceTimersByTimeAsync(15_001);
      await failure;
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
