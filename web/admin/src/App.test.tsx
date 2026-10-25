import { render, screen } from "@testing-library/react";
import { BRAND_IDENTITY, BrandMark, NEXUS_TOKENS } from "@speechnexus/shared";
import { describe, expect, it } from "vitest";

import {
  extractDefinedCssVariables,
  validateSourceLine,
} from "../../../scripts/dev-env/validate-design-tokens.mjs";
import { App } from "./App";

describe("AdminUI shell", () => {
  it("states the shared boundary and delivered contract work", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "运行事实管理" })).toBeInTheDocument();
    expect(screen.getByLabelText("部署数据边界")).toHaveTextContent("没有登录与角色隔离");
    // Two cards are now honestly in the `contract` presentation state: the protocol card and
    // the diagnostics card. Pin the specific claim instead of counting labels.
    expect(screen.getByRole("article", { name: "部署诊断" })).toHaveTextContent("契约已交付");
    expect(screen.getByRole("article", { name: "服务进程" })).toHaveTextContent("基础就绪");
    expect(screen.getByText("本地离线 · 音频不出网")).toBeInTheDocument();
    expect(screen.getByText("AdminUI")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: "服务进程" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "共享工作空间" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "场景预设草稿" })).toBeInTheDocument();
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

  it("enforces all 6 design token and architectural guardrail rules", () => {
    const vars = extractDefinedCssVariables(":root { --line-strong: 1px; }");
    expect(vars.has("--line-strong")).toBe(true);
    expect(vars.has("--line")).toBe(false);

    expect(validateSourceLine('const c = "#ff0000";')).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "no-hardcoded-color" })]),
    );
    expect(validateSourceLine('const c = "oklch(0.5 0.2 160)";')).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "no-hardcoded-color" })]),
    );
    expect(validateSourceLine("<span>仅自己可见</span>")).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "architectural-copy-guard" })]),
    );
    expect(validateSourceLine('<CapabilityCard state={"mock"} />')).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "capability-state-integrity" })]),
    );
    expect(validateSourceLine("const fakeGpu = Math.random();")).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "no-fake-telemetry" })]),
    );
    expect(
      validateSourceLine('<link href="https://fonts.googleapis.com/css2" rel="stylesheet" />', {
        isMarkupOrStyleOnly: true,
      }),
    ).toEqual(
      expect.arrayContaining([expect.objectContaining({ rule: "no-external-network-asset" })]),
    );
    expect(
      validateSourceLine('<svg xmlns="http://www.w3.org/2000/svg">', {
        isMarkupOrStyleOnly: true,
      }),
    ).toHaveLength(0);
  });
});
