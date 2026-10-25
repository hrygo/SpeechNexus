import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { TaskCenter } from "./TaskCenter";
import { TaskCenterSession } from "./task-center-session";
import type { TaskApi } from "./job-client";

const job = (id = "job-demo") =>
  parse("Job", {
    ...structuredClone(fixtures.examples.find((e) => e.id === "queued-job")!.value),
    id,
  });
function fixture(empty = false) {
  const cancelMock = vi.fn(() =>
    Promise.resolve(
      parse(
        "JobCancellation",
        structuredClone(fixtures.examples.find((e) => e.id === "cancel-job")!.value),
      ),
    ),
  );
  const api: TaskApi = {
    list: () =>
      Promise.resolve(
        parse("JobList", {
          object: "nexus.list",
          data: empty ? [] : [job(), job("job-other")],
          has_more: false,
          next_cursor: null,
        }),
      ),
    getJob: (id) => Promise.resolve(job(id)),
    cancel: cancelMock,
  };
  const session = new TaskCenterSession(api);
  return { api, session, cancelMock };
}

describe("Admin task view", () => {
  it("requires target confirmation and keeps cancellation distinct from resource release", async () => {
    const { session, cancelMock } = fixture();
    render(<TaskCenter session={session} />);
    fireEvent.click(screen.getByRole("button", { name: "读取任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看 job-demo" }));
    await screen.findByTestId("task-detail-status");
    fireEvent.click(screen.getByRole("button", { name: "准备取消" }));
    expect(cancelMock).not.toHaveBeenCalled();
    expect(screen.getByText("确认取消任务 job-demo？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认取消本任务" }));
    await waitFor(() => expect(cancelMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("task-detail-status")).toHaveTextContent("queued"),
    );
    expect(screen.getByTestId("task-cleanup")).toHaveTextContent("pending");
    expect(await screen.findByText("accepted=true / cancelling / pending")).toBeInTheDocument();
    session.dispose();
  });

  it("does not move a prepared confirmation onto a different selected task", async () => {
    const { session, cancelMock } = fixture();
    render(<TaskCenter session={session} />);
    await act(async () => {
      await session.load();
      await session.select("job-demo");
    });
    fireEvent.click(await screen.findByRole("button", { name: "准备取消" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 job-other" }));
    await waitFor(() =>
      expect(screen.getByTestId("task-detail-id")).toHaveTextContent("job-other"),
    );
    expect(screen.queryByRole("button", { name: "确认取消本任务" })).not.toBeInTheDocument();
    expect(cancelMock).not.toHaveBeenCalled();
    session.dispose();
  });

  it("shows empty and failed reads without exposing response content", async () => {
    const { api, session } = fixture(true);
    render(<TaskCenter session={session} />);
    fireEvent.click(screen.getByRole("button", { name: "读取任务列表" }));
    expect(await screen.findByText("当前快照没有任务。可重新读取列表。")).toBeInTheDocument();
    api.list = () => Promise.reject(new Error("synthetic-sensitive-value"));
    fireEvent.click(screen.getByRole("button", { name: "读取任务列表" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("列表读取失败");
    expect(screen.queryByText("synthetic-sensitive-value")).not.toBeInTheDocument();
    session.dispose();
  });
});
