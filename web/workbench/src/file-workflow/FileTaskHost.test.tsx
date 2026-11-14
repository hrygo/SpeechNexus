import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { File as NodeFile, Blob as NodeBlob } from "node:buffer";
import { webcrypto } from "node:crypto";
import { StrictMode } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { FileTaskHost } from "./FileTaskHost";
import { inputFingerprint } from "./input-fingerprint";
import { FileTaskSession } from "./file-task-session";
import { SessionTaskJournal } from "./task-journal";
import { SpeechFileClient, SpeechResponseError, parse } from "./speech-file-client";

const key = "speechnexus.workbench.asr-task.v1";
const example = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);
const uuid = "9f712fc4-943f-4171-b910-9132c44927a2";
function stored(jobId: string | null = "job-demo", pending: "create" | "cancel" | null = null) {
  return {
    version: 1,
    model: "asr-quality",
    assetId: "asset-demo",
    workspaceId: "ws-default",
    createKey: uuid,
    cancelKey: uuid,
    jobId,
    pending,
    operationId: null,
    cursor: null,
  };
}
function fixture() {
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      data.set(k, v);
    },
    removeItem: (k: string) => {
      data.delete(k);
    },
  };
  const client = new SpeechFileClient("http://127.0.0.1");
  const upload = vi
    .spyOn(client, "upload")
    .mockResolvedValue(parse("Asset", example("ready-audio")));
  const create = vi.spyOn(client, "create").mockResolvedValue(parse("Job", example("queued-job")));
  const getJob = vi.spyOn(client, "getJob").mockResolvedValue(parse("Job", example("queued-job")));
  const getAsset = vi
    .spyOn(client, "getAsset")
    .mockResolvedValue(parse("Asset", example("ready-audio")));
  const cancel = vi
    .spyOn(client, "cancel")
    .mockResolvedValue(parse("JobCancellation", example("cancel-job")));
  const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("unexpected network"));
  return { data, storage, client, upload, create, getJob, getAsset, cancel, fetch };
}
function selectInput(model = "asr-quality") {
  fireEvent.change(screen.getByLabelText("转写模型"), { target: { value: model } });
  fireEvent.change(screen.getByLabelText("音频文件"), {
    target: {
      files: [new NodeFile(["synthetic private audio"], "private-file.wav", { type: "audio/wav" })],
    },
  });
}
beforeEach(() => {
  vi.stubGlobal("File", NodeFile);
  vi.stubGlobal("Blob", NodeBlob);
  vi.stubGlobal("crypto", webcrypto);
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("production Workbench file host", () => {
  it.each([false, true])(
    "offers only the matching committed ASR result for import (wrong receipt: %s)",
    async (wrongReceipt) => {
      const f = fixture();
      const job = parse("Job", example("succeeded-job"));
      if (wrongReceipt) job.artifacts[0]!.receipt_id = "different-receipt";
      f.getJob.mockResolvedValue(job);
      f.data.set(key, JSON.stringify(stored()));
      const importResult = vi.fn();
      render(
        <FileTaskHost client={f.client} storage={f.storage} onImportTranscript={importResult} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
      const button = await screen.findByRole("button", { name: "导入到项目稿件" });
      expect(importResult).not.toHaveBeenCalled();
      if (wrongReceipt) expect(button).toBeDisabled();
      else {
        expect(button).toBeEnabled();
        fireEvent.click(button);
        expect(importResult).toHaveBeenCalledExactlyOnceWith(job.result);
      }
    },
  );
  it("permits correction after a local save failure followed by the first actual rejected upload", async () => {
    const f = fixture();
    vi.spyOn(f.storage, "setItem").mockImplementationOnce(() => {
      throw new Error("synthetic storage full");
    });
    f.upload.mockRejectedValueOnce(
      new SpeechResponseError(
        413,
        parse("ErrorResponse", {
          error: {
            code: "asset_too_large",
            type: "invalid_request_error",
            message: "private",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      ),
    );
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    selectInput();
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await waitFor(() => expect(screen.getByRole("button", { name: "按原键重放" })).toBeEnabled());
    expect(f.upload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
    await screen.findByRole("button", { name: "准备新任务" });
    expect(f.upload).toHaveBeenCalledTimes(1);
    expect(f.create).not.toHaveBeenCalled();
  });

  it("clears first-rejection evidence when the original input is later explicitly accepted", async () => {
    const f = fixture();
    f.upload.mockRejectedValueOnce(
      new SpeechResponseError(
        422,
        parse("ErrorResponse", {
          error: {
            code: "asset_empty",
            type: "invalid_request_error",
            message: "private",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      ),
    );
    const session = new FileTaskSession(f.client, new SessionTaskJournal(f.storage, key));
    const file = new NodeFile(["synthetic"], "synthetic.wav", { type: "audio/wav" }) as File;
    await session.start(file, "asr-quality");
    expect(session.getSnapshot().uploadRejected).toBe(true);
    await session.resumeFile(file);
    expect(session.getSnapshot().job?.id).toBe("job-demo");
    expect(session.getSnapshot().uploadRejected).toBe(false);
    session.dispose();
  });

  it.each([
    [413, "asset_too_large"],
    [422, "asset_empty"],
  ] as const)("allows explicit correction after first %i %s rejection", async (status, code) => {
    const f = fixture();
    f.upload.mockRejectedValueOnce(
      new SpeechResponseError(
        status,
        parse("ErrorResponse", {
          error: {
            code,
            type: "invalid_request_error",
            message: "private-rejection-body",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      ),
    );
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    selectInput();
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await screen.findByRole("button", { name: "准备新任务" });
    expect(f.create).not.toHaveBeenCalled();
    expect(f.data.has(key)).toBe(true);
    const first = JSON.parse(f.data.get(key)!) as { uploadKey: string };
    fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
    fireEvent.click(screen.getByRole("button", { name: "结束本页任务并新建" }));
    expect(screen.getByLabelText("转写模型")).toHaveFocus();
    fireEvent.change(screen.getByLabelText("转写模型"), { target: { value: "asr-quality" } });
    fireEvent.change(screen.getByLabelText("音频文件"), {
      target: { files: [new NodeFile(["tiny"], "smaller.wav", { type: "audio/wav" })] },
    });
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await screen.findByText("job-demo");
    expect(f.upload.mock.calls[1]?.[1]).not.toBe(first.uploadKey);
    expect(f.upload.mock.calls[1]?.[0].size).toBe(4);
    expect(f.create).toHaveBeenCalledTimes(1);
  });
  it("offers explicit abandonment after a deterministic create rejection, and a reload keeps it settled", async () => {
    const f = fixture();
    f.create.mockRejectedValue(
      new SpeechResponseError(
        422,
        parse("ErrorResponse", {
          error: {
            code: "job_binding_incomplete",
            type: "invalid_request_error",
            message: "Synthetic terminal rejection",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      ),
    );
    const view = render(<FileTaskHost client={f.client} storage={f.storage} />);
    selectInput();
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await screen.findByRole("button", { name: "准备新任务" });
    expect(f.create).toHaveBeenCalledTimes(1);
    // The protocol answered definitively: no unknown alert, and the input stays locked so
    // no replacement intent can start before the user abandons this one explicitly.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/不能重放，刷新也/)).toBeInTheDocument();
    expect(screen.getByLabelText("音频文件")).toBeDisabled();
    expect(f.data.get(key)).toContain('"settled":"terminal"');
    const spentKey = f.create.mock.calls[0]?.[1];

    // Reload keeps the refusal settled instead of reviving it as "result unknown".
    view.unmount();
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/不能重放，刷新也/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "准备新任务" })).toBeInTheDocument();
    // Counterexample 3 also holds across a reload: the input stays locked and nothing is
    // resubmitted until the user explicitly abandons the spent intent.
    expect(screen.getByLabelText("音频文件")).toBeDisabled();
    expect(f.create).toHaveBeenCalledTimes(1);

    // Abandonment is explicit and two-stepped; only its confirmation removes the record.
    fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
    fireEvent.click(screen.getByRole("button", { name: "保留本页任务" }));
    expect(f.data.has(key)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
    expect(screen.getByRole("button", { name: "放弃这次意图并新建" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放弃这次意图并新建" }));
    expect(f.data.has(key)).toBe(false);
    expect(screen.getByLabelText("转写模型")).toHaveFocus();

    // A replacement intent uses a fresh idempotency key; the spent key is never reused.
    fireEvent.change(screen.getByLabelText("转写模型"), { target: { value: "asr-quality" } });
    fireEvent.change(screen.getByLabelText("音频文件"), {
      target: { files: [new NodeFile(["synthetic"], "new.wav", { type: "audio/wav" })] },
    });
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await waitFor(() => expect(f.create).toHaveBeenCalledTimes(2));
    expect(f.create.mock.calls[1]?.[1]).not.toBe(spentKey);
  });

  it("shows a reloaded settled cancellation as refused, not unknown, and offers only the explicit end", async () => {
    const f = fixture();
    f.data.set(key, JSON.stringify({ ...stored("job-demo", "cancel"), settled: "terminal" }));
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    // No unknown alert: the protocol already answered this cancellation (#723).
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/不能重放，刷新也/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "准备新任务" })).toBeInTheDocument();
    // The Job the refusal named stays readable, but the spent cancel intent is never
    // replayed or re-sent, so its button stays disabled.
    fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
    await screen.findByText("job-demo");
    expect(screen.getByRole("button", { name: "准备取消" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "按原键重放" })).toBeDisabled();
    // Only the explicit, two-stepped end clears the record.
    fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
    fireEvent.click(screen.getByRole("button", { name: "放弃这次意图并新建" }));
    expect(f.data.has(key)).toBe(false);
    expect(f.getJob).toHaveBeenCalled();
    expect(f.cancel).not.toHaveBeenCalled();
    expect(f.create).not.toHaveBeenCalled();
  });

  it("gives the user an exit after a 413 body-bound refusal, without claiming no execution", async () => {
    // #729: the 413 is answered before the key lookup, so the intent cannot be "settled"
    // like a post-key 4xx; but replaying the same oversized body can never succeed. The
    // page must therefore allow explicit abandonment, say only that the request was
    // refused, and never claim the server did not execute anything.
    const f = fixture();
    f.create.mockRejectedValue(
      new SpeechResponseError(
        413,
        parse("ErrorResponse", {
          error: {
            code: "job_request_too_large",
            type: "invalid_request_error",
            message: "Synthetic body bound",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      ),
    );
    const view = render(<FileTaskHost client={f.client} storage={f.storage} />);
    selectInput();
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await screen.findByRole("button", { name: "准备新任务" });
    expect(f.create).toHaveBeenCalledTimes(1);
    // The protocol refused this request: no "result unconfirmed" alert.
    expect(screen.queryByText(/请求结果尚未确认/)).not.toBeInTheDocument();
    // The notice and the refusal paragraph both name the bound; neither names an outcome.
    expect(screen.getAllByText(/请求体超过本部署的接收上限/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/结果未知/)).not.toBeInTheDocument();
    expect(screen.queryByText(/未执行/)).not.toBeInTheDocument();
    expect(f.data.get(key)).toContain('"settled":"unreplayable"');
    const spentKey = f.create.mock.calls[0]?.[1];

    // A reload keeps the verdict and still offers the exit instead of rebuilding an
    // unknown submission whose only action is a guaranteed-failing replay.
    view.unmount();
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    expect(screen.getAllByText(/请求体超过本部署的接收上限/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "准备新任务" })).toBeInTheDocument();
    expect(screen.getByLabelText("音频文件")).toBeDisabled();
    expect(f.create).toHaveBeenCalledTimes(1);

    // Abandonment is explicit and yields a fresh idempotency key for the replacement.
    fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
    expect(screen.getByText(/这次请求被拒绝，原意图作废/)).toBeInTheDocument();
    expect(screen.queryByText(/确定性拒绝/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放弃这次意图并新建" }));
    expect(f.data.has(key)).toBe(false);
    fireEvent.change(screen.getByLabelText("转写模型"), { target: { value: "asr-quality" } });
    fireEvent.change(screen.getByLabelText("音频文件"), {
      target: { files: [new NodeFile(["synthetic"], "smaller.wav", { type: "audio/wav" })] },
    });
    f.create.mockResolvedValue(parse("Job", example("queued-job")));
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await waitFor(() => expect(f.create).toHaveBeenCalledTimes(2));
    expect(f.create.mock.calls[1]?.[1]).not.toBe(spentKey);
  });

  it("fails safe when a recovery record carries fields this format does not know", () => {
    const f = fixture();
    // A reader from before #707 counts keys strictly, so it rejects a #707 settled record
    // as invalid instead of picking a shape; the Host must keep it and submit nothing.
    f.data.set(
      key,
      JSON.stringify({ ...stored(null, "create"), settled: "terminal", futureApi: 1 }),
    );
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可用");
    expect(screen.queryByRole("button", { name: "上传并转写" })).not.toBeInTheDocument();
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.create).not.toHaveBeenCalled();
    expect(f.data.has(key)).toBe(true);
  });

  it.each([false, true])(
    "never discards a previously unknown upload after later 413; restored=%s",
    async (restored) => {
      const f = fixture();
      const rejected = new SpeechResponseError(
        413,
        parse("ErrorResponse", {
          error: {
            code: "asset_too_large",
            type: "invalid_request_error",
            message: "private",
            retryable: false,
            request_id: "req-demo",
            param: null,
          },
        }),
      );
      if (restored) {
        const file = new NodeFile(["synthetic private audio"], "private-file.wav", {
          type: "audio/wav",
        });
        f.data.set(
          key,
          JSON.stringify({
            ...stored(null),
            version: 3,
            assetId: null,
            workspaceId: null,
            pending: "upload",
            uploadKey: uuid,
            inputFingerprint: await inputFingerprint(file as File),
            assetMediaType: null,
            assetBytes: null,
            assetSha256: null,
          }),
        );
        f.upload.mockRejectedValue(rejected);
      } else
        f.upload
          .mockRejectedValueOnce(new TypeError("lost acknowledgement"))
          .mockRejectedValueOnce(rejected);
      render(<FileTaskHost client={f.client} storage={f.storage} />);
      if (restored) {
        fireEvent.change(screen.getByLabelText("音频文件"), {
          target: {
            files: [
              new NodeFile(["synthetic private audio"], "private-file.wav", { type: "audio/wav" }),
            ],
          },
        });
        fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
      } else {
        selectInput();
        fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "按原键重放" })).toBeEnabled(),
        );
        fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
      }
      await waitFor(() => expect(screen.getByText(/413/)).toBeInTheDocument());
      expect(screen.getByRole("alert")).toHaveTextContent("请求结果尚未确认");
      expect(screen.queryByRole("button", { name: "准备新任务" })).not.toBeInTheDocument();
      expect(f.create).not.toHaveBeenCalled();
      expect(f.data.has(key)).toBe(true);
    },
  );

  it("mounts under StrictMode without any network request or demo controls", async () => {
    const f = fixture();
    render(
      <StrictMode>
        <FileTaskHost client={f.client} storage={f.storage} />
      </StrictMode>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "上传并转写" })).toBeDisabled();
    expect(screen.queryByText("故障场景")).not.toBeInTheDocument();
    expect(f.fetch).not.toHaveBeenCalled();
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.getJob).not.toHaveBeenCalled();
  });
  it.each(["unavailable", "corrupt", "tts", "string-version"])(
    "blocks all submission for %s recovery storage",
    (mode) => {
      const f = fixture();
      if (mode === "unavailable")
        f.storage.getItem = () => {
          throw new Error("private-storage-error");
        };
      else
        f.data.set(
          key,
          mode === "corrupt"
            ? "{"
            : JSON.stringify(
                mode === "tts"
                  ? {
                      ...stored(),
                      version: 2,
                      voiceId: "voice-demo",
                      voiceRevision: "1",
                      profileId: "profile-demo",
                      profileRevision: "2",
                    }
                  : { ...stored(), version: "1" },
              ),
        );
      render(<FileTaskHost client={f.client} storage={f.storage} />);
      expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可用");
      expect(screen.getByRole("alert")).not.toHaveTextContent("private-storage-error");
      expect(screen.queryByRole("button", { name: "上传并转写" })).not.toBeInTheDocument();
      expect(f.upload).not.toHaveBeenCalled();
      if (mode !== "unavailable") expect(f.data.has(key)).toBe(true);
    },
  );
  it("uses the selected file/model, clears input, preserves keys and requires explicit cancel confirmation", async () => {
    const f = fixture();
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    selectInput();
    expect(screen.getByRole("button", { name: "上传并转写" })).toBeEnabled();
    fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
    await screen.findByText("job-demo");
    expect(f.upload).toHaveBeenCalledTimes(1);
    expect(f.create.mock.calls[0]?.[0]).toMatchObject({
      kind: "asr",
      model: "asr-quality",
      output: { transcript_format: "json", save_artifacts: true },
    });
    expect(screen.getByLabelText("音频文件")).toBeDisabled();
    expect(f.data.get(key)).not.toMatch(/private-file|private audio/);
    fireEvent.click(screen.getByRole("button", { name: "准备取消" }));
    const confirm = screen.getByRole("button", { name: "确认取消 job-demo" });
    expect(confirm).toHaveFocus();
    expect(f.cancel).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() => expect(f.cancel).toHaveBeenCalledTimes(1));
    const saved = JSON.parse(f.data.get(key)!) as { cancelKey: string };
    expect(f.cancel.mock.calls[0]?.slice(0, 2)).toEqual(["job-demo", saved.cancelKey]);
    await screen.findByText("取消响应：accepted=true / pending");
    expect(screen.queryByRole("button", { name: "准备新任务" })).not.toBeInTheDocument();
  });
  it("restores an unknown create only on user action, then replays the original key and fixed model", async () => {
    const f = fixture();
    f.data.set(key, JSON.stringify(stored(null, "create")));
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    expect(f.create).not.toHaveBeenCalled();
    expect(f.getAsset).not.toHaveBeenCalled();
    expect(screen.getByLabelText("转写模型")).toHaveValue("asr-quality");
    expect(screen.getByText("asset-demo")).toBeInTheDocument();
    expect(screen.getByLabelText("转写模型")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
    await screen.findByText("job-demo");
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.create.mock.calls[0]?.[1]).toBe(uuid);
  });
  it("restores the create crash window with an alert that names only the original-key replay", () => {
    const f = fixture();
    f.data.set(key, JSON.stringify(stored(null, "create")));
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    // #780: no Job, no Operation and no asset handle survived the crash, so `读取原状态`
    // would issue no request at all. The alert may therefore not ask for a read; it names
    // the one reconciliation this state really has, and the affordance agrees with it.
    expect(screen.getByRole("alert")).toHaveTextContent(
      "请求结果尚未确认。没有可核对的原素材、原任务或原执行身份；重放只使用原输入和原键，不会新建任务。",
    );
    expect(screen.getByRole("button", { name: "读取原状态" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "按原键重放" })).toBeEnabled();
    expect(f.getJob).not.toHaveBeenCalled();
    expect(f.getAsset).not.toHaveBeenCalled();
    expect(f.create).not.toHaveBeenCalled();
  });
  it.each([
    { shape: "create crash window", record: stored(null, "create"), read: null, replay: true },
    {
      shape: "create with execution identity",
      record: { ...stored(null, "create"), operationId: "op-demo" },
      read: "operation",
      replay: false,
    },
    { shape: "pending cancel", record: stored("job-demo", "cancel"), read: "job", replay: true },
    {
      shape: "pending cancel with execution identity",
      record: { ...stored("job-demo", "cancel"), operationId: "op-demo" },
      read: "operation",
      replay: false,
    },
  ])("reloaded $shape alerts exactly the actions it enables", ({ record, read, replay }) => {
    const f = fixture();
    f.data.set(key, JSON.stringify(record));
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    const alert = screen.getByRole("alert");
    // The rendered copy's action set, read from its own product strings.
    const phrase: Record<string, string> = {
      operation: "读取原执行",
      job: "读取原任务",
      asset: "核对原素材",
    };
    const copy = alert.textContent ?? "";
    const named = Object.entries(phrase)
      .filter(([, text]) => copy.includes(text))
      .map(([target]) => target);
    expect(named).toEqual(read === null ? [] : [read]);
    // Both directions against the buttons the user can really press: the copy names a read
    // iff `读取原状态` is enabled, and mentions the replay iff `按原键重放` is enabled. The
    // fixture's own `read`/`replay` columns keep that equality from being a tautology.
    const refresh = screen.getByRole("button", { name: "读取原状态" });
    const replayButton = screen.getByRole("button", { name: "按原键重放" });
    expect(named.length > 0).toBe(read !== null);
    expect(named.length > 0).toBe(!refresh.hasAttribute("disabled"));
    expect(copy.includes("重放")).toBe(replay);
    expect(copy.includes("重放")).toBe(!replayButton.hasAttribute("disabled"));
    expect(f.getJob).not.toHaveBeenCalled();
  });
  it("reads a durable Asset hint without upload and waits for explicit continue", async () => {
    const f = fixture();
    const base = stored(null);
    f.data.set(
      key,
      JSON.stringify({
        ...base,
        version: 5,
        assetId: null,
        workspaceId: null,
        pending: "upload",
        uploadKey: uuid,
        inputFingerprint: `sha256-chain-v1:${"a".repeat(64)}`,
        assetMediaType: null,
        assetBytes: null,
        assetSha256: null,
        assetHintId: "asset-demo",
      }),
    );
    render(<FileTaskHost client={f.client} storage={f.storage} />);
    expect(f.getAsset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
    await screen.findByRole("button", { name: "继续创建原任务" });
    expect(f.getAsset.mock.calls[0]?.[0]).toBe("asset-demo");
    expect(f.upload).not.toHaveBeenCalled();
    expect(f.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "继续创建原任务" }));
    await screen.findByText("job-demo");
    expect(f.create.mock.calls[0]?.[1]).toBe(uuid);
  });
  it("stops explicit event streams on hide/pagehide/unmount and survives StrictMode replay", async () => {
    const f = fixture();
    f.data.set(key, JSON.stringify(stored()));
    const signals: AbortSignal[] = [];
    vi.spyOn(f.client, "events").mockImplementation(async function* (_id, _after, signal) {
      signals.push(signal!);
      yield await new Promise<never>((_resolve, reject) =>
        signal!.addEventListener("abort", () => reject(new Error("stopped")), { once: true }),
      );
    });
    const view = render(
      <StrictMode>
        <FileTaskHost client={f.client} storage={f.storage} />
      </StrictMode>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
    await screen.findByText("job-demo");
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signals).toHaveLength(1));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(signals[0]?.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signals).toHaveLength(2));
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    hidden.mockRestore();
    expect(signals[1]?.aborted).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "连接任务事件" }));
    await waitFor(() => expect(signals).toHaveLength(3));
    view.unmount();
    await act(async () => {
      await Promise.resolve();
    });
    expect(signals[2]?.aborted).toBe(true);
  });
  it.each([false, true])(
    "explicitly resets only a terminal completed intent; owner conflict=%s",
    async (changed) => {
      const f = fixture();
      f.data.set(key, JSON.stringify(stored()));
      f.getJob.mockResolvedValue(parse("Job", example("succeeded-job")));
      render(<FileTaskHost client={f.client} storage={f.storage} />);
      fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
      await screen.findByText("job-demo");
      fireEvent.click(screen.getByRole("button", { name: "准备新任务" }));
      expect(f.data.has(key)).toBe(true);
      if (changed) f.data.set(key, "foreign-owner");
      fireEvent.click(screen.getByRole("button", { name: "结束本页任务并新建" }));
      if (changed) {
        await screen.findByText(/本地任务信息已变化或无法清理/);
        expect(f.data.get(key)).toBe("foreign-owner");
        expect(screen.getByText("job-demo")).toBeInTheDocument();
      } else {
        expect(f.data.has(key)).toBe(false);
        expect(screen.getByLabelText("转写模型")).toHaveValue("");
        expect(screen.getByLabelText("转写模型")).toHaveFocus();
        selectInput();
        expect(screen.getByRole("button", { name: "上传并转写" })).toBeEnabled();
        fireEvent.submit(screen.getByLabelText("音频文件").closest("form")!);
        await screen.findByText("job-demo");
        expect(f.create.mock.calls[0]?.[1]).not.toBe(uuid);
      }
      expect(f.fetch).not.toHaveBeenCalled();
    },
  );
  it("does not create a download URL or click a link after unmount", async () => {
    const f = fixture();
    f.data.set(key, JSON.stringify(stored()));
    f.getJob.mockResolvedValue(parse("Job", example("succeeded-job")));
    let finish!: (value: Awaited<ReturnType<SpeechFileClient["download"]>>) => void;
    vi.spyOn(f.client, "download").mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const url = vi.fn();
    const original = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    URL.createObjectURL = url;
    try {
      const view = render(<FileTaskHost client={f.client} storage={f.storage} />);
      fireEvent.click(screen.getByRole("button", { name: "读取原状态" }));
      await screen.findByText("job-demo");
      fireEvent.click(screen.getByRole("button", { name: /下载原始制品/ }));
      await waitFor(() => expect(finish).toBeDefined());
      view.unmount();
      await act(async () => {
        await Promise.resolve();
        finish({
          artifact: parse("Artifact", example("artifact")),
          blob: new NodeBlob(["synthetic"]) as Blob,
        });
      });
      expect(url).not.toHaveBeenCalled();
    } finally {
      if (original) Object.defineProperty(URL, "createObjectURL", original);
      else Reflect.deleteProperty(URL, "createObjectURL");
    }
  });
});
