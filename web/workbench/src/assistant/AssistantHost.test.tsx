import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkbenchClient,
  WorkbenchResponseError,
  parse,
  type Contracts,
} from "../manuscripts/workbench-client";
import { AssistantHost } from "./AssistantHost";
import { availabilityCopy, designState } from "./copy";

function status(capability: "available" | "disabled" | "unavailable", llmEnabled = false) {
  return parse("WorkbenchStatusV2", {
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

function mount(
  capability: "available" | "disabled" | "unavailable",
  voices = 0,
  projects: Contracts["Project"][] = [],
) {
  const client = new WorkbenchClient(vi.fn());
  vi.spyOn(client, "status").mockResolvedValue(status(capability, capability === "available"));
  vi.spyOn(client, "voices").mockResolvedValue(catalogue(voices));
  vi.spyOn(client, "projects").mockResolvedValue({
    object: "nexus.list",
    data: projects,
    has_more: false,
    next_cursor: null,
  } as never);
  const create = vi.spyOn(client, "createAssistantTurn");
  const view = render(<AssistantHost client={client} />);
  return { client, create, ...view };
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

beforeEach(() => sessionStorage.clear());

const recoveryProject = parse("Project", {
  object: "nexus.workbench_project",
  id: "project_demo",
  name: "合成项目",
  workspace_id: "workspace_demo",
  status: "active",
  metadata_revision: 1,
  created_at: "2026-10-25T00:00:00Z",
  updated_at: "2026-10-25T00:00:00Z",
});
function acceptedTurn() {
  return parse("AssistantTurn", {
    object: "nexus.assistant_turn",
    id: "turn_demo",
    project_id: "project_demo",
    status: "queued",
    input_text: "合成问题",
    output_text: null,
    voice: { id: "voice_0", revision: "1" },
    speech_job_id: null,
    playback_generation: 1,
    error: null,
    created_at: "2026-10-25T00:00:00Z",
    updated_at: "2026-10-25T00:00:00Z",
  });
}
async function fillTurn() {
  await screen.findByRole("option", { name: /合成音色 0/ });
  fireEvent.change(screen.getByLabelText("项目"), { target: { value: "project_demo" } });
  fireEvent.change(screen.getByLabelText("已发布音色"), { target: { value: "voice_0@1" } });
  fireEvent.change(screen.getByLabelText("问题"), { target: { value: "合成问题" } });
  fireEvent.change(screen.getByLabelText(/请求键/), { target: { value: "original-turn-key" } });
}
it("recovers a committed assistant intent after a lost reply and remount without creating twice", async () => {
  const view = mount("available", 1, [recoveryProject]);
  const committed = new Map<string, Contracts["AssistantTurn"]>();
  view.create.mockImplementation((_project, _body, key) => {
    if (!committed.has(key)) {
      committed.set(key, acceptedTurn());
      return Promise.reject(new TypeError("synthetic lost reply"));
    }
    return Promise.resolve(committed.get(key)!);
  });
  await fillTurn();
  fireEvent.click(screen.getByRole("button", { name: "提交回合" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("问题")).toHaveValue("合成问题");
  expect(screen.getByLabelText(/请求键/)).toHaveValue("original-turn-key");
  view.unmount();
  render(<AssistantHost client={view.client} />);
  const replay = await screen.findByRole("button", { name: "按原键重放助手请求" });
  expect(view.create).toHaveBeenCalledTimes(1);
  fireEvent.click(replay);
  await screen.findByText("当前回合 turn_demo");
  expect(view.create.mock.calls[1]).toEqual(view.create.mock.calls[0]);
  expect(committed.size).toBe(1);
});
it("does not send an assistant request when its private journal cannot be saved", async () => {
  const { create } = mount("available", 1, [recoveryProject]);
  await fillTurn();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("synthetic quota");
  });
  fireEvent.click(screen.getByRole("button", { name: "提交回合" }));
  await screen.findByRole("alert");
  expect(create).not.toHaveBeenCalled();
});

it("keeps the original assistant intent unknown when a later replay is rejected at ingress", async () => {
  const view = mount("available", 1, [recoveryProject]);
  view.create.mockRejectedValueOnce(new TypeError("synthetic lost reply")).mockRejectedValueOnce(
    new WorkbenchResponseError(409, {
      error: {
        code: "idempotency_conflict",
        type: "invalid_request_error",
        message: "synthetic ingress refusal",
        param: null,
        request_id: "request_demo",
        retryable: false,
      },
    }),
  );
  await fillTurn();
  fireEvent.click(screen.getByRole("button", { name: "提交回合" }));
  const replay = await screen.findByRole("button", { name: "按原键重放助手请求" });
  await waitFor(() => expect(replay).not.toBeDisabled());
  fireEvent.click(replay);
  await waitFor(() => expect(view.create).toHaveBeenCalledTimes(2));
  expect(view.create.mock.calls[1]).toEqual(view.create.mock.calls[0]);
  expect(screen.queryByRole("button", { name: "结束已拒绝助手请求" })).toBeNull();
  expect(screen.getByLabelText("问题")).toBeDisabled();
  expect(sessionStorage.getItem("speechnexus.workbench.assistant.pending.v1")).toContain(
    "original-turn-key",
  );
});
