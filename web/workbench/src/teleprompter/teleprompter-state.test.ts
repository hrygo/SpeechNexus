import { describe, expect, it, vi } from "vitest";
import { WorkbenchClient, WorkbenchResponseError, parse } from "../manuscripts/workbench-client";
import { TeleprompterStateSession } from "./teleprompter-state";

const PROJECT = "project_demo";

function state(overrides: Record<string, unknown> = {}) {
  return parse("TeleprompterState", {
    object: "nexus.teleprompter_state",
    project_id: PROJECT,
    script_revision: "2",
    visible_position: 40,
    manual_hold: true,
    updated_at: "2026-10-25T00:00:00Z",
    ...overrides,
  });
}

function notFound() {
  return new WorkbenchResponseError(404, {
    error: {
      code: "resource_not_found",
      type: "invalid_request_error",
      message: "合成缺失",
      param: null,
      request_id: "request_demo",
      retryable: false,
    },
  });
}

function conflict() {
  return new WorkbenchResponseError(409, {
    error: {
      code: "revision_conflict",
      type: "invalid_request_error",
      message: "合成冲突",
      param: null,
      request_id: "request_demo",
      retryable: false,
    },
  });
}

function setup() {
  const client = new WorkbenchClient(vi.fn());
  const read = vi.spyOn(client, "teleprompterState").mockResolvedValue(state());
  const write = vi.spyOn(client, "updateTeleprompter").mockResolvedValue(state());
  const session = new TeleprompterStateSession(client);
  return { client, read, write, session };
}

describe("deployment teleprompter state", () => {
  it("reports an unsaved project as missing instead of position zero", async () => {
    const { session, read } = setup();
    read.mockRejectedValueOnce(notFound());
    await session.load(PROJECT);
    const snapshot = session.getSnapshot();
    expect(snapshot.problem).toBe("missing");
    expect(snapshot.state).toBeNull();
    // Nothing invents a revision or a position for an unsaved project.
    expect(JSON.stringify(snapshot)).not.toContain("visible_position");
  });

  it("reads the stored revision, position and manual takeover", async () => {
    const { session, read } = setup();
    await session.load(PROJECT);
    expect(read).toHaveBeenCalledWith(PROJECT, undefined);
    expect(session.getSnapshot().state).toMatchObject({
      project_id: PROJECT,
      script_revision: "2",
      visible_position: 40,
      manual_hold: true,
    });
    expect(session.getSnapshot().problem).toBeNull();
  });

  it("accepts a write and publishes the stored state", async () => {
    const { session, write } = setup();
    write.mockResolvedValueOnce(
      state({ script_revision: "3", visible_position: 10, manual_hold: false }),
    );
    await session.load(PROJECT);
    await session.save(
      PROJECT,
      { script_revision: "3", visible_position: 10, manual_hold: false },
      "prompter-key-1",
    );
    expect(write).toHaveBeenCalledWith(
      PROJECT,
      { script_revision: "3", visible_position: 10, manual_hold: false },
      "prompter-key-1",
    );
    const snapshot = session.getSnapshot();
    expect(snapshot.accepted).toBe(true);
    expect(snapshot.state?.visible_position).toBe(10);
    expect(snapshot.problem).toBeNull();
  });

  it("keeps the previous reading and never retries on a conflict", async () => {
    const { session, write } = setup();
    await session.load(PROJECT);
    write.mockRejectedValueOnce(conflict());
    await session.save(
      PROJECT,
      { script_revision: "1", visible_position: 5, manual_hold: false },
      "prompter-key-2",
    );
    const snapshot = session.getSnapshot();
    expect(snapshot.problem).toBe("conflict");
    expect(snapshot.accepted).toBe(false);
    // The state that was last read from the deployment is still what the UI shows.
    expect(snapshot.state?.script_revision).toBe("2");
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("separates an unconfirmed transport failure from a rejection", async () => {
    const { session, write } = setup();
    await session.load(PROJECT);
    write.mockRejectedValueOnce(new TypeError("network"));
    await session.save(
      PROJECT,
      { script_revision: "2", visible_position: 41, manual_hold: true },
      "prompter-key-3",
    );
    expect(session.getSnapshot().problem).toBe("unavailable");
    expect(session.getSnapshot().accepted).toBe(false);
  });

  it("ignores a late answer after disposal", async () => {
    const { session, read } = setup();
    read.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(() => resolve(state()), 10);
        }),
    );
    const pending = session.load(PROJECT);
    session.dispose();
    await pending;
    expect(session.getSnapshot().state).toBeNull();
  });
});
