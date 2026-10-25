import { describe, expect, it, vi } from "vitest";
import { RealtimeClient, connectionUrl } from "./realtime-client";
import { request, session } from "./realtime-fixtures";

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
describe("fixed-origin realtime REST and socket boundary", () => {
  it("uses exact generated create input and original key without retries, rejects mismatched responses", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(response(session(), 201));
    const c = new RealtimeClient(fetch);
    expect(await c.create(request(), "original-key")).toEqual(session());
    const sent = fetch.mock.calls[0]![0] as Request;
    expect(sent.redirect).toBe("error");
    expect(new URL(sent.url).origin).toBe(location.origin);
    expect(sent.headers.get("Idempotency-Key")).toBe("original-key");
    expect(await sent.json()).toEqual(request());
    expect(fetch).toHaveBeenCalledTimes(1);
    await expect(
      new RealtimeClient(() =>
        Promise.resolve(
          response(
            {
              ...session(),
              effective_configuration: {
                ...session().effective_configuration,
                profile: { id: "foreign", revision: "1" },
              },
            },
            201,
          ),
        ),
      ).create(request(), "key"),
    ).rejects.toThrow();
  });
  it("checks GET/cancel identity and returns current cleanup independently of accepted mode", async () => {
    const active = { ...session(), status: "draining", cleanup_state: "pending" };
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(response(active, 202))
      .mockResolvedValueOnce(response({ ...session(), id: "other" }));
    const c = new RealtimeClient(fetch);
    expect((await c.cancel("session-demo", "abort", "cancel-key")).cleanup_state).toBe("pending");
    expect(await (fetch.mock.calls[0]![0] as Request).json()).toEqual({ mode: "abort" });
    await expect(c.current("session-demo")).rejects.toThrow();
  });
  it("rejects foreign/mixed/credentialed/extra-query socket URLs", () => {
    const ok = connectionUrl(session());
    expect(ok.pathname).toBe("/v1/realtime");
    expect(ok.searchParams.get("session_id")).toBe("session-demo");
    for (const url of [
      "wss://other.test/v1/realtime?session_id=session-demo",
      "/other?session_id=session-demo",
      "/v1/realtime?session_id=other",
      "/v1/realtime?session_id=session-demo&token=private",
      "/v1/realtime?session_id=session-demo&session_id=session-demo",
      "/v1/realtime?session_id=session-demo#fragment",
    ])
      expect(() => connectionUrl({ ...session(), connection_url: url })).toThrow();
  });
  it("rejects oversized or non-JSON HTTP bodies before DTO interpretation", async () => {
    const c = new RealtimeClient(() =>
      Promise.resolve(
        new Response("x", { status: 201, headers: { "Content-Type": "text/plain" } }),
      ),
    );
    await expect(c.create(request(), "key")).rejects.toThrow();
    const large = new RealtimeClient(() =>
      Promise.resolve(
        new Response("x", {
          status: 201,
          headers: {
            "Content-Type": "application/json",
            "Content-Length": String(9 * 1024 * 1024),
          },
        }),
      ),
    );
    await expect(large.create(request(), "key")).rejects.toThrow();
  });
});
