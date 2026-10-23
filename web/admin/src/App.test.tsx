import { render, screen } from "@testing-library/react";
import { BRAND_IDENTITY, BrandMark, NEXUS_TOKENS } from "@speechnexus/shared";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("AdminUI shell", () => {
  it("states the shared boundary and delivered contract work", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "运行事实管理" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("没有登录与角色隔离");
    expect(screen.getAllByText("契约已交付")).toHaveLength(1);
    expect(screen.getByText("本地离线 · 音频不出网")).toBeInTheDocument();
    expect(screen.getByText("AdminUI")).toBeInTheDocument();
  });

  it("renders BrandMark with unique gradient IDs, CSS token stops, and decorative a11y support", () => {
    const { container } = render(
      <div>
        <BrandMark tone="admin" />
        <BrandMark tone="workbench" decorative />
      </div>,
    );

    const svgs = container.querySelectorAll("svg");
    expect(svgs).toHaveLength(2);
    expect(svgs[0]).toHaveAttribute("role", "img");
    expect(svgs[0]).toHaveAttribute("aria-label", "SpeechNexus · 声枢");
    expect(svgs[1]).toHaveAttribute("aria-hidden", "true");
    expect(svgs[1]).not.toHaveAttribute("role");

    const gradients = container.querySelectorAll("linearGradient");
    const ids = Array.from(gradients).map((g) => g.getAttribute("id"));
    expect(new Set(ids).size).toBe(ids.length);

    expect(BRAND_IDENTITY.cnName).toBe("声枢");
    expect(NEXUS_TOKENS.semantics.page).toBe("var(--bg-page)");
    expect(NEXUS_TOKENS.space.s4).toBe("var(--nexus-space-4)");
  });
});
