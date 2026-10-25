import { expect, it, vi } from "vitest";
import { voiceTransport } from "./voice-transport";
it("bounds same-origin JSON and disables redirects", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(Response.json({ ok: true }));
  const send = voiceTransport("https://speech.invalid", fetch);
  expect(await (await send("https://speech.invalid/v1/nexus/jobs")).json()).toEqual({ ok: true });
  expect((fetch.mock.calls[0]![0] as Request).redirect).toBe("error");
  await expect(send("https://elsewhere.invalid/jobs")).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("rejects large and non-JSON replies", async () => {
  const fetch = vi
    .fn<typeof globalThis.fetch>()
    .mockResolvedValueOnce(new Response("x", { headers: { "Content-Type": "text/html" } }))
    .mockResolvedValueOnce(
      new Response("x".repeat(1048577), { headers: { "Content-Type": "application/json" } }),
    );
  const send = voiceTransport("https://speech.invalid", fetch);
  await expect(send("https://speech.invalid/jobs")).rejects.toThrow();
  await expect(send("https://speech.invalid/jobs")).rejects.toThrow();
});
it("cancels an unusable response body", async () => {
  const cancel = vi.fn();
  const fetch = vi
    .fn<typeof globalThis.fetch>()
    .mockResolvedValue(
      new Response(new ReadableStream({ cancel }), { headers: { "Content-Type": "text/html" } }),
    );
  await expect(
    voiceTransport("https://speech.invalid", fetch)("https://speech.invalid/jobs"),
  ).rejects.toThrow();
  expect(cancel).toHaveBeenCalledTimes(1);
});
