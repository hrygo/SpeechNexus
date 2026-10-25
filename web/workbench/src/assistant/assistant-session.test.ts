import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchClient, parse } from "../manuscripts/workbench-client";
import { AssistantSession } from "./assistant-session";

const VOICE = { id: "voice_demo", revision: "1" };

function status(capability: "available" | "disabled" | "unavailable", workbenchEnabled = true) {
  return parse("WorkbenchStatusV2", {
    object: "nexus.workbench_status",
    workbench_enabled: workbenchEnabled,
    llm_enabled: false,
    shared_deployment_notice: "shared_deployment",
    capabilities: [{ name: "assistant", status: capability }],
    minutes_profile: null,
  });
}

function turn(overrides: Record<string, unknown> = {}) {
  return parse("AssistantTurn", {
    id: "turn_demo",
    object: "nexus.assistant_turn",
    project_id: "project_demo",
    status: "queued",
    input_text: "合成问题",
    output_text: null,
    voice: VOICE,
    speech_job_id: null,
    playback_generation: 1,
    error: null,
    created_at: "2026-10-25T00:00:00Z",
    updated_at: "2026-10-25T00:00:00Z",
    ...overrides,
  });
}

function setup(
  capability: "available" | "disabled" | "unavailable" = "available",
  workbenchEnabled = true,
) {
  const client = new WorkbenchClient(vi.fn());
  const statusRead = vi
    .spyOn(client, "status")
    .mockResolvedValue(status(capability, workbenchEnabled));
  const voices = vi.spyOn(client, "voices").mockResolvedValue({
    object: "nexus.list",
    data: [],
    has_more: false,
    next_cursor: null,
  } as never);
  const create = vi.spyOn(client, "createAssistantTurn").mockResolvedValue(turn());
  const read = vi.spyOn(client, "assistantTurn").mockResolvedValue(turn());
  const cancel = vi.spyOn(client, "cancelAssistantTurn").mockResolvedValue(turn());
  const session = new AssistantSession(client, { pollMs: 10 });
  return { client, statusRead, voices, create, read, cancel, session };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("assistant session", () => {
  it("keeps creation closed and reads no catalogue when the deployment disables it", async () => {
    const { session, voices, create } = setup("disabled");
    await session.load();
    expect(session.getSnapshot().availability).toBe("disabled");
    await session.start(
      "project_demo",
      { input_text: "合成问题", voice: VOICE },
      "assistant-key-1",
    );
    expect(create).not.toHaveBeenCalled();
    expect(voices).not.toHaveBeenCalled();
  });

  it("distinguishes a missing dependency from a deliberate disable", async () => {
    const { session } = setup("unavailable");
    await session.load();
    expect(session.getSnapshot()).toMatchObject({ availability: "unavailable", problem: null });
  });

  it("reports the deployment as unavailable when the whole workbench is off", async () => {
    const { session } = setup("available", false);
    await session.load();
    expect(session.getSnapshot().availability).toBe("unavailable");
  });

  it("submits once, polls to success and shows the text only then", async () => {
    const { session, create, read } = setup();
    read.mockResolvedValueOnce(
      turn({
        status: "running",
        playback_generation: 2,
        speech_job_id: "job_demo",
        output_text: "合成回答草稿",
      }),
    );
    read.mockResolvedValueOnce(
      turn({
        status: "succeeded",
        playback_generation: 3,
        speech_job_id: "job_demo",
        output_text: "合成回答",
      }),
    );
    await session.load();
    expect(session.getSnapshot().availability).toBe("available");
    await session.start(
      "project_demo",
      { input_text: "合成问题", voice: VOICE },
      "assistant-key-2",
    );
    expect(create).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().turn?.output_text).toBeNull();
    await vi.waitFor(() => {
      expect(session.getSnapshot().turn?.status).toBe("succeeded");
    });
    expect(read).toHaveBeenCalledTimes(2);
    expect(session.getSnapshot().turn?.output_text).toBe("合成回答");
    expect(session.getSnapshot().watching).toBe(false);
  });

  it("shows a server-reported failure and never retries by itself", async () => {
    const { session, create, read } = setup();
    read.mockResolvedValueOnce(
      turn({
        status: "failed",
        playback_generation: 2,
        error: {
          code: "speech_job_failed",
          message: "合成失败",
          retryable: false,
          param: null,
        },
      }),
    );
    await session.load();
    await session.start(
      "project_demo",
      { input_text: "合成问题", voice: VOICE },
      "assistant-key-3",
    );
    await vi.waitFor(() => {
      expect(session.getSnapshot().turn?.status).toBe("failed");
    });
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(create).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().problem).toBeNull();
  });

  it("accepts an idempotent repeat cancellation without watching a terminal turn", async () => {
    const { session, cancel } = setup();
    cancel.mockResolvedValueOnce(turn({ status: "cancelled", playback_generation: 2 }));
    await session.load();
    await session.start(
      "project_demo",
      { input_text: "合成问题", voice: VOICE },
      "assistant-key-4",
    );
    await session.cancel("assistant-cancel-2");
    await vi.waitFor(() => {
      expect(session.getSnapshot().turn?.status).toBe("cancelled");
    });
    await session.cancel("assistant-cancel-3");
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().watching).toBe(false);
  });

  it("maps a rejected request to a visible problem instead of an invented answer", async () => {
    const { session, create } = setup();
    const { WorkbenchResponseError } = await import("../manuscripts/workbench-client");
    create.mockRejectedValueOnce(
      new WorkbenchResponseError(409, {
        error: {
          code: "idempotency_conflict",
          type: "invalid_request_error",
          message: "合成冲突",
          param: null,
          request_id: "request_demo",
          retryable: false,
        },
      }),
    );
    await session.load();
    await session.start(
      "project_demo",
      { input_text: "合成问题", voice: VOICE },
      "assistant-key-5",
    );
    expect(session.getSnapshot().problem).toBe("conflict");
    expect(session.getSnapshot().turn).toBeNull();
  });
});

beforeEach(() => sessionStorage.clear());
