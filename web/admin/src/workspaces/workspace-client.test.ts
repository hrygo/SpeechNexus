import { describe, expect, it, vi } from "vitest";
import { WorkspaceClient, WorkspaceReadError } from "./workspace-client";
const workspace = {
  id: "workspace-one",
  object: "nexus.workspace",
  display_name: "共享空间😀",
  status: "active",
  metadata_revision: 1,
  policy_revision: "1",
  created_at: "2026-10-24T00:00:00Z",
};
const page = () => ({
  object: "nexus.list",
  data: [workspace],
  has_more: false,
  next_cursor: null,
});
describe("generated workspace reads", () => {
  it("uses exact GETs, omitted defaults, redirect error/no-store and a bounded signal", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation((input) => {
      const r = new Request(input);
      expect(r.method).toBe("GET");
      expect(r.redirect).toBe("error");
      expect(r.cache).toBe("no-store");
      expect(r.signal.aborted).toBe(false);
      return Promise.resolve(Response.json(r.url.endsWith("/workspace-one") ? workspace : page()));
    });
    const c = new WorkspaceClient("https://deployment.invalid", fetch);
    await c.list();
    await c.get("workspace-one");
    expect(new Request(fetch.mock.calls[0]![0]).url).toBe(
      "https://deployment.invalid/v1/nexus/workspaces",
    );
    await c.list("cursor-original");
    expect(new URL(new Request(fetch.mock.calls[2]![0]).url).searchParams.get("cursor")).toBe(
      "cursor-original",
    );
  });
  it("rejects invalid ids before fetching", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>();
    const c = new WorkspaceClient("https://deployment.invalid", fetch);
    await expect(c.get("../foreign")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("returns only a stable error code from the generated envelope", async () => {
    const c = new WorkspaceClient("https://deployment.invalid", () =>
      Promise.resolve(
        Response.json(
          {
            error: {
              code: "workspace_not_found",
              type: "not_found_error",
              message: "PRIVATE_PROVIDER_BODY",
              param: null,
              request_id: "req-test",
              retryable: false,
            },
          },
          { status: 404 },
        ),
      ),
    );
    await expect(c.get("workspace-one")).rejects.toEqual(
      new WorkspaceReadError("workspace_not_found"),
    );
  });
  it.each(["duplicate", "no-cursor", "repeated-cursor", "invalid-dto"])(
    "rejects unusable list %s",
    async (mode) => {
      const value = page();
      if (mode === "duplicate") value.data.push(workspace);
      if (mode === "no-cursor") value.has_more = true;
      const body =
        mode === "repeated-cursor"
          ? { ...value, has_more: true, next_cursor: "same" }
          : mode === "invalid-dto"
            ? { ...value, arbitrary: true }
            : value;
      const c = new WorkspaceClient("https://deployment.invalid", () =>
        Promise.resolve(Response.json(body)),
      );
      await expect(c.list("same")).rejects.toThrow();
    },
  );
  it.each(["same", "previous", "terminal-marker"])(
    "accepts a generated-valid final page with cursor %s",
    async (cursor) => {
      const body = { ...page(), next_cursor: cursor };
      const c = new WorkspaceClient("https://deployment.invalid", () =>
        Promise.resolve(Response.json(body)),
      );
      await expect(c.list("same")).resolves.toEqual(body);
    },
  );
  it("rejects a different workspace and oversized JSON success", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(Response.json({ ...workspace, id: "foreign" }))
      .mockResolvedValueOnce(
        Response.json({ ...workspace, display_name: "x".repeat(1024 * 1024) }),
      );
    const c = new WorkspaceClient("https://deployment.invalid", fetch);
    await expect(c.get("workspace-one")).rejects.toThrow();
    await expect(c.get("workspace-one")).rejects.toThrow();
  });
  it("does not follow redirects or retry an unreadable response", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValue(
        new Response("", { status: 302, headers: { Location: "https://foreign.invalid" } }),
      );
    await expect(new WorkspaceClient("https://deployment.invalid", fetch).list()).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
