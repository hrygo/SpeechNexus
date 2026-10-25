import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../../../../packages/contracts/src";
import { App } from "../App";
import { TaskCenterHost } from "./TaskCenterHost";
import type { TaskApi } from "./job-client";

const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
function fixture() {
  let owner: AbortSignal | undefined;
  const api: TaskApi = {
    list: () =>
      Promise.resolve(
        parse("JobList", {
          object: "nexus.list",
          data: [example("queued-job")],
          has_more: false,
          next_cursor: null,
        }),
      ),
    getJob: () => Promise.resolve(parse("Job", example("queued-job"))),
    cancel: () => Promise.resolve(parse("JobCancellation", example("cancel-job"))),
    async *events(id, after, signal) {
      expect(id).toBe("job-demo");
      expect(after).toBeUndefined();
      owner = signal;
      yield await new Promise<never>((_resolve, reject) =>
        signal?.addEventListener("abort", () => reject(new Error("stopped")), { once: true }),
      );
    },
  };
  return { api, signal: () => owner };
}

describe("Admin production task host", () => {
  it("mounts the real task controls without automatically accessing any service", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    render(<App />);
    expect(screen.getByRole("button", { name: "读取任务列表" })).toBeInTheDocument();
    expect(screen.getByText(/任务中心使用当前部署的公开 API/)).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("演练场景")).not.toBeInTheDocument();
  });

  it("survives StrictMode replay, stops on pagehide, can reconnect after return, and releases on unmount", async () => {
    const { api, signal } = fixture();
    const view = render(
      <StrictMode>
        <TaskCenterHost api={api} />
      </StrictMode>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "读取任务列表" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看 job-demo" }));
    await screen.findByTestId("task-detail-status");
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signal()).toBeDefined());
    const first = signal();
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(first?.aborted).toBe(true);
    await waitFor(() =>
      expect(screen.getByTestId("task-event-state")).toHaveTextContent("事件连接已停止"),
    );
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signal()).not.toBe(first));
    const second = signal();
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(second?.aborted).toBe(true);
    hidden.mockRestore();
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signal()).not.toBe(second));
    const third = signal();
    view.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(third?.aborted).toBe(true);
  });
});
