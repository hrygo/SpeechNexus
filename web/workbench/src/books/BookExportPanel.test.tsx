import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchClient, WorkbenchResponseError, parse } from "../manuscripts/workbench-client";
import { BookExportPanel } from "./BookExportPanel";

const book = parse("Book", {
  object: "nexus.book",
  id: "book_demo",
  project_id: "project_demo",
  title: "合成有声书",
  status: "draft",
  casting_revision: "3",
  created_at: "2026-10-24T12:00:00Z",
  updated_at: "2026-10-24T12:00:00Z",
});

function job(overrides: Record<string, unknown> = {}) {
  return parse("ExportJob", {
    object: "nexus.export_job",
    id: "export_demo",
    book_id: book.id,
    status: "queued",
    format: "zip",
    manifest_revision: "3",
    progress_completed: 0,
    progress_total: 2,
    artifact: null,
    error: null,
    created_at: "2026-10-24T12:00:00Z",
    updated_at: "2026-10-24T12:00:00Z",
    ...overrides,
  });
}

const artifact = {
  object: "nexus.export_artifact",
  id: "artifact_demo",
  export_id: "export_demo",
  manifest_revision: "3",
  media_type: "application/zip",
  bytes: 2048,
  sha256: "sha256:" + "ab".repeat(32),
  created_at: "2026-10-24T12:00:05Z",
};

function setup() {
  const client = new WorkbenchClient(vi.fn());
  const create = vi.spyOn(client, "createBookExport").mockResolvedValue(job());
  const read = vi.spyOn(client, "bookExport").mockResolvedValue(job());
  const cancel = vi
    .spyOn(client, "cancelBookExport")
    .mockResolvedValue(job({ status: "cancelling" }));
  const view = render(
    <StrictMode>
      <BookExportPanel client={client} book={book} pollMs={10} />
    </StrictMode>,
  );
  return { client, create, read, cancel, ...view };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("book export panel", () => {
  it("offers a verified download only after the pinned export succeeds", async () => {
    vi.useFakeTimers();
    const { client, create, read } = setup();
    const download = vi
      .spyOn(client, "downloadBookExport")
      .mockRejectedValue(new Error("synthetic unavailable"));
    expect(screen.queryByRole("button", { name: "校验并下载导出" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0]![1]).toEqual({ expected_casting_revision: "3", format: "zip" });
    expect(screen.getByText(/已完成 0 \/ 2/)).toBeTruthy();

    read.mockResolvedValue(job({ status: "succeeded", progress_completed: 2, artifact }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByText(/产物已发布/)).toBeTruthy();
    expect(screen.getByText(`sha256:${"ab".repeat(32)}`)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "校验并下载导出" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(download).toHaveBeenCalledOnce();
    expect(download.mock.calls[0]![0].artifact).toEqual(artifact);
    expect(screen.getByRole("alert").textContent).toContain("下载未完成");
  });

  it("asks for cancellation and keeps the accepted answer", async () => {
    vi.useFakeTimers();
    const { cancel } = setup();
    fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    fireEvent.click(screen.getByRole("button", { name: "取消导出" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/取消已受理/)).toBeTruthy();
  });

  it("surfaces an unconfirmed outcome instead of inventing success", async () => {
    const { create } = setup();
    create.mockRejectedValueOnce(
      new WorkbenchResponseError(503, {
        error: {
          code: "store_unavailable",
          type: "api_error",
          message: "private-synthetic",
          param: null,
          request_id: "request-demo",
          retryable: true,
        },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
    await act(async () => {});
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText(/产物已发布/)).toBeNull();
  });

  it("shows a failed export with its stable code", async () => {
    vi.useFakeTimers();
    const { read } = setup();
    fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    read.mockResolvedValue(
      job({
        status: "failed",
        error: { code: "unavailable", message: "export production failed", retryable: true },
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.getByText("unavailable")).toBeTruthy();
    expect(screen.getByText(/可重试/)).toBeTruthy();
  });
});

beforeEach(() => sessionStorage.clear());

it("restores the exact export intent after commit reply loss even when the book revision changed", async () => {
  const view = setup();
  const committed = new Map<string, ReturnType<typeof job>>();
  view.create.mockImplementation((_book, _body, key) => {
    if (!committed.has(key)) {
      committed.set(key, job());
      return Promise.reject(new TypeError("synthetic lost reply"));
    }
    return Promise.resolve(committed.get(key)!);
  });
  fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("导出格式")).toBeDisabled();
  view.unmount();
  render(<BookExportPanel client={view.client} book={{ ...book, casting_revision: "9" }} />);
  const replay = await screen.findByRole("button", { name: "按原键重放导出请求" });
  expect(view.create).toHaveBeenCalledTimes(1);
  fireEvent.click(replay);
  await screen.findByText(/已完成 0 \/ 2/);
  expect(view.create.mock.calls[1]).toEqual(view.create.mock.calls[0]);
  expect(committed.size).toBe(1);
});
it("does not send an export before saving its private recovery intent", async () => {
  const { create } = setup();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("synthetic quota");
  });
  fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
  await screen.findByRole("alert");
  expect(create).not.toHaveBeenCalled();
});

it("keeps an export unknown after a later replay receives an ingress rejection", async () => {
  const { create } = setup();
  create.mockRejectedValueOnce(new TypeError("synthetic lost reply")).mockRejectedValueOnce(
    new WorkbenchResponseError(409, {
      error: {
        code: "revision_conflict",
        type: "invalid_request_error",
        message: "synthetic ingress refusal",
        param: null,
        request_id: "request_demo",
        retryable: false,
      },
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "发起导出" }));
  const replay = await screen.findByRole("button", { name: "按原键重放导出请求" });
  await waitFor(() => expect(replay).not.toBeDisabled());
  fireEvent.click(replay);
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  expect(create.mock.calls[1]).toEqual(create.mock.calls[0]);
  expect(screen.queryByRole("button", { name: "结束已拒绝导出请求" })).toBeNull();
  expect(screen.getByLabelText("导出格式")).toBeDisabled();
});
