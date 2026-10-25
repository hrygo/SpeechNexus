import { describe, expect, it, vi } from "vitest";
import { WorkspaceSession } from "./workspace-session";
import { WorkspaceReadError, type Workspace, type WorkspaceApi } from "./workspace-client";
const item = (id = "workspace-one"): Workspace => ({
  id,
  object: "nexus.workspace",
  display_name: "共享😀",
  status: "active",
  metadata_revision: 1,
  policy_revision: "1",
  created_at: "2026-10-24T00:00:00Z",
});
const page = (next: string | null = null) => ({
  object: "nexus.list" as const,
  data: [item()],
  has_more: next !== null,
  next_cursor: next,
});
function fixture() {
  const api = {
    list: vi.fn<WorkspaceApi["list"]>().mockResolvedValue(page()),
    get: vi.fn<WorkspaceApi["get"]>().mockResolvedValue(item()),
  };
  return { api, c: new WorkspaceSession(api) };
}
function pending<T>() {
  let resolve!: (x: T) => void;
  return {
    promise: new Promise<T>((r) => {
      resolve = r;
    }),
    resolve: (x: T) => resolve(x),
  };
}
describe("workspace read session", () => {
  it.each(["a", "b", "terminal-marker"])(
    "shows a final page regardless of terminal cursor %s",
    async (cursor) => {
      const f = fixture();
      f.api.list
        .mockResolvedValueOnce(page("a"))
        .mockResolvedValueOnce(page("b"))
        .mockResolvedValueOnce({ ...page(), data: [item("workspace-final")], next_cursor: cursor });
      await f.c.list();
      await f.c.list("a");
      await f.c.list("b");
      expect(f.c.snapshot().pageCurrent).toBe(true);
      expect(f.c.snapshot().page?.data[0]?.id).toBe("workspace-final");
      expect(f.c.snapshot().error).toBe(null);
      expect(f.api.list).toHaveBeenCalledTimes(3);
    },
  );
  it("does not fetch on construction and freezes snapshots", async () => {
    const f = fixture();
    expect(f.api.list).not.toHaveBeenCalled();
    await f.c.list();
    expect(f.c.snapshot().pageCurrent).toBe(true);
    expect(() => {
      f.c.snapshot().page!.data[0]!.display_name = "changed";
    }).toThrow();
  });
  it("retains historical page on failure, retries exactly the failed cursor", async () => {
    const f = fixture();
    f.api.list.mockResolvedValueOnce(page("cursor-a")).mockRejectedValueOnce(Error("PRIVATE"));
    await f.c.list();
    await f.c.list("cursor-a");
    expect(f.c.snapshot().page?.data).toHaveLength(1);
    expect(f.c.snapshot().pageCurrent).toBe(false);
    expect(f.c.snapshot().error).toBe("workspace_read_unavailable");
    await f.c.retry();
    expect(f.api.list.mock.calls[2]![0]).toBe("cursor-a");
    expect(f.c.snapshot().pageCurrent).toBe(true);
  });
  it("rejects cursor cycles and resets traversal on a first-page read", async () => {
    const f = fixture();
    f.api.list
      .mockResolvedValueOnce(page("a"))
      .mockResolvedValueOnce(page("b"))
      .mockResolvedValueOnce(page("a"));
    await f.c.list();
    await f.c.list("a");
    await f.c.list("b");
    expect(f.c.snapshot().pageCurrent).toBe(false);
    f.api.list.mockResolvedValue(page("a"));
    await f.c.list();
    expect(f.c.snapshot().pageCurrent).toBe(true);
  });
  it("late detail cannot overwrite a newer selected workspace", async () => {
    const f = fixture(),
      old = pending<Workspace>();
    f.api.get.mockReturnValueOnce(old.promise).mockResolvedValueOnce(item("workspace-two"));
    const a = f.c.get("workspace-one");
    await f.c.get("workspace-two");
    old.resolve(item());
    await a;
    expect(f.c.snapshot().selectedId).toBe("workspace-two");
    expect(f.c.snapshot().detail?.id).toBe("workspace-two");
    expect(f.api.get.mock.calls[0]![1]?.aborted).toBe(true);
  });
  it("reading a list invalidates an outstanding detail", async () => {
    const f = fixture(),
      old = pending<Workspace>();
    f.api.get.mockReturnValueOnce(old.promise);
    const a = f.c.get("workspace-one");
    await f.c.list();
    old.resolve(item());
    await a;
    expect(f.c.snapshot().detail).toBe(null);
    expect(f.c.snapshot().selectedId).toBe(null);
  });
  it.each(["workspace_not_found", "service_unavailable"])(
    "failed selected detail clears the previous object: %s",
    async (code) => {
      const f = fixture();
      await f.c.get("workspace-one");
      f.api.get.mockRejectedValueOnce(new WorkspaceReadError(code));
      await f.c.get("workspace-two");
      expect(f.c.snapshot().detail).toBe(null);
      expect(f.c.snapshot().error).toBe(code);
      expect(f.c.snapshot().detailCurrent).toBe(false);
      expect(f.api.get).toHaveBeenCalledTimes(2);
    },
  );
  it("validates injected API identity and pages too", async () => {
    const f = fixture();
    await f.c.get("foreign");
    expect(f.c.snapshot().detail).toBe(null);
    f.api.list.mockResolvedValue({ ...page(), data: [item(), item()] });
    await f.c.list();
    expect(f.c.snapshot().page).toBe(null);
  });
  it("hiding aborts, invalidates current snapshots and ignores late replies without auto refresh", async () => {
    const f = fixture(),
      old = pending<Workspace>();
    await f.c.list();
    f.api.get.mockReturnValueOnce(old.promise);
    const a = f.c.get("workspace-one");
    f.c.setActive(false);
    old.resolve(item());
    await a;
    expect(f.c.snapshot().detail).toBe(null);
    expect(f.c.snapshot().pageCurrent).toBe(false);
    f.c.setActive(true);
    expect(f.api.list).toHaveBeenCalledTimes(1);
    await f.c.retry();
    expect(f.api.get).toHaveBeenCalledTimes(2);
  });
  it("dispose blocks every future read", async () => {
    const f = fixture();
    f.c.dispose();
    await f.c.list();
    await f.c.get("workspace-one");
    await f.c.retry();
    expect(f.api.list).not.toHaveBeenCalled();
    expect(f.api.get).not.toHaveBeenCalled();
  });
});
