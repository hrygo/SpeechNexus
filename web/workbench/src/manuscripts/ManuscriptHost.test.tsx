import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { ManuscriptHost } from "./ManuscriptHost";
import { journalKey } from "./editor-journal";
import { WorkbenchClient, WorkbenchResponseError, parse } from "./workbench-client";

function example(operation: string) {
  return structuredClone(
    fixtures.examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === "response" &&
        e.valid &&
        typeof e.status === "number" &&
        [200, 201].includes(e.status),
    )!.value.body,
  );
}
function view(revision = "1", text = "合成原文") {
  const value = parse("TranscriptView", example("getTranscript"));
  value.transcript.head_revision = revision;
  Object.assign(value.revision, {
    revision,
    text,
    status: "draft",
    source_revision: revision === "1" ? null : "1",
  });
  return value;
}
function setup() {
  const data = new Map<string, string>();
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const client = new WorkbenchClient(vi.fn());
  const status = vi
    .spyOn(client, "status")
    .mockResolvedValue(parse("WorkbenchStatusV2", example("getWorkbenchStatusV2")));
  vi.spyOn(client, "projects").mockResolvedValue(parse("ProjectList", example("listProjects")));
  vi.spyOn(client, "project").mockResolvedValue(parse("Project", example("createProject")));
  vi.spyOn(client, "transcripts").mockResolvedValue(
    parse("TranscriptList", example("listTranscripts")),
  );
  const read = vi.spyOn(client, "view").mockResolvedValue(view());
  const revise = vi.spyOn(client, "revise").mockResolvedValue(view("2", "校对").revision);
  return { data, storage, client, status, read, revise };
}
async function choose() {
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic project" }));
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic transcript" }));
  await screen.findByDisplayValue("合成原文");
}

