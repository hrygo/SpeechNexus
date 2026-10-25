import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { TaskCenter } from "./TaskCenter";
import { TaskCenterSession } from "./task-center-session";
import { TaskResponseError, type TaskApi } from "./job-client";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const job = () => parse("Job", example("queued-job"));
function fixture() {
  const api = {
    list: () =>
      Promise.resolve(
        parse("JobList", {
          object: "nexus.list",
          data: [job()],
          has_more: false,
          next_cursor: null,
        }),
      ),
    getJob: vi.fn(() => Promise.resolve(job())),
    cancel: () => Promise.resolve(parse("JobCancellation", example("cancel-job"))),
    events: vi.fn(async function* () {
      yield await Promise.resolve(
        parse("JobEvent", { ...example("job.queued"), job_id: "job-demo", id: "9007199254740993" }),
      );
    }),
  } satisfies TaskApi;
  return { api, session: new TaskCenterSession(api) };
}

describe("Admin event controls", () => {
  it("disables connection until selection and displays confirmed cursor after full read", async () => {
    const { api, session } = fixture();
    render(<TaskCenter session={session} />);
    expect(screen.getByRole("button", { name: "连接任务事件" })).toBeDisabled();
    await act(async () => {
      await session.select("job-demo");
    });
    fireEvent.click(screen.getByRole("button", { name: "准备取消" }));
    api.getJob.mockResolvedValue(parse("Job", { ...job(), status: "running" }));
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() =>
      expect(screen.getByTestId("task-event-cursor")).toHaveTextContent("9007199254740993"),
    );
    expect(screen.getByTestId("task-detail-status")).toHaveTextContent("running");
    expect(screen.getByTestId("task-cleanup")).toHaveTextContent("pending");
    expect(screen.queryByRole("button", { name: "确认取消本任务" })).not.toBeInTheDocument();
    expect(screen.getByTestId("task-event-state")).toHaveTextContent("事件连接已结束");
    expect(screen.getByRole("button", { name: "停止事件连接" })).toBeDisabled();
    session.dispose();
  });

  it("shows history recovery separately from normal disconnect and never exposes raw errors", async () => {
    const { api, session } = fixture();
    render(<TaskCenter session={session} />);
    await act(async () => {
      await session.select("job-demo");
    });
    api.events.mockImplementation(async function* () {
      yield await Promise.reject(new TaskResponseError(410, "event_history_expired"));
    });
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() =>
      expect(screen.getByTestId("task-event-state")).toHaveTextContent("已重读当前任务"),
    );
    expect(screen.getByTestId("task-event-cursor")).toHaveTextContent("无已确认水位");
    api.events.mockImplementation(async function* () {
      yield await Promise.reject(new Error("synthetic-sensitive-value"));
    });
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() =>
      expect(screen.getByTestId("task-event-state")).toHaveTextContent("事件连接中断"),
    );
    expect(screen.queryByText("synthetic-sensitive-value")).not.toBeInTheDocument();
    session.dispose();
  });

  it("disables the event action for a snapshot-only host", async () => {
    const { api } = fixture();
    const snapshots: TaskApi = { list: api.list, getJob: api.getJob, cancel: api.cancel };
    const session = new TaskCenterSession(snapshots);
    render(<TaskCenter session={session} />);
    await act(async () => {
      await session.select("job-demo");
    });
    expect(screen.getByRole("button", { name: "连接任务事件" })).toBeDisabled();
    expect(screen.getByText("当前入口未接入事件端口，可重读任务快照。")).toBeInTheDocument();
    session.dispose();
  });
});
