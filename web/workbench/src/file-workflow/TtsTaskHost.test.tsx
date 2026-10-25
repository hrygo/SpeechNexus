import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { File as NodeFile, Blob as NodeBlob } from "node:buffer";
import { webcrypto } from "node:crypto";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { FileTaskHost } from "./FileTaskHost";
import { FileTaskSession } from "./file-task-session";
import { inputFingerprint, textUploadFile } from "./input-fingerprint";
import { SpeechFileClient, parse } from "./speech-file-client";
import type { WorkbenchSaveClient } from "./TtsTaskResult";

const play = vi.fn<() => Promise<void>>();
const createUrl = vi.fn(() => "blob:synthetic");
const revokeUrl = vi.fn<(url: string) => void>();
const key = "speechnexus.workbench.tts-task.v1";
const asrKey = "speechnexus.workbench.asr-task.v1";
const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const uuid = "9f712fc4-943f-4171-b910-9132c44927a2";
const selection = {
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
};
const text = "合成测试文本，不是真实书稿。";
function stored() {
  return {
    ...selection,
    version: 2,
    assetId: "asset-text",
    workspaceId: "ws-default",
    createKey: uuid,
    cancelKey: uuid,
    jobId: "job-demo",
    pending: null,
    operationId: null,
    cursor: null,
  };
}
function fixture() {
  const data = new Map<string, string>();
  const storage = {
    getItem: (name: string) => data.get(name) ?? null,
    setItem: (name: string, value: string) => {
      data.set(name, value);
    },
    removeItem: (name: string) => {
      data.delete(name);
    },
  };
  const client = new SpeechFileClient("http://127.0.0.1");
  const asset = parse("Asset", {
    ...example("ready-audio"),
    id: "asset-text",
    purpose: "text_input",
    media_type: "text/plain",
    duration_ms: null,
    sample_rate_hz: null,
    channels: null,
  });
  const job = parse("Job", { ...example("queued-job"), kind: "tts" });
  const artifact = parse("Artifact", {
    ...example("artifact"),
    id: "artifact-audio",
    kind: "audio",
    media_type: "audio/wav",
  });
  const complete = parse("Job", {
    ...job,
    status: "succeeded",
    cleanup_state: "complete",
    artifacts: [
      {
        artifact_id: artifact.id,
        kind: artifact.kind,
        media_type: artifact.media_type,
        bytes: artifact.bytes,
        sha256: artifact.sha256,
        receipt_id: "receipt-demo",
      },
    ],
    result: { kind: "tts", audio_artifact_id: artifact.id, receipt_id: "receipt-demo" },
  });
  const upload = vi.spyOn(client, "uploadText").mockResolvedValue(asset);
  const create = vi.spyOn(client, "create").mockResolvedValue(job);
  const getJob = vi.spyOn(client, "getJob").mockResolvedValue(job);
  const getAsset = vi.spyOn(client, "getAsset").mockResolvedValue(asset);
  const download = vi.spyOn(client, "download").mockResolvedValue({
    artifact,
    blob: new NodeBlob(["synthetic WAV"], { type: "audio/wav" }) as Blob,
  });
  const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network"));
  return {
    data,
    storage,
    client,
    asset,
    job,
    artifact,
    complete,
    upload,
    create,
    getJob,
    getAsset,
    download,
    fetch,
  };
}
function fill() {
  for (const [label, value] of [
    ["配音模型", selection.model],
    ["声音标识", selection.voiceId],
    ["声音版本", selection.voiceRevision],
    ["参数标识", selection.profileId],
    ["参数版本", selection.profileRevision],
    ["配音文本", text],
  ])
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
}
function submit() {
  fireEvent.submit(screen.getByLabelText("配音文本").closest("form")!);
}
async function readComplete(f: ReturnType<typeof fixture>, workbench?: WorkbenchSaveClient) {
  f.data.set(key, JSON.stringify(stored()));
  f.getJob.mockResolvedValue(f.complete);
  const view = render(
    <FileTaskHost kind="tts" client={f.client} storage={f.storage} workbench={workbench} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
  await screen.findByText("job-demo");
  return view;
}
beforeEach(() => {
  vi.stubGlobal("File", NodeFile);
  vi.stubGlobal("Blob", NodeBlob);
  vi.stubGlobal("crypto", webcrypto);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
  play.mockReset().mockResolvedValue(undefined);
  createUrl.mockClear();
  revokeUrl.mockClear();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(play);
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: createUrl,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: revokeUrl,
  });
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("production text synthesis host", () => {
  it("mounts empty with no network/default fake choices and owns a separate slot", async () => {
    const f = fixture();
    f.data.set(asrKey, "foreign-asr-record");
    render(
      <StrictMode>
        <FileTaskHost kind="tts" client={f.client} storage={f.storage} />
      </StrictMode>,
    );
    expect(screen.getByLabelText("配音模型")).toHaveValue("");
    expect(screen.getByRole("button", { name: "创建配音任务" })).toBeDisabled();
    expect(screen.queryByText("选择音频开始转写。")).not.toBeInTheDocument();
    await act(async () => {
      await Promise.resolve();
    });
    expect(f.fetch).not.toHaveBeenCalled();
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.data.get(asrKey)).toBe("foreign-asr-record");
  });
  it("keeps invalid preflight editable, then fixes choices and excludes text from storage", async () => {
    const f = fixture();
    render(<FileTaskHost kind="tts" client={f.client} storage={f.storage} />);
    fill();
    fireEvent.change(screen.getByLabelText("声音标识"), { target: { value: "invalid voice" } });
    submit();
    await screen.findByText("配音参数无效，请检查文本和固定版本。");
    expect(screen.getByLabelText("声音标识")).toBeEnabled();
    expect(screen.getByLabelText("配音文本")).toHaveValue(text);
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.data.has(key)).toBe(false);
    fireEvent.change(screen.getByLabelText("声音标识"), { target: { value: selection.voiceId } });
    submit();
    await screen.findByText("job-demo");
    expect(f.upload.mock.calls[0]?.[0]).toBe(text);
    expect(f.create.mock.calls[0]?.[0]).toMatchObject({
      kind: "tts",
      model: selection.model,
      input: { text_asset_id: "asset-text" },
      voice: { id: selection.voiceId, revision: "1" },
      profile: { id: selection.profileId, revision: "2" },
      output: { audio_format: "wav", save_artifacts: true },
    });
    expect(screen.getByLabelText("声音标识")).toBeDisabled();
    expect(screen.getByLabelText("配音文本")).toHaveValue("");
    expect(f.data.get(key)).not.toContain(text);
  });
  it("sets inputFixed only after valid intent and for restored records", async () => {
    const f = fixture();
    const session = new FileTaskSession(f.client);
    expect(session.getSnapshot().inputFixed).toBe(false);
    await session.startText(text, { ...selection, voiceId: "invalid voice" });
    expect(session.getSnapshot().inputFixed).toBe(false);
    await session.startText(text, selection);
    expect(session.getSnapshot().inputFixed).toBe(true);
    session.dispose();
  });
  it.each([1, "2", "corrupt"])("preserves and blocks wrong storage version %s", (version) => {
    const f = fixture();
    const raw = version === "corrupt" ? "{" : JSON.stringify({ ...stored(), version });
    f.data.set(key, raw);
    render(<FileTaskHost kind="tts" client={f.client} storage={f.storage} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可用");
    expect(f.data.get(key)).toBe(raw);
    expect(f.upload).not.toHaveBeenCalled();
  });
  it("restores unknown create without requests and replays original choices/key", async () => {
    const f = fixture();
    f.data.set(key, JSON.stringify({ ...stored(), pending: "create", jobId: null }));
    render(<FileTaskHost kind="tts" client={f.client} storage={f.storage} />);
    expect(screen.getByLabelText("声音版本")).toHaveValue("1");
    expect(screen.getByLabelText("配音模型")).toBeDisabled();
    expect(f.getAsset).not.toHaveBeenCalled();
    expect(f.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
    await screen.findByText("job-demo");
    expect(f.create.mock.calls[0]?.[1]).toBe(uuid);
    expect(f.create.mock.calls[0]?.[0]).toMatchObject({
      kind: "tts",
      voice: { id: selection.voiceId, revision: "1" },
    });
    expect(f.upload).not.toHaveBeenCalled();
  });
  it.each([false, true])("recovers original upload: durable hint=%s", async (hint) => {
    const f = fixture();
    f.data.set(
      key,
      JSON.stringify({
        ...stored(),
        version: hint ? 6 : 4,
        assetId: null,
        workspaceId: null,
        jobId: null,
        pending: "upload",
        uploadKey: uuid,
        inputFingerprint: await inputFingerprint(textUploadFile(text)),
        assetMediaType: null,
        assetBytes: null,
        assetSha256: null,
        ...(hint ? { assetHintId: "asset-text" } : {}),
      }),
    );
    render(<FileTaskHost kind="tts" client={f.client} storage={f.storage} />);
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.getAsset).not.toHaveBeenCalled();
    if (hint) {
      fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
      await screen.findByRole("button", { name: "继续创建原任务" });
      expect(f.upload).not.toHaveBeenCalled();
      expect(f.create).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "继续创建原任务" }));
    } else {
      fireEvent.change(screen.getByLabelText("配音文本"), { target: { value: "different" } });
      submit();
      await screen.findByText("输入与原素材不一致，请重新提供原文件或原文本。");
      expect(f.upload).not.toHaveBeenCalled();
      fireEvent.change(screen.getByLabelText("配音文本"), { target: { value: text } });
      submit();
    }
    await screen.findByText("job-demo");
    expect(f.create.mock.calls[0]?.[1]).toBe(uuid);
    if (!hint) expect(f.upload.mock.calls[0]?.slice(0, 2)).toEqual([text, uuid]);
  });
  it.each(
    ["stop", "hidden", "pagehide", "unmount"].flatMap((action) => [
      [action, "试听音频"],
      [action, "下载 WAV"],
    ]),
  )("ignores late WAV after %s during %s", async (action, control) => {
    const f = fixture();
    let resolve!: (value: Awaited<ReturnType<SpeechFileClient["download"]>>) => void;
    f.download.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const view = await readComplete(f);
    fireEvent.click(screen.getByRole("button", { name: control }));
    await waitFor(() => expect(resolve).toBeDefined());
    if (action === "stop") fireEvent.click(screen.getByRole("button", { name: "停止试听" }));
    if (action === "pagehide")
      act(() => {
        window.dispatchEvent(new Event("pagehide"));
      });
    if (action === "hidden") {
      vi.spyOn(document, "hidden", "get").mockReturnValue(true);
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
    }
    if (action === "unmount") view.unmount();
    await act(async () => {
      resolve({ artifact: f.artifact, blob: new NodeBlob(["WAV"]) as Blob });
      await Promise.resolve();
    });
    expect(createUrl).not.toHaveBeenCalled();
    expect(play).not.toHaveBeenCalled();
  });
  it("loads only WAV and releases its URL on stop", async () => {
    const f = fixture();
    await readComplete(f);
    fireEvent.click(screen.getByRole("button", { name: "试听音频" }));
    await waitFor(() => expect(play).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("任务配音播放器")).toHaveAttribute("src", "blob:synthetic");
    fireEvent.click(screen.getByRole("button", { name: "停止试听" }));
    expect(revokeUrl).toHaveBeenCalledWith("blob:synthetic");
    expect(screen.getByLabelText("任务配音播放器")).not.toHaveAttribute("src");
    f.download.mockResolvedValue({
      artifact: parse("Artifact", example("artifact")),
      blob: new NodeBlob(["JSON"]) as Blob,
    });
    fireEvent.click(screen.getByRole("button", { name: "试听音频" }));
    await screen.findByText(/音频格式与本次 WAV 请求不一致/);
    expect(createUrl).toHaveBeenCalledTimes(1);
  });
  it("downloads WAV with a fixed filename and revokes the temporary URL", async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const f = fixture();
    await readComplete(f);
    fireEvent.click(screen.getByRole("button", { name: "下载 WAV" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect((click.mock.instances[0] as HTMLAnchorElement | undefined)?.download).toBe("speech.wav");
    await waitFor(() => expect(revokeUrl).toHaveBeenCalledWith("blob:synthetic"));
  });
});