describe("production manuscript host", () => {
  it("exposes a new speech import while preserving an existing editor", async () => {
    const f = setup();
    const host = render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await choose();
    host.rerender(
      <ManuscriptHost
        client={f.client}
        storage={f.storage}
        speechSource={{
          kind: "speech_artifact",
          artifact_id: "artifact-new",
          receipt_id: "receipt-new",
        }}
      />,
    );
    expect(screen.getByRole("group", { name: "待导入语音原稿" })).toBeVisible();
    expect(screen.getByLabelText("校对草稿")).toHaveValue("合成原文");
  });
  it("explicitly imports the fixed speech artifact and receipt into the selected project", async () => {
    const f = setup();
    const source = {
      kind: "speech_artifact",
      artifact_id: "artifact-source",
      receipt_id: "receipt-source",
    } as const;
    const imported = view();
    imported.revision.status = "sealed";
    imported.transcript.source_artifact_id = source.artifact_id;
    imported.transcript.source_receipt_id = source.receipt_id;
    const create = vi.spyOn(f.client, "createTranscript").mockResolvedValue(imported);
    render(<ManuscriptHost client={f.client} storage={f.storage} speechSource={source} />);
    fireEvent.click(await screen.findByRole("button", { name: "Synthetic project" }));
    expect(create).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "导入语音原稿" })).toBeEnabled());
    fireEvent.change(await screen.findByLabelText("稿件名称"), { target: { value: "语音原稿" } });
    fireEvent.click(screen.getByRole("button", { name: "导入语音原稿" }));
    await screen.findByText("原稿已保存至当前部署。");
    expect(create).toHaveBeenCalledWith(
      "project-demo",
      { display_name: "语音原稿", source },
      expect.any(String),
    );
    expect(screen.getByLabelText("校对草稿")).toHaveValue("合成原文");
  });

  it("offers a fixed revision text download after an authoritative read", async () => {
    const f = setup();
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await choose();
    fireEvent.change(screen.getByLabelText("校对草稿"), { target: { value: "未保存的新草稿" } });
    expect(screen.getByRole("button", { name: "下载固定版本 TXT" })).toBeEnabled();
    expect(
      screen.getByText("下载服务端已读取的版本 1，不包含未保存草稿。", { exact: true }),
    ).toBeVisible();
  });
  it("reads live lists under StrictMode and appends an exact correction", async () => {
    const f = setup();
    render(
      <StrictMode>
        <ManuscriptHost client={f.client} storage={f.storage} />
      </StrictMode>,
    );
    await choose();
    expect(f.status).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("校对草稿"), { target: { value: "校对" } });
    f.read.mockResolvedValueOnce(view("2", "校对"));
    fireEvent.click(screen.getByRole("button", { name: "保存新修订" }));
    await screen.findByText("新修订已保存至当前部署。");
    expect(f.revise.mock.calls[0]![1]).toEqual({
      text: "校对",
      expected_revision: "1",
      source_revision: "1",
    });
    expect(screen.getByLabelText("校对草稿")).toHaveValue("校对");
  });

  it("retains an unknown intent across remount and replays only on explicit action", async () => {
    const f = setup();
    const first = render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await choose();
    fireEvent.change(screen.getByLabelText("校对草稿"), { target: { value: "校对" } });
    f.revise.mockRejectedValueOnce(new Error("private-synthetic"));
    fireEvent.click(screen.getByRole("button", { name: "保存新修订" }));
    await screen.findByRole("button", { name: "按原键重放" });
    const original = f.revise.mock.calls[0];
    first.unmount();
    await act(() => Promise.resolve());
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    const replay = await screen.findByRole("button", { name: "按原键重放" });
    await waitFor(() => expect(replay).toBeEnabled());
    expect(f.revise).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("校对草稿")).toBeDisabled();
    f.read.mockResolvedValueOnce(view("2", "校对"));
    fireEvent.click(replay);
    await screen.findByText("新修订已保存至当前部署。");
    expect(f.revise.mock.calls[1]).toEqual(original);
    expect(screen.queryByText("private-synthetic")).not.toBeInTheDocument();
  });

  it("compares conflict text and requires explicit adoption of the latest baseline", async () => {
    const f = setup();
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await choose();
    fireEvent.change(screen.getByLabelText("校对草稿"), { target: { value: "我的草稿" } });
    f.revise.mockRejectedValueOnce(
      new WorkbenchResponseError(
        409,
        parse("ErrorResponse", {
          error: {
            code: "revision_conflict",
            type: "invalid_request_error",
            message: "private",
            request_id: "request-demo",
            param: null,
            retryable: false,
          },
        }),
      ),
    );
    f.read.mockResolvedValueOnce(view("2", "其他编辑者的新稿"));
    fireEvent.click(screen.getByRole("button", { name: "保存新修订" }));
    await screen.findByLabelText("服务端最新版本");
    expect(screen.getByLabelText("服务端最新版本")).toHaveTextContent("其他编辑者的新稿");
    expect(screen.getByLabelText("校对草稿")).toHaveValue("我的草稿");
    expect(screen.getByRole("button", { name: "保存新修订" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "保留草稿并基于最新版本继续" }));
    expect(screen.getByRole("button", { name: "保存新修订" })).toBeEnabled();
    expect(f.revise).toHaveBeenCalledTimes(1);
  });

  it("keeps local recovery after storage corruption and makes no API calls", () => {
    const f = setup();
    f.data.set(journalKey, "{corrupt");
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可用");
    expect(f.status).not.toHaveBeenCalled();
    expect(f.data.get(journalKey)).toBe("{corrupt");
  });

  it("requires explicit confirmation to discard unsaved text", async () => {
    const f = setup();
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await choose();
    fireEvent.change(screen.getByLabelText("校对草稿"), { target: { value: "还没保存" } });
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    expect(screen.getByLabelText("校对草稿")).toHaveValue("还没保存");
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.getByLabelText("校对草稿")).toHaveValue("还没保存");
    expect(screen.getByRole("button", { name: "放弃本地修改" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "放弃本地修改" }));
    fireEvent.click(screen.getByRole("button", { name: "确认放弃本地修改" }));
    expect(screen.queryByLabelText("校对草稿")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "项目与稿件", level: 2 })).toHaveFocus();
  });

  it("reports disabled service without fictional sample rows", async () => {
    const f = setup();
    const status = parse("WorkbenchStatusV2", example("getWorkbenchStatusV2"));
    status.workbench_enabled = false;
    f.status.mockResolvedValue(status);
    render(<ManuscriptHost client={f.client} storage={f.storage} />);
    await screen.findByText("当前部署尚未启用工作台，已有本地草稿仍保留。");
    expect(screen.queryByRole("button", { name: "Synthetic project" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建项目" })).toBeDisabled();
  });
});
