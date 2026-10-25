import { fireEvent, render, screen } from "@testing-library/react";
import { AppShell } from "@speechnexus/shared";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";

describe("workbench shell", () => {
  it("opens the formal voice creation entry and retains unsubmitted text across navigation", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "音色创作" }));
    const input = await screen.findByLabelText("声音描述");
    expect(screen.getByLabelText("创作模型编号")).toHaveValue("");
    fireEvent.change(input, { target: { value: "待确认设计😀" } });
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    fireEvent.click(screen.getByRole("button", { name: "音色创作" }));
    expect(screen.getByLabelText("声音描述")).toHaveValue("待确认设计😀");
  });
  it("provides a formal realtime caption entry", () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "实时字幕" })).toBeInTheDocument();
  });
  it("provides the realtime TTS consumer entry", () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "实时配音" })).toBeInTheDocument();
  });
  it("provides the formal source-backed Meeting entry", () => {
    render(<App />);
    expect(screen.getByRole("button", { name: "会议记录" })).toBeInTheDocument();
  });
  it("does not present shared storage as personal isolation", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "语音任务集中完成" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("不代表服务端按用户私有保存");
    expect(screen.getByRole("button", { name: "上传并转写" })).toBeInTheDocument();
    expect(screen.getByText("文件转写入口已接入")).toBeInTheDocument();
  });

  it("switches between independent hosts without submitting requests", () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "文本配音" }));
    expect(screen.getByLabelText("配音模型")).toHaveValue("");
    expect(screen.queryByLabelText("音频文件")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "文本配音" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    expect(screen.getByLabelText("音频文件")).toBeInTheDocument();
    expect(screen.queryByLabelText("配音模型")).not.toBeInTheDocument();
  });

  it("opens the production manuscript consumer and reports actual unavailable service", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "workbench_unavailable",
            type: "service_unavailable_error",
            message: "Synthetic failure",
            param: null,
            request_id: "request-demo",
            retryable: false,
          },
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "项目与稿件" }));
    await screen.findByRole("region", { name: "项目与稿件工作台" });
    await screen.findByText("读取未完成。已有草稿仍保留，请检查连接后重新读取。");
    expect(screen.getByRole("button", { name: "创建项目" })).toBeDisabled();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("共享");
  });

  it("supports controlled status props and decorative header brand mark", () => {
    const { container } = render(
      <AppShell
        surface="个人语音工作台"
        tone="workbench"
        eyebrow="SpeechNexus / Workbench"
        title="受控状态验证"
        description="验证受控状态指示器"
        boundaryNote="当前部署共享"
        statusTitle="语音契约已冻结"
        statusDetail="等待 P1 路由联调"
        statusState="contract"
      >
        <div>内容区</div>
      </AppShell>,
    );

    expect(screen.getByText("语音契约已冻结")).toBeInTheDocument();
    expect(screen.getByText("等待 P1 路由联调")).toBeInTheDocument();
    expect(container.querySelector(".status-dot")).toHaveAttribute("data-state", "contract");
    expect(container.querySelector(".brand svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("opens manual reading and leaves unpersisted script behind when switching away", () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "提词阅读" }));
    fireEvent.change(screen.getByLabelText("阅读稿"), { target: { value: "仅供当前页面阅读。" } });
    fireEvent.click(screen.getByRole("button", { name: "使用此稿" }));
    expect(screen.getByLabelText("固定阅读稿")).toHaveTextContent("仅供当前页面阅读。");
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    expect(screen.queryByLabelText("固定阅读稿")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "提词阅读" }));
    expect(screen.getByLabelText("阅读稿")).toHaveValue("");
    expect(screen.queryByLabelText("固定阅读稿")).not.toBeInTheDocument();
  });
});