type SaveMock = Mock<WorkbenchSaveClient["saveProjectAsset"]>;

function workbenchFake(
  save: SaveMock = vi
    .fn<WorkbenchSaveClient["saveProjectAsset"]>()
    .mockResolvedValue({ id: "asset-copy" }),
): WorkbenchSaveClient & { saveProjectAsset: SaveMock } {
  return {
    projects: vi.fn<WorkbenchSaveClient["projects"]>(() =>
      Promise.resolve({ data: [{ id: "project-demo", name: "合成项目" }] }),
    ),
    saveProjectAsset: save,
  };
}

async function armed(f: ReturnType<typeof fixture>, workbench: WorkbenchSaveClient) {
  await readComplete(f, workbench);
  await screen.findByRole("option", { name: "合成项目" });
  fireEvent.change(screen.getByLabelText("目标项目"), { target: { value: "project-demo" } });
}

it("copies the committed audio into the chosen project and reports the saved copy", async () => {
  const f = fixture();
  const workbench = workbenchFake();
  await armed(f, workbench);
  fireEvent.click(screen.getByRole("button", { name: "保存到工作台" }));
  await screen.findByText("已保存副本 asset-copy");
  const calls = workbench.saveProjectAsset.mock.calls;
  expect(calls).toHaveLength(1);
  expect(calls[0]).toEqual([
    "project-demo",
    { source_artifact_id: "artifact-audio", source_receipt_id: "receipt-demo" },
    expect.any(String),
  ]);
});

it("replays one idempotency key after an unknown outcome and never fakes a copy", async () => {
  const f = fixture();
  const save = vi
    .fn()
    .mockRejectedValueOnce({ status: 503 })
    .mockResolvedValueOnce({ id: "asset-copy" });
  const workbench = workbenchFake(save);
  await armed(f, workbench);
  fireEvent.click(screen.getByRole("button", { name: "保存到工作台" }));
  await screen.findByText(/保存结果未确认/);
  expect(screen.queryByText(/已保存副本/)).toBe(null);
  fireEvent.click(screen.getByRole("button", { name: "保存到工作台" }));
  await screen.findByText("已保存副本 asset-copy");
  const [first, second] = save.mock.calls;
  expect(first![2]).toBe(second![2]);
  expect(first![0]).toBe("project-demo");
});

it("reports an absent project list instead of inventing a destination", async () => {
  const f = fixture();
  const workbench = {
    projects: vi.fn().mockRejectedValue({ status: 503 }),
    saveProjectAsset: vi.fn(),
  };
  await readComplete(f, workbench);
  await screen.findByText(/无法读取项目列表/);
  expect(screen.getByRole("button", { name: "保存到工作台" })).toBeDisabled();
});
