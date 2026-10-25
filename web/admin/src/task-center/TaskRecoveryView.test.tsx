import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { TaskCenterHost } from "./TaskCenterHost";
import { ADMIN_RECOVERY_KEY, type TaskRecoveryRecord } from "./task-recovery";
import type { TaskApi } from "./job-client";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const key = "9f712fc4-943f-4171-b910-9132c44927a2";
const record = {
  version: 1,
  selectedId: "job-demo",
  cursor: "9007199254740993",
  cancellation: { jobId: "job-demo", key },
};
function fixture() {
  const getJob = vi.fn((id: string) =>
    Promise.resolve(parse("Job", { ...example("queued-job"), id })),
  );
  const cancel = vi.fn(() => Promise.resolve(parse("JobCancellation", example("cancel-job"))));
  const api: TaskApi = {
    getJob,
    cancel,
    list: () =>
      Promise.resolve(
        parse("JobList", {
          object: "nexus.list",
          data: [example("queued-job")],
          has_more: false,
          next_cursor: null,
        }),
      ),
  };
  return { api, getJob, cancel };
}

beforeEach(() => sessionStorage.clear());
describe("Admin recovery controls", () => {
  it("restores metadata in StrictMode without reads, then explicitly replays the original cancellation", async () => {
    sessionStorage.setItem(ADMIN_RECOVERY_KEY, JSON.stringify(record));
    const f = fixture();
    render(
      <StrictMode>
        <TaskCenterHost api={f.api} />
      </StrictMode>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(f.getJob).not.toHaveBeenCalled();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(screen.getByTestId("task-recovery-state")).toHaveTextContent("本标签页保留");
    expect(screen.getByText("待核对任务：job-demo")).toBeInTheDocument();
    expect(screen.queryByTestId("task-detail-status")).not.toBeInTheDocument();
    expect(screen.getByTestId("task-event-cursor")).toHaveTextContent(record.cursor);
    expect(screen.getByRole("button", { name: "准备取消" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重读所选任务" }));
    await screen.findByTestId("task-detail-status");
    expect(f.cancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "按原键重放取消" }));
    await waitFor(() =>
      expect(f.cancel).toHaveBeenCalledWith("job-demo", key, expect.any(AbortSignal)),
    );
    expect(await screen.findByText("accepted=true / cancelling / pending")).toBeInTheDocument();
    expect(
      (JSON.parse(sessionStorage.getItem(ADMIN_RECOVERY_KEY)!) as TaskRecoveryRecord).cancellation,
    ).toBeNull();
  });

  it("handles denied sessionStorage without render failure or memory-only cancellation", async () => {
    vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
      throw new Error("synthetic-sensitive-value");
    });
    const f = fixture();
    render(<TaskCenterHost api={f.api} />);
    expect(screen.getByTestId("task-recovery-state")).toHaveTextContent("已阻止取消与事件推进");
    fireEvent.click(screen.getByRole("button", { name: "读取任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看 job-demo" }));
    await screen.findByTestId("task-detail-status");
    expect(screen.getByRole("button", { name: "准备取消" })).toBeDisabled();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(screen.queryByText("synthetic-sensitive-value")).not.toBeInTheDocument();
  });
});
