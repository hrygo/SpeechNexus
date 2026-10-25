import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchClient, parse } from "../manuscripts/workbench-client";
import { AssistantHost } from "./AssistantHost";
import { availabilityCopy, designState } from "./copy";

function status(capability: "available" | "disabled" | "unavailable", llmEnabled = false) {
  return parse("WorkbenchStatus", {
    object: "nexus.workbench_status",
    workbench_enabled: true,
    llm_enabled: llmEnabled,
    shared_deployment_notice: "shared_deployment",
    capabilities: [{ name: "assistant", status: capability }],
    ...(llmEnabled
      ? { minutes_profile: { template_revision: "1", llm_model: "synthetic-model" } }
      : { minutes_profile: null }),
  });
}

function catalogue(rows: number) {
  return {
    object: "nexus.list",
    data: Array.from({ length: rows }, (_, index) => ({
      voice_id: `voice_${index}`,
      revision: "1",
      display_name: `合成音色 ${index}`,
      source_type: "designed",
      status: "published",
      languages: ["zh"],
      audio_formats: ["wav"],
    })),
    has_more: false,
    next_cursor: null,
  } as never;
}

function mount(capability: "available" | "disabled" | "unavailable", voices = 0) {
  const client = new WorkbenchClient(vi.fn());
  vi.spyOn(client, "status").mockResolvedValue(status(capability, capability === "available"));
  vi.spyOn(client, "voices").mockResolvedValue(catalogue(voices));
  vi.spyOn(client, "projects").mockResolvedValue({
    object: "nexus.list",
    data: [],
    has_more: false,
    next_cursor: null,
  } as never);
  const create = vi.spyOn(client, "createAssistantTurn");
  render(<AssistantHost client={client} />);
  return { client, create };
}

describe("assistant panel", () => {
  it("maps the deployment verdict onto the three design states without upgrading it", () => {
    expect(designState("available")).toBe("ready");
    expect(designState("unavailable")).toBe("contract");
    expect(designState("disabled")).toBe("planned");
    expect(designState("unknown")).toBe("planned");
  });

  it("keeps the create form closed and explains why when the deployment disabled it", async () => {
    const { create } = mount("disabled");
    expect(await screen.findByText(availabilityCopy.disabled, { exact: false })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "创建助手回合" })).toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it("names the missing dependency instead of pretending the capability is off", async () => {
    const { create } = mount("unavailable");
    expect(await screen.findByText(availabilityCopy.unavailable, { exact: false })).toBeTruthy();
    expect(screen.queryByRole("form", { name: "创建助手回合" })).toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it("offers the form only when available, and says the catalogue is still empty", async () => {
    mount("available");
    const form = await screen.findByRole("form", { name: "创建助手回合" });
    expect(form).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText(/尚未发布可用音色/)).toBeTruthy();
    });
    const submit = screen.getByRole("button", { name: "提交回合" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
  });
});
