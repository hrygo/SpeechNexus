import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import VoiceCreateHost from "./VoiceCreateHost";
import { VoiceSession } from "./voice-session";
import { clone, design, job, reference, storage } from "./voice-fixtures";
import { parse, SpeechResponseError } from "../file-workflow/speech-file-client";

afterEach(cleanup);
it("reports the stable failure code without rendering provider error content", async () => {
  const { c, client } = setup();
  await c.create(design);
  client.getJob.mockResolvedValue({
    ...job(),
    status: "failed",
    cleanup_state: "complete",
    error: { code: "voice_creation_failed", message: "PRIVATE_PROVIDER_BODY", retryable: false },
  });
  await c.refresh();
  await waitFor(() => expect(screen.getByText(/voice_creation_failed/)).toBeVisible());
  expect(screen.queryByText(/PRIVATE_PROVIDER_BODY/)).toBe(null);
});
it("restores the fixed clone mode and original reference text after reload", async () => {
  const client = {
    create: vi.fn().mockResolvedValue(job("voice_clone")),
    getJob: vi.fn().mockResolvedValue(job("voice_clone")),
    getAsset: vi.fn().mockResolvedValue(reference()),
    cancel: vi.fn(),
    getOperation: vi.fn(),
  };
  const s = storage();
  const first = new VoiceSession(client, s);
  await first.create(clone);
  first.dispose();
  render(<VoiceCreateHost session={new VoiceSession(client, s)} />);
  expect(screen.getByRole("button", { name: "样本克隆" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByLabelText("参考音频素材编号")).toHaveValue("asset-demo");
  expect(screen.getByLabelText("参考文本")).toHaveValue(" 合成参考\n");
  const input = screen.getByLabelText("参考文本");
  expect(input.id).not.toBe("");
  expect(document.querySelector(`label[for="${input.id}"]`)).toHaveTextContent("参考文本");
  expect(document.querySelector(`label[for="${input.id}"]`)?.textContent).toBe("参考文本");
  expect(screen.getByRole("checkbox", { name: /确认已获准/ })).toBeChecked();
});
function setup() {
  const client = {
    create: vi.fn().mockResolvedValue(job()),
    getJob: vi.fn().mockResolvedValue(job()),
    getAsset: vi.fn().mockResolvedValue(reference()),
    cancel: vi.fn().mockRejectedValue(new Error("lost")),
    getOperation: vi.fn(),
  };
  const c = new VoiceSession(client, storage());
  render(<VoiceCreateHost session={c} />);
  return { c, client };
}
const fill = (name: string, value: string) =>
  fireEvent.change(screen.getByLabelText(name), { target: { value } });
it("keeps rejected form text editable and reports only its stable code", async () => {
  const { client } = setup();
  client.create.mockRejectedValueOnce(
    new SpeechResponseError(
      422,
      parse("ErrorResponse", {
        error: {
          code: "unsupported_parameter",
          type: "invalid_request_error",
          message: "PRIVATE",
          param: "model",
          request_id: "req-test",
          retryable: false,
        },
      }),
    ),
  );
  fill("工作空间编号", "ws-default");
  fill("创作模型编号", "wrong-model");
  fill("声音描述", " 精确😀\n");
  fill("试听文本", "试听");
  fireEvent.click(screen.getByRole("button", { name: "确认并创建任务" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("unsupported_parameter"));
  expect(screen.getByLabelText("声音描述")).toHaveValue(" 精确😀\n");
  expect(screen.getByLabelText("创作模型编号")).not.toBeDisabled();
  expect(screen.queryByText(/PRIVATE/)).toBe(null);
});
it("offers explicit original operation GET instead of POST replay", async () => {
  const { c, client } = setup();
  client.create.mockRejectedValueOnce(
    new SpeechResponseError(
      409,
      parse("ErrorResponse", {
        error: {
          code: "operation_in_progress",
          type: "invalid_request_error",
          message: "PRIVATE",
          param: null,
          request_id: "req-test",
          retryable: false,
          nexus: { operation_id: "op-original" },
        },
      }),
    ),
  );
  await c.create(design);
  await waitFor(() => expect(screen.getByText(/op-original/)).toBeVisible());
  expect(screen.queryByRole("button", { name: "重放原操作" })).toBe(null);
  expect(screen.getByRole("button", { name: "读取当前任务" })).not.toBeDisabled();
  expect(client.getOperation).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "读取当前任务" }));
  await waitFor(() => expect(client.getOperation).toHaveBeenCalledTimes(1));
});
it("separates modes, resets material confirmation when material changes", () => {
  setup();
  expect(screen.getByLabelText("声音描述")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "样本克隆" }));
  expect(screen.queryByLabelText("声音描述")).toBe(null);
  const confirm = screen.getByRole("checkbox", { name: /确认已获准/ });
  fireEvent.click(confirm);
  expect(confirm).toBeChecked();
  fill("参考文本", "changed");
  expect(confirm).not.toBeChecked();
});
it("creates exact design without auto GET, then confirms cancellation after current read", async () => {
  const { client } = setup();
  fill("工作空间编号", "ws-default");
  fill("创作模型编号", "voice-design");
  fill("声音描述", "  <script>原文😀</script>\n");
  fill("试听文本", "换文试听");
  fireEvent.click(screen.getByRole("button", { name: "确认并创建任务" }));
  await waitFor(() => expect(client.create).toHaveBeenCalledTimes(1));
  expect(client.create.mock.calls[0]![0]).toMatchObject({
    kind: "voice_design",
    input: { description: "  <script>原文😀</script>\n" },
  });
  expect(client.getJob).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "准备取消" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "读取当前任务" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "准备取消" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "准备取消" }));
  expect(client.cancel).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认取消此任务" }));
  await waitFor(() => expect(client.cancel).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("button", { name: "重放原操作" })).toBeVisible();
  expect(screen.getByLabelText("声音描述")).toBeDisabled();
});
it("does not claim a published voice from an accepted task", async () => {
  const { c } = setup();
  await c.create({
    kind: "voice_design",
    model: "voice-design",
    workspace_id: "ws-default",
    input: { description: "合成", preview_text: "试听" },
  });
  await waitFor(() => expect(screen.getByText(/创建任务不等于已发布/)).toBeVisible());
  expect(screen.queryByRole("button", { name: "发布声音" })).toBe(null);
});
