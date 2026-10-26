import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import RealtimeHost from "./RealtimeHost";
import { RealtimeController, type RealtimeSocket } from "./realtime-session";
import { RealtimeJournal, realtimeJournalKey } from "./realtime-journal";
import { created, event, ready, request, session, storage } from "./realtime-fixtures";
import type { RealtimeClient } from "./realtime-client";
import type { MicrophoneCapture } from "../media/microphone-capture";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function expectUsableFocus(stage: string) {
  const focused = document.activeElement;
  expect
    .soft(
      {
        element: focused?.tagName,
        inWorkbench: screen.getByRole("region", { name: "实时字幕工作台" }).contains(focused),
        enabled: !focused?.matches(":disabled, [aria-disabled='true']"),
        focusable: focused?.matches("button, input, textarea, select, a[href], [tabindex]"),
      },
      stage,
    )
    .toMatchObject({ inWorkbench: true, enabled: true, focusable: true });
  if (focused instanceof HTMLElement && focused !== document.body)
    expect.soft(focused, stage).toBeVisible();
}
function fillReferences() {
  for (const [label, value] of [
    ["会话预设编号", "profile-demo"],
    ["预设版本", "1"],
    ["实时转写模型编号", "asr-quality"],
    ["模型版本", "1"],
  ])
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
}

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
  it("keeps usable keyboard focus through pending creation, unknown result and original-key replay", async () => {
    const f = fixture(),
      first = deferred<ReturnType<typeof session>>(),
      replay = deferred<ReturnType<typeof session>>();
    f.client.create
      .mockReset()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(replay.promise);
    render(<RealtimeHost controller={f.controller} />);
    fillReferences();
    const submit = screen.getByRole("button", { name: "创建实时会话" });
    submit.focus();
    expect(submit).toHaveFocus();
    fireEvent.click(submit);
    expect(f.client.create).toHaveBeenCalledTimes(1);
    expect(submit).toBeDisabled();
    expectUsableFocus("creation pending must not strand focus on the disabled submit button");
    await act(async () => {
      first.reject(Error("synthetic lost response"));
      await first.promise.catch(() => {});
    });
    expect(screen.getByRole("alert")).toHaveTextContent("提交结果尚未确认");
    const retry = screen.getByRole("button", { name: "按原键重放" });
    expect(retry).toBeEnabled();
    expectUsableFocus("unknown result must leave a keyboard recovery destination");
    retry.focus();
    fireEvent.click(retry);
    expect(retry).toBeDisabled();
    expectUsableFocus("replay pending must leave the now-disabled retry button");
    await act(async () => {
      replay.resolve(session());
      await replay.promise;
    });
    expect(screen.getByText("session-demo")).toBeInTheDocument();
    expect(f.client.create.mock.calls[1]).toEqual(f.client.create.mock.calls[0]);
    expect(screen.queryByRole("button", { name: "按原键重放" })).not.toBeInTheDocument();
    expectUsableFocus("successful replay must not leave focus on BODY after removing retry");
    f.controller.dispose();
  });
  it("keeps usable focus when connecting and starting microphone capture disable their triggers", async () => {
    const f = fixture(),
      reading = deferred<ReturnType<typeof session>>(),
      opening = deferred<MicrophoneCapture>(),
      closed = deferred<void>();
    await f.controller.create(request());
    f.client.current.mockReturnValue(reading.promise);
    f.capture.mockReturnValue(opening.promise);
    render(<RealtimeHost controller={f.controller} />);
    const connect = screen.getByRole("button", { name: "连接此会话" });
    connect.focus();
    expect(connect).toHaveFocus();
    fireEvent.click(connect);
    expect(connect).toBeDisabled();
    expectUsableFocus("connection pending must leave the disabled connect button");
    await act(async () => {
      reading.resolve(session());
      await reading.promise;
    });
    act(() => {
      f.socket.message(created());
      f.socket.message(ready());
    });
    const start = screen.getByRole("button", { name: "开始麦克风采集" });
    expect(start).toBeEnabled();
    start.focus();
    expect(start).toHaveFocus();
    fireEvent.click(start);
    expect(f.capture).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("asr-microphone-state")).toHaveTextContent("正在请求设备");
    expectUsableFocus("microphone permission pending must leave the disabled start button");
    await act(async () => {
      opening.resolve({
        token: "synthetic-capture",
        sampleRate: 24000,
        snapshot: { state: "running", reason: undefined },
        stopping: new Promise<void>(() => {}),
        closed: closed.promise,
        stop: () => {
          closed.resolve();
          return closed.promise;
        },
      });
      await opening.promise;
    });
    expect(screen.getByTestId("asr-microphone-state")).toHaveTextContent("采集中");
    expectUsableFocus("running capture must leave an enabled keyboard destination");
    f.controller.dispose();
  });
  it.each(["host", "outside", "body", "hidden"] as const)(
    "repairs a disconnected control only while focus belongs to this visible host: %s",
    async (destination) => {
      const f = fixture();
      await f.controller.create(request());
      render(
        <>
          <button>其他工作区入口</button>
          <RealtimeHost controller={f.controller} />
        </>,
      );
      await act(() => f.controller.connect());
      act(() => {
        f.socket.message(created());
        f.socket.message(ready());
      });
      const drain = screen.getByRole("button", { name: "停止采集并排空" });
      expect(drain).toBeEnabled();
      drain.focus();
      if (destination === "outside") screen.getByRole("button", { name: "其他工作区入口" }).focus();
      if (destination === "body") {
        fireEvent.pointerDown(document.body);
        drain.blur();
      }
      const hidden =
        destination === "hidden"
          ? vi.spyOn(document, "hidden", "get").mockReturnValue(true)
          : undefined;
      try {
        act(() => {
          f.socket.dispatchEvent(new CloseEvent("close"));
        });
        expect(drain).toBeDisabled();
        if (destination === "host")
          expectUsableFocus("disconnect must repair a disabled focused control");
        else if (destination === "outside")
          expect(screen.getByRole("button", { name: "其他工作区入口" })).toHaveFocus();
        else if (destination === "body") expect(document.body).toHaveFocus();
        else expect(screen.getByRole("heading", { name: "实时字幕" })).not.toHaveFocus();
      } finally {
        hidden?.mockRestore();
        f.controller.dispose();
      }
    },
  );
  it.each([true, false])(
    "does not steal user-moved focus when creation settles with active=%s",
    async (active) => {
      const f = fixture(),
        creating = deferred<ReturnType<typeof session>>();
      f.client.create.mockReturnValue(creating.promise);
      const view = render(
        <>
          <button>其他工作区入口</button>
          <RealtimeHost controller={f.controller} />
        </>,
      );
      fillReferences();
      const submit = screen.getByRole("button", { name: "创建实时会话" });
      submit.focus();
      fireEvent.click(submit);
      const destination = active
        ? screen.getByLabelText("本标签页最终正文草稿")
        : screen.getByRole("button", { name: "其他工作区入口" });
      destination.focus();
      if (!active)
        view.rerender(
          <>
            <button>其他工作区入口</button>
            <RealtimeHost controller={f.controller} active={false} />
          </>,
        );
      expect(destination).toHaveFocus();
      await act(async () => {
        creating.resolve(session());
        await creating.promise;
      });
      expect(screen.getByText("session-demo")).toBeInTheDocument();
      expect(destination).toHaveFocus();
      f.controller.dispose();
    },
  );
  it.each(["resolve", "reject"] as const)(
    "shows local cleanup %s independently of released remote session",
    async (outcome) => {
      const f = fixture();
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const closing = new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      });
      f.capture.mockResolvedValue({
        token: "capture",
        sampleRate: 24000,
        stopping: new Promise<void>(() => {}),
        snapshot: { state: "running", reason: undefined },
        closed: closing,
        stop: () => closing,
      });
      await f.controller.create(request());
      await f.controller.connect();
      f.socket.message(created());
      f.socket.message(ready());
      await f.controller.startCapture();
      const complete = {
        ...session(),
        status: "closed" as const,
        cleanup_state: "complete" as const,
        connection_url: null,
        closed_at: new Date().toISOString(),
      };
      f.client.cancel.mockResolvedValue(complete);
      f.client.current.mockResolvedValue(complete);
      render(<RealtimeHost controller={f.controller} />);
      await act(async () => {
        await f.controller.close("abort");
        await f.controller.refresh();
      });
      expect(screen.getByTestId("asr-microphone-state")).toHaveTextContent("正在停止");
      const create = screen.getByRole("button", { name: "创建实时会话" });
      expect(create).toBeDisabled();
      if (outcome === "resolve") {
        await act(async () => {
          resolve();
          await closing;
        });
        await waitFor(() => expect(create).toBeEnabled());
        expect(screen.getByTestId("asr-microphone-state")).toHaveTextContent("已停止");
      } else {
        await act(async () => {
          reject(Error("synthetic close failure"));
          await closing.catch(() => {});
        });
        await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("释放未确认"));
        await act(async () => {
          await f.controller.refresh();
        });
        expect(screen.getByTestId("asr-microphone-state")).not.toHaveTextContent("已停止");
        expect(create).toBeDisabled();
      }
      f.controller.dispose();
    },
  );
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
