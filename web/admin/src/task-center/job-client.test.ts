// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { AdminJobClient } from "./job-client";

const respond = (value: unknown, init?: ResponseInit) =>
  Promise.resolve(Response.json(value, init));
const sample = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);

describe("Admin public job client", () => {
  it("bounds a stalled request and propagates deadline abort without retry", async () => {
    const deadline = new AbortController();
    const timer = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    let request: Request | undefined;
    const client = new AdminJobClient("http://127.0.0.1", (input, init) => {
      request = new Request(input, init);
      return new Promise<Response>((_resolve, reject) => {
        request!.signal.addEventListener("abort", () => reject(new Error("aborted")), {
          once: true,
        });
      });
    });
    const pending = client.cancel("job-demo", "original-key");
    // openapi-fetch dispatches on the next microtask.
    await Promise.resolve();
    expect(timer).toHaveBeenCalledWith(15_000);
    deadline.abort();
    await expect(pending).rejects.toThrow("aborted");
    expect(request?.signal.aborted).toBe(true);
  });

  it("uses the generated routes, opaque cursor and explicit redirect rejection", async () => {
    const requests: Request[] = [];
    const client = new AdminJobClient("http://127.0.0.1", (input, init) => {
      const request = new Request(input, init);
      requests.push(request);
      const path = new URL(request.url).pathname;
      if (path.endsWith("cancellations")) return respond(sample("cancel-job"));
      if (path.endsWith("job-demo")) return respond(sample("queued-job"));
      return respond({
        object: "nexus.list",
        data: [sample("queued-job")],
        has_more: false,
        next_cursor: null,
      });
    });
    expect((await client.list("opaque:page-2")).data).toHaveLength(1);
    expect((await client.getJob("job-demo")).id).toBe("job-demo");
    expect((await client.cancel("job-demo", "original-key")).accepted).toBe(true);
    expect(new URL(requests[0]!.url).searchParams.get("cursor")).toBe("opaque:page-2");
    expect(new URL(requests[0]!.url).searchParams.get("limit")).toBe("50");
    expect(requests[2]!.headers.get("Idempotency-Key")).toBe("original-key");
    expect(requests.every((r) => r.redirect === "error")).toBe(true);
  });

  it("rejects mismatched identities and invalid generated responses", async () => {
    const client = new AdminJobClient("http://127.0.0.1", () => respond(sample("queued-job")));
    await expect(client.getJob("another-job")).rejects.toThrow("resource mismatch");
    await expect(client.list()).rejects.toThrow();
    const cancellation = new AdminJobClient("http://127.0.0.1", () =>
      respond(sample("cancel-job")),
    );
    await expect(cancellation.cancel("another-job", "original-key")).rejects.toThrow(
      "resource mismatch",
    );
  });

  it("rejects duplicate identities and unusable pagination", async () => {
    for (const body of [
      {
        object: "nexus.list",
        data: [sample("queued-job"), sample("queued-job")],
        has_more: false,
        next_cursor: null,
      },
      { object: "nexus.list", data: [], has_more: true, next_cursor: null },
    ]) {
      const client = new AdminJobClient("http://127.0.0.1", () => respond(body));
      await expect(client.list()).rejects.toThrow();
    }
  });

  it("does not retry a lost POST or expose response content in its error", async () => {
    let calls = 0;
    const client = new AdminJobClient("http://127.0.0.1", () => {
      calls++;
      return Promise.reject(new TypeError("synthetic-sensitive-value"));
    });
    await expect(client.cancel("job-demo", "original-key")).rejects.toThrow();
    expect(calls).toBe(1);
    const failure = new AdminJobClient("http://127.0.0.1", () =>
      respond(sample("recover-operation"), { status: 409 }),
    );
    await expect(failure.list()).rejects.not.toThrow("Synthetic");
  });
});
