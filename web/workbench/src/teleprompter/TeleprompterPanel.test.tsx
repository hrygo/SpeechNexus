import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TeleprompterPanel } from "./TeleprompterPanel";
import type { Contracts, WorkbenchClient } from "../manuscripts/workbench-client";

const key = "speechnexus.workbench.prompter.v1";
const body = "😀 第一段，保留原稿。\n第二段继续阅读。 <img src=x onerror=alert(1)>";
function useScript(text = body) {
  fireEvent.change(screen.getByLabelText("阅读稿"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "使用此稿" }));
}
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("manual reading panel", () => {
  it("has an honest empty state and does not save text automatically", () => {
    render(<TeleprompterPanel />);
    expect(screen.getByText(/自动语音跟随尚未接入/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "使用此稿" }));
    expect(screen.getByRole("alert", { name: "本地提词错误" })).toHaveTextContent("稿件无效");
    useScript();
    expect(screen.getByText("阅读稿", { exact: true }).textContent).toBe("阅读稿");
    expect(screen.getByLabelText("固定阅读稿")).toHaveTextContent("第一段，保留原稿。");
    expect(localStorage.getItem(key)).toBeNull();
    expect(screen.getByLabelText("固定阅读稿").querySelector("img")).toBeNull();
  });

  it("keeps active text fixed until explicitly applied, then creates a new local revision", () => {
    render(<TeleprompterPanel />);
    useScript();
    fireEvent.change(screen.getByLabelText("阅读稿"), { target: { value: "新版本完整稿件。" } });
    expect(screen.getByLabelText("固定阅读稿")).toHaveTextContent("第一段");
    expect(screen.getByText("本地稿第 1 版")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "使用此稿" }));
    expect(screen.getByLabelText("固定阅读稿")).toHaveTextContent("新版本完整稿件。");
    expect(screen.getByText("本地稿第 2 版")).toBeInTheDocument();
  });

  it("supports keyboard and range manual positioning without changing text", () => {
    render(<TeleprompterPanel />);
    useScript();
    const reading = screen.getByLabelText("固定阅读稿");
    fireEvent.keyDown(reading, { key: "End" });
    expect(screen.getByLabelText("阅读位置")).toHaveValue(String(Array.from(body).length));
    fireEvent.keyDown(reading, { key: "Home" });
    expect(screen.getByLabelText("阅读位置")).toHaveValue("0");
    fireEvent.change(screen.getByLabelText("阅读位置"), { target: { value: "9" } });
    expect(screen.getByLabelText("阅读位置")).toHaveValue("9");
    expect(reading.textContent).toBe(body);
  });

  it("explicitly saves a fixed script/position and restores only when requested", () => {
    const view = render(<TeleprompterPanel />);
    useScript();
    fireEvent.change(screen.getByLabelText("阅读位置"), { target: { value: "9" } });
    fireEvent.change(screen.getByLabelText("阅读稿"), {
      target: { value: "未应用草稿，不应保存" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存到此浏览器" }));
    expect(screen.getByRole("status", { name: "本地提词状态" })).toHaveTextContent(
      "已保存到此浏览器",
    );
    expect(localStorage.getItem(key)).not.toContain("未应用草稿");
    view.unmount();
    render(<TeleprompterPanel />);
    expect(screen.queryByLabelText("固定阅读稿")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复并替换当前稿" }));
    expect(screen.getByLabelText("阅读稿")).toHaveValue(body);
    expect(screen.getByLabelText("阅读位置")).toHaveValue("9");
    expect(screen.getByRole("status", { name: "本地提词状态" })).toHaveTextContent("手动阅读");
  });

  it("does not falsely report saving/restoring when browser storage fails", () => {
    render(<TeleprompterPanel />);
    useScript();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("private detail");
    });
    fireEvent.click(screen.getByRole("button", { name: "保存到此浏览器" }));
    expect(screen.getByRole("alert", { name: "本地提词错误" })).toHaveTextContent("保存失败");
    expect(screen.queryByText("private detail")).not.toBeInTheDocument();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private detail");
    });
    fireEvent.click(screen.getByRole("button", { name: "恢复并替换当前稿" }));
    expect(screen.getByRole("alert", { name: "本地提词错误" })).toHaveTextContent("恢复失败");
    expect(screen.getByLabelText("固定阅读稿").textContent).toBe(body);
  });

  it("rejects corrupt/oversized saved state and preserves the current script", () => {
    render(<TeleprompterPanel />);
    useScript();
    localStorage.setItem(key, '{"version":99}');
    fireEvent.click(screen.getByRole("button", { name: "恢复并替换当前稿" }));
    expect(screen.getByRole("alert", { name: "本地提词错误" })).toHaveTextContent("恢复失败");
    expect(screen.getByLabelText("固定阅读稿").textContent).toBe(body);
  });

  it("names both live regions so a saved snapshot and an operation stay distinguishable", async () => {
    const state = {
      project_id: "project-synthetic",
      script_revision: "3",
      visible_position: 12,
      manual_hold: false,
      updated_at: "2026-10-25T00:00:00Z",
    } as unknown as Contracts["TeleprompterState"];
    const client = {
      projects: () => Promise.resolve({ data: [{ id: "project-synthetic", name: "合成项目" }] }),
      teleprompterState: () => Promise.resolve(state),
    } as unknown as WorkbenchClient;
    render(<TeleprompterPanel client={client} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署项目" }));
    await screen.findByRole("option", { name: "合成项目" });
    fireEvent.change(screen.getByLabelText("项目"), { target: { value: "project-synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    const snapshot = await screen.findByRole("status", { name: "部署提词快照" });
    expect(snapshot).toHaveTextContent("部署内已保存");
    // The panel legitimately has two live regions; each must be addressable on its own, so a
    // screen reader announcement says which of the two changed.
    const operation = screen.getByRole("status", { name: "部署提词状态" });
    expect(operation).not.toBe(snapshot);
    expect(snapshot).not.toHaveTextContent("读取项目后选择目标。");
  });
});
