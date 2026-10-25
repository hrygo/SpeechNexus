import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { meetingJournalKey } from "./meeting-journal";
import { sample, source, meeting } from "./meeting-fixtures";
import { parse } from "../manuscripts/workbench-client";

function setup(loseCreate = false) {
  window.sessionStorage.clear();
  const writes: { key: string | null; body: unknown }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const path = new URL(request.url).pathname;
    let body: unknown;
    let status = 200;
    if (request.method === "POST") {
      writes.push({
        key: request.headers.get("Idempotency-Key"),
        body: (await request.json()) as unknown,
      });
      if (loseCreate && writes.length === 1) {
        body = {
          error: {
            code: "workbench_unavailable",
            type: "service_unavailable_error",
            message: "synthetic",
            param: null,
            request_id: "request-demo",
            retryable: true,
          },
        };
        status = 503;
      } else {
        body = meeting();
        status = 201;
      }
    } else if (path.endsWith("/status")) body = sample("getWorkbenchStatus");
    else if (path.endsWith("/projects")) body = sample("listProjects");
    else if (path.endsWith("/projects/project-demo")) body = sample("createProject");
    else if (path.endsWith("/transcripts")) body = sample("listTranscripts");
    else if (path.endsWith("/transcripts/transcript-demo")) body = source();
    else if (path.endsWith("/meetings/meeting-demo")) body = meeting();
    else throw Error("unexpected synthetic route");
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  render(<App />);
  return writes;
}
async function select() {
  fireEvent.click(screen.getByRole("button", { name: "会议记录" }));
  fireEvent.click(
    await screen.findByRole("button", {
      name: parse("ProjectList", sample("listProjects")).data[0]!.name,
    }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: parse("TranscriptList", sample("listTranscripts")).data[0]!.display_name,
    }),
  );
  await screen.findByLabelText("会议校对正文");
}
describe("Meeting memory survives outer navigation", () => {
  it("retains quota-only Unicode draft across feature switches and recovers storage explicitly", async () => {
    setup();
    await select();
    const original = window.sessionStorage.setItem.bind(window.sessionStorage);
    let fail = true;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      k: string,
      v: string,
    ) {
      if (k === meetingJournalKey && fail) throw Error("quota");
      original(k, v);
    });
    fireEvent.change(screen.getByLabelText("会议校对正文"), {
      target: { value: " 仅内存 e\u0301😀\n " },
    });
    await screen.findByText(/本地恢复信息未能保存/);
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    fireEvent.click(screen.getByRole("button", { name: "会议记录" }));
    expect(await screen.findByLabelText("会议校对正文")).toHaveValue(" 仅内存 e\u0301😀\n ");
    expect(screen.getByRole("button", { name: "重试保存恢复信息" })).toBeEnabled();
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "重试保存恢复信息" }));
    await waitFor(() => expect(screen.queryByText(/本地恢复信息未能保存/)).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    fireEvent.click(screen.getByRole("button", { name: "会议记录" }));
    expect(await screen.findByLabelText("会议校对正文")).toHaveValue(" 仅内存 e\u0301😀\n ");
  });
  it("preserves the unknown original create intent across feature switches", async () => {
    const writes = setup(true);
    await select();
    fireEvent.change(screen.getByLabelText("会议名称"), {
      target: { value: meeting().display_name },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建会议记录" }));
    await screen.findByText(/提交结果尚未确认/);
    fireEvent.click(screen.getByRole("button", { name: "文件转写" }));
    fireEvent.click(screen.getByRole("button", { name: "会议记录" }));
    fireEvent.click(await screen.findByRole("button", { name: "按原键重放" }));
    await screen.findByLabelText("当前会议编号");
    expect(writes).toHaveLength(2);
    expect(writes[0]).toEqual(writes[1]);
  });
});
