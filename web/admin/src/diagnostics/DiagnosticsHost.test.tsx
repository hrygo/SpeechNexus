import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { parse } from "../../../../packages/contracts/src/workbench";
import { DiagnosticsHost } from "./DiagnosticsHost";
import { asState, designState } from "./copy";
import { WorkbenchStatusError, type WorkbenchStatus } from "./workbench-status";

function status(overrides: Record<string, unknown> = {}): WorkbenchStatus {
  return parse("WorkbenchStatus", {
    object: "nexus.workbench_status",
    workbench_enabled: true,
    llm_enabled: false,
    shared_deployment_notice: "shared_deployment",
    capabilities: [
      { name: "transcription", status: "disabled" },
      { name: "meeting_minutes", status: "disabled" },
      { name: "assistant", status: "disabled" },
      { name: "teleprompter", status: "available" },
      { name: "audiobook_export", status: "disabled" },
    ],
    minutes_profile: null,
    ...overrides,
  });
}

describe("admin deployment diagnostics", () => {
  it("maps a server verdict onto the design states without upgrading it", () => {
    expect(designState("available")).toBe("ready");
    expect(designState("unavailable")).toBe("contract");
    expect(designState("disabled")).toBe("planned");
    expect(asState("available")).toBe("available");
    // An unknown or empty state is never treated as available.
    expect(asState("degraded")).toBeNull();
    expect(asState("")).toBeNull();
  });

  it("reads nothing until the operator asks", () => {
    const load = vi.fn(() => Promise.resolve(status()));
    render(<DiagnosticsHost load={load} />);
    expect(load).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "部署能力三态" })).toBeNull();
    expect(screen.getByRole("button", { name: "读取部署状态" })).toBeTruthy();
  });

  it("reports every capability the deployment sent, verbatim", async () => {
    render(<DiagnosticsHost load={() => Promise.resolve(status())} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    expect(await screen.findByRole("region", { name: "部署能力三态" })).toBeTruthy();
    for (const title of ["转写", "会议纪要", "语音助手", "提词阅读", "有声书导出"]) {
      expect(screen.getByRole("heading", { name: title })).toBeTruthy();
    }
    // The teleprompter is the only enabled capability in this deployment.
    expect(screen.getAllByText(/服务端状态：available/)).toHaveLength(1);
    expect(screen.getAllByText(/服务端状态：disabled/)).toHaveLength(4);
    expect(screen.getByText(/默认关闭/)).toBeTruthy();
  });

  it("shows the configured intranet model binding without inventing one", async () => {
    render(
      <DiagnosticsHost
        load={() =>
          Promise.resolve(
            status({
              llm_enabled: true,
              minutes_profile: { template_revision: "1", llm_model: "synthetic-intranet-model" },
            }),
          )
        }
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    expect(await screen.findByText(/synthetic-intranet-model/)).toBeTruthy();
    expect(screen.getByText(/模板修订 1/)).toBeTruthy();
  });

  it("claims no capability at all when the status cannot be read", async () => {
    render(<DiagnosticsHost load={() => Promise.reject(new WorkbenchStatusError("status_503"))} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("status_503");
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "部署能力三态" })).toBeNull();
    });
    expect(screen.queryByText(/服务端状态：/)).toBeNull();
  });

  it("reports an unrecognised state as unknown instead of available", async () => {
    // A non-conforming server cannot pass `parse`, so the defensive branch is built by hand.
    const drifted = { ...status(), capabilities: [{ name: "assistant", status: "degraded" }] };
    render(<DiagnosticsHost load={() => Promise.resolve(drifted as unknown as WorkbenchStatus)} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    expect(await screen.findByText(/无法识别的状态/)).toBeTruthy();
    expect(screen.queryByText(/服务端状态：degraded/)).toBeNull();
  });

  it("localizes the shared-deployment code and never echoes it as English", async () => {
    render(<DiagnosticsHost load={() => Promise.resolve(status())} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    expect(await screen.findByText(/本部署为共享部署/)).toBeTruthy();
    expect(screen.queryByText("shared_deployment")).toBeNull();
  });

  it("stops publishing a late answer after unmount", async () => {
    let release: (value: WorkbenchStatus) => void = () => {};
    const load = () =>
      new Promise<WorkbenchStatus>((resolve) => {
        release = resolve;
      });
    const view = render(<DiagnosticsHost load={load} />);
    fireEvent.click(screen.getByRole("button", { name: "读取部署状态" }));
    view.unmount();
    release(status());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(/工作台：/)).toBeNull();
  });
});
