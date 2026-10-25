import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MeetingHost } from "./MeetingHost";
import { WorkbenchClient, WorkbenchResponseError, parse } from "../manuscripts/workbench-client";
import { sample, meeting, source, job } from "./meeting-fixtures";
function setup(enabled = false) {
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      data.set(k, v);
    },
  };
  const client = new WorkbenchClient(vi.fn());
  vi.spyOn(client, "status").mockResolvedValue({
    ...parse("WorkbenchStatus", sample("getWorkbenchStatus")),
    llm_enabled: enabled,
  });
  vi.spyOn(client, "projects").mockResolvedValue(parse("ProjectList", sample("listProjects")));
  vi.spyOn(client, "project").mockResolvedValue(parse("Project", sample("createProject")));
  vi.spyOn(client, "transcripts").mockResolvedValue(
    parse("TranscriptList", sample("listTranscripts")),
  );
  const read = vi.spyOn(client, "view").mockResolvedValue(source());
  const create = vi.spyOn(client, "createMeeting").mockResolvedValue(meeting());
  const current = vi.spyOn(client, "getMeeting").mockResolvedValue(meeting());
  const getMinutes = vi.spyOn(client, "getMinutes");
  const minutes = vi.spyOn(client, "createMinutes");
  render(<MeetingHost client={client} storage={storage} />);
  return { client, create, minutes, storage, read, current, getMinutes };
}
async function select() {
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
describe("formal Meeting workbench", () => {
  it("removes a successful server minutes body on missing-current read while preserving the local draft", async () => {
    const f = setup();
    await select();
    f.current.mockResolvedValue({
      ...meeting(),
      status: "minutes_ready",
      sealed_revision: "2",
      ended_at: "2026-10-24T00:00:00Z",
      minutes_id: job().id,
    });
    f.read.mockResolvedValue(source("2"));
    f.getMinutes.mockResolvedValue({
      ...job(),
      status: "succeeded",
      source_revision: "2",
      minutes_text: "旧服务端正文",
      artifact_id: null,
      error: null,
    });
    fireEvent.change(screen.getByLabelText("会议名称"), {
      target: { value: meeting().display_name },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建会议记录" }));
    await screen.findByLabelText("纪要正文");
    f.current.mockRejectedValueOnce(
      new WorkbenchResponseError(
        404,
        parse("ErrorResponse", {
          error: {
            code: "resource_not_found",
            type: "invalid_request_error",
            message: "synthetic",
            param: null,
            request_id: "request-demo",
            retryable: false,
          },
        }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "刷新当前记录" }));
    await screen.findByText(/已不可读取/);
    expect(screen.queryByLabelText("纪要正文")).not.toBeInTheDocument();
    expect(screen.queryByText(/纪要：生成完成/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("会议校对正文")).toHaveValue("原文");
  });
  it("uses real source discovery and keeps base records available with LLM disabled", async () => {
    const f = setup();
    await select();
    expect(screen.getByLabelText("会议校对正文")).toHaveValue("原文");
    fireEvent.change(screen.getByLabelText("会议名称"), {
      target: { value: meeting().display_name },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建会议记录" }));
    await screen.findByLabelText("当前会议编号");
    expect(f.create).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "生成纪要" })).toBeDisabled();
    expect(screen.getByText(/尚未配置可用的纪要模板/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "封存校对稿" })).toBeEnabled();
    expect(f.minutes).not.toHaveBeenCalled();
  });
  it("retains and exposes recovery on an unknown mutation without auto replacement", async () => {
    const f = setup();
    await select();
    f.create.mockRejectedValueOnce(Error("lost reply"));
    fireEvent.change(screen.getByLabelText("会议名称"), {
      target: { value: meeting().display_name },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建会议记录" }));
    await screen.findByText(/提交结果尚未确认/);
    expect(screen.getByRole("button", { name: "按原键重放" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "创建会议记录" })).toBeDisabled();
    expect(f.create).toHaveBeenCalledTimes(1);
  });
  it("fails closed on corrupt storage without clearing it or sending writes", () => {
    const storage = { getItem: () => "{", setItem: vi.fn() };
    const transport = vi.fn<typeof fetch>();
    render(<MeetingHost client={new WorkbenchClient(transport)} storage={storage} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可用");
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(transport).not.toHaveBeenCalled();
  });
});
