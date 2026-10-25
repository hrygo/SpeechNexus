import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { WorkspaceHost } from "./WorkspaceHost";
import { WorkspaceSession } from "./workspace-session";
import { WorkspaceReadError, type Workspace } from "./workspace-client";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const item: Workspace = {
  id: "workspace-one",
  object: "nexus.workspace",
  display_name: "<script>共享😀</script>",
  status: "disabled",
  metadata_revision: 2,
  policy_revision: "4",
  created_at: "2026-10-24T00:00:00Z",
};
it("displays a legal final page with a repeated terminal cursor and disables paging", async () => {
  const { api } = setup();
  api.list
    .mockResolvedValueOnce({ object: "nexus.list", data: [item], has_more: true, next_cursor: "a" })
    .mockResolvedValueOnce({
      object: "nexus.list",
      data: [{ ...item, id: "workspace-final", display_name: "终页空间" }],
      has_more: false,
      next_cursor: "a",
    });
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "读取下一页工作空间" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "读取下一页工作空间" }));
  expect(await screen.findByText("终页空间")).toBeVisible();
  expect(screen.getByRole("button", { name: "读取下一页工作空间" })).toBeDisabled();
  expect(screen.queryByRole("alert")).toBe(null);
  expect(api.list).toHaveBeenCalledTimes(2);
});
function setup(strict = false) {
  const api = {
    list: vi.fn().mockResolvedValue({
      object: "nexus.list",
      data: [item],
      has_more: false,
      next_cursor: null,
    }),
    get: vi.fn().mockResolvedValue(item),
  };
  const session = new WorkspaceSession(api);
  const view = <WorkspaceHost session={session} />;
  render(strict ? <StrictMode>{view}</StrictMode> : view);
  return { api, session };
}
it("starts without reads or write controls and explains the shared boundary", () => {
  const { api } = setup();
  expect(api.list).not.toHaveBeenCalled();
  expect(screen.getByText(/内容与预算分组，不构成用户或安全隔离/)).toBeVisible();
  expect(screen.queryByRole("button", { name: /创建|修改|禁用工作空间/ })).toBe(null);
  expect(screen.getByText("尚未读取工作空间")).toBeVisible();
});
it("shows escaped metadata/versions and selected detail after explicit GET", async () => {
  const { api } = setup();
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  await screen.findByRole("button", { name: "读取工作空间 workspace-one" });
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间 workspace-one" }));
  await waitFor(() => expect(screen.getByRole("heading", { name: "工作空间详情" })).toHaveFocus());
  expect(api.get.mock.calls[0]![0]).toBe("workspace-one");
  expect(screen.getAllByText("<script>共享😀</script>").length).toBeGreaterThan(0);
  expect(screen.getByTestId("workspace-status")).toHaveTextContent("disabled");
  expect(screen.getByTestId("workspace-metadata-version")).toHaveTextContent("2");
});
it("keeps historical list after next-page failure and retries only on demand", async () => {
  const { api } = setup();
  api.list
    .mockResolvedValueOnce({
      object: "nexus.list",
      data: [item],
      has_more: true,
      next_cursor: "cursor-a",
    })
    .mockRejectedValueOnce(new WorkspaceReadError("service_unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "读取下一页工作空间" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "读取下一页工作空间" }));
  await screen.findByRole("alert");
  expect(screen.getByText(/历史列表快照/)).toBeVisible();
  expect(screen.getByRole("button", { name: "读取下一页工作空间" })).toBeDisabled();
  expect(api.list).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "重试原读取" }));
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(3));
  expect(api.list.mock.calls[2]![0]).toBe("cursor-a");
});
it("supports a declared empty deployment", async () => {
  const { api } = setup();
  api.list.mockResolvedValue({
    object: "nexus.list",
    data: [],
    has_more: false,
    next_cursor: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  expect(await screen.findByText("此页没有工作空间。")).toBeVisible();
});
it("StrictMode and hidden/visible do not dispose or automatically read", async () => {
  const { api } = setup(true);
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  await screen.findByRole("button", { name: "读取工作空间 workspace-one" });
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  fireEvent(document, new Event("visibilitychange"));
  expect(screen.getByText(/历史列表快照/)).toBeVisible();
  hidden.mockReturnValue(false);
  fireEvent(document, new Event("visibilitychange"));
  expect(api.list).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "读取工作空间" }));
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
});
