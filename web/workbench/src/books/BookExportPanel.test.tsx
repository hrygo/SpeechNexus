import { act, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
  render(
    <StrictMode>
      <BookExportPanel client={client} book={book} pollMs={10} />
    </StrictMode>,
  );
  return { client, create, read, cancel };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("book export panel", () => {
  it("starts a pinned export and shows published evidence without a download link", async () => {
    vi.useFakeTimers();
    const { create, read } = setup();
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
    // Nothing offers a download while the download operation is not served.
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/下载入口尚未接入/)).toBeTruthy();
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
