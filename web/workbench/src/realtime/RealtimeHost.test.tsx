import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RealtimeHost from "./RealtimeHost";
import { RealtimeController, type RealtimeSocket } from "./realtime-session";
import { RealtimeJournal, realtimeJournalKey } from "./realtime-journal";
import { created, event, ready, session, storage } from "./realtime-fixtures";
import type { RealtimeClient } from "./realtime-client";

class Socket extends EventTarget implements RealtimeSocket {
  readyState = 1;
  bufferedAmount = 0;
  send() {}
  close = vi.fn();
  message(value: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}
function fixture() {
  const s = storage(),
    socket = new Socket(),
    capture = vi.fn();
  const client = {
    create: vi.fn<RealtimeClient["create"]>().mockResolvedValue(session()),
    current: vi.fn<RealtimeClient["current"]>().mockResolvedValue(session()),
    cancel: vi
      .fn<RealtimeClient["cancel"]>()
      .mockResolvedValue({ ...session(), status: "draining", cleanup_state: "pending" }),
  };
  const controller = new RealtimeController(client, new RealtimeJournal(s), {
    socket: () => socket,
    capture,
  });
  return { s, socket, capture, client, controller };
}
async function create() {
  for (const [label, value] of [
    ["会话预设编号", "profile-demo"],
    ["预设版本", "1"],
    ["实时转写模型编号", "asr-quality"],
    ["模型版本", "1"],
  ])
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "创建实时会话" }));
  await screen.findByText("session-demo");
}
describe("formal realtime ASR host", () => {
  it("has empty deployment references, no automatic media/connection, and admits capture only after ready", async () => {
    const f = fixture();
    render(<RealtimeHost controller={f.controller} />);
    expect(screen.getByLabelText("会话预设编号")).toHaveValue("");
    expect(f.client.create).not.toHaveBeenCalled();
    expect(f.capture).not.toHaveBeenCalled();
    await create();
    expect(screen.getByRole("button", { name: "开始麦克风采集" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "连接此会话" }));
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      f.socket.message(created());
    });
    expect(screen.getByRole("button", { name: "开始麦克风采集" })).toBeDisabled();
    act(() => f.socket.message(ready()));
    expect(screen.getByRole("button", { name: "开始麦克风采集" })).toBeEnabled();
    expect(screen.getByText(/当前部署共享/)).toBeInTheDocument();
  });
  it("shows immutable empty final independently of required failure and unverified metadata", async () => {
    const f = fixture();
    render(<RealtimeHost controller={f.controller} />);
    await create();
    await act(async () => {
      await f.controller.connect();
    });
    act(() => {
      f.socket.message(created());
      f.socket.message(ready());
      f.socket.message(
        event(
          "nexus.transcription.hypothesis",
          {
            operation_id: "operation-demo",
            segment_id: "segment-demo",
            item_id: "item-demo",
            revision: 1,
            text: "<b>预览</b>",
            sample_span: { start: 0, end: 2400 },
          },
          2,
        ),
      );
    });
    expect(screen.getByText("<b>预览</b>")).toBeInTheDocument();
    act(() => {
      f.socket.message({
        type: "conversation.item.input_audio_transcription.completed",
        event_id: "final-demo",
        item_id: "item-demo",
        content_index: 0,
        transcript: "",
        usage: { type: "duration", seconds: 0.1 },
      });
      f.socket.message(
        event(
          "nexus.operation.done",
          {
            operation_id: "operation-demo",
            kind: "asr",
            status: "failed",
            cleanup_state: "quarantined",
            reason: "required_auxiliary_failed",
          },
          3,
        ),
      );
    });
    expect(screen.getByText("正文已完成（空文本）")).toBeInTheDocument();
    expect(screen.getByText(/整体执行失败/)).toBeInTheDocument();
    expect(screen.getByLabelText("本标签页最终正文草稿")).toHaveValue("");
    expect(screen.queryByText("<b>预览</b>")).not.toBeInTheDocument();
  });
  it("does not overwrite corrupt storage or fabricate availability", () => {
    const s = storage();
    s.setItem(realtimeJournalKey, "broken");
    render(<RealtimeHost storage={s} />);
    expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可读取");
    expect(s.getItem(realtimeJournalKey)).toBe("broken");
    expect(screen.queryByRole("button", { name: "创建实时会话" })).not.toBeInTheDocument();
  });
});
