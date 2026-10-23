import { render, screen } from "@testing-library/react";
import { AppShell } from "@speechnexus/shared";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("workbench shell", () => {
  it("does not present shared storage as personal isolation", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "语音任务集中完成" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("不代表服务端按用户私有保存");
    expect(screen.getByText("工程外壳已就绪")).toBeInTheDocument();
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
});
