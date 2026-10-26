import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import RealtimeTtsHost from "./RealtimeTtsHost";
import { TtsController } from "./tts-session";
import { TtsJournal, ttsJournalKey } from "./tts-journal";
import { storage } from "./realtime-fixtures";
import { ttsEvent, ttsReady, ttsRequest, ttsSession, ttsStarted } from "./tts-fixtures";
import type { TtsPlayback } from "./tts-playback";
import type { RealtimeClient } from "./realtime-client";

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
        inWorkbench: screen.getByRole("region", { name: "实时配音工作台" }).contains(focused),
        enabled: !focused?.matches(":disabled, [aria-disabled='true']"),
        focusable: focused?.matches("button, input, textarea, select, a[href], [tabindex]"),
      },
      stage,
    )
    .toMatchObject({ inWorkbench: true, enabled: true, focusable: true });
  if (focused instanceof HTMLElement && focused !== document.body)
    expect.soft(focused, stage).toBeVisible();
}
function focusFixture() {
  const client = {
    create: vi.fn<RealtimeClient["create"]>().mockResolvedValue(ttsSession()),
    current: vi.fn<RealtimeClient["current"]>().mockResolvedValue(ttsSession()),
    cancel: vi
      .fn<RealtimeClient["cancel"]>()
      .mockResolvedValue({ ...ttsSession(), status: "draining", cleanup_state: "pending" }),
  };
  const socket = Object.assign(new EventTarget(), {
    readyState: 1,
    bufferedAmount: 0,
    send: vi.fn(),
    close: vi.fn(),
  });
  const playback = vi.fn<(...args: unknown[]) => Promise<TtsPlayback>>();
  const controller = new TtsController(client, new TtsJournal(storage()), {
    socket: () => socket,
    playback,
  });
  return { client, socket, playback, controller };
}
function fillReferences() {
  for (const [label, value] of [
    ["TTS profile ID", "profile-tts"],
    ["TTS profile revision", "1"],
    ["TTS model ID", "tts-demo"],
    ["TTS model revision", "1"],
    ["Voice ID（可留空由服务端解析）", "voice-demo"],
    ["Voice revision", "1"],
  ])
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
}

it("keeps usable keyboard focus through pending TTS creation, unknown result and original-key replay", async () => {
  const f = focusFixture(),
    first = deferred<ReturnType<typeof ttsSession>>(),
    replay = deferred<ReturnType<typeof ttsSession>>();
  f.client.create
    .mockReset()
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(replay.promise);
  render(<RealtimeTtsHost controller={f.controller} />);
  fillReferences();
  const submit = screen.getByRole("button", { name: "创建实时配音会话" });
  submit.focus();
  expect(submit).toHaveFocus();
  fireEvent.click(submit);
  expect(f.client.create).toHaveBeenCalledTimes(1);
  expect(submit).toBeDisabled();
  expectUsableFocus("TTS creation pending must not strand focus on the disabled submit button");
  await act(async () => {
    first.reject(Error("synthetic lost response"));
    await first.promise.catch(() => {});
  });
  expect(screen.getByRole("alert")).toHaveTextContent("请求结果尚未确认");
  const retry = screen.getByRole("button", { name: "按原键重放" });
  expect(retry).toBeEnabled();
  expectUsableFocus("unknown TTS result must leave a keyboard recovery destination");
  retry.focus();
  fireEvent.click(retry);
  expect(retry).toBeDisabled();
  expectUsableFocus("TTS replay pending must leave the now-disabled retry button");
  await act(async () => {
    replay.resolve(ttsSession());
    await replay.promise;
  });
  expect(screen.getByText("session-demo")).toBeInTheDocument();
  expect(f.client.create.mock.calls[1]).toEqual(f.client.create.mock.calls[0]);
  expect(retry).toBeDisabled();
  expectUsableFocus("successful TTS replay must leave the disabled retry button");
  f.controller.dispose();
});
it("keeps usable focus when connecting and starting TTS playback disable their triggers", async () => {
  const f = focusFixture(),
    reading = deferred<ReturnType<typeof ttsSession>>(),
    opening = deferred<TtsPlayback>();
  await f.controller.create(ttsRequest());
  f.client.current.mockReturnValue(reading.promise);
  f.playback.mockReturnValue(opening.promise);
  render(<RealtimeTtsHost controller={f.controller} />);
  const connect = screen.getByRole("button", { name: "连接配音会话" });
  connect.focus();
  expect(connect).toHaveFocus();
  fireEvent.click(connect);
  expect(connect).toBeDisabled();
  expectUsableFocus("TTS connection pending must leave the disabled connect button");
  await act(async () => {
    reading.resolve(ttsSession());
    await reading.promise;
  });
  await act(async () => {
    f.socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(ttsReady()) }));
    await f.controller.settled();
  });
  const start = screen.getByRole("button", { name: "开始一轮配音" });
  expect(start).toBeEnabled();
  start.focus();
  expect(start).toHaveFocus();
  fireEvent.click(start);
  expect(f.playback).toHaveBeenCalledTimes(1);
  expect(start).toBeDisabled();
  expectUsableFocus("TTS playback opening must leave the disabled start button");
  let state: ReturnType<TtsPlayback["snapshot"]>["state"] = "running";
  await act(async () => {
    opening.resolve({
      push: vi.fn(),
      snapshot: () => ({ state, received: 0, rendered: 0 }),
      stop: () => {
        state = "stopped";
        return Promise.resolve();
      },
    });
    await opening.promise;
  });
  expect(screen.getByTestId("tts-playback-state")).toHaveTextContent("running");
  expectUsableFocus("running TTS playback must leave an enabled keyboard destination");
  f.controller.dispose();
});
it.each([true, false])(
  "does not steal user-moved focus when TTS creation settles with active=%s",
  async (active) => {
    const f = focusFixture(),
      creating = deferred<ReturnType<typeof ttsSession>>();
    f.client.create.mockReturnValue(creating.promise);
    const view = render(
      <>
        <button>其他工作区入口</button>
        <RealtimeTtsHost controller={f.controller} />
      </>,
    );
    fillReferences();
    const submit = screen.getByRole("button", { name: "创建实时配音会话" });
    submit.focus();
    fireEvent.click(submit);
    const destination = active
      ? screen.getByLabelText("待追加文本")
      : screen.getByRole("button", { name: "其他工作区入口" });
    destination.focus();
    if (!active)
      view.rerender(
        <>
          <button>其他工作区入口</button>
          <RealtimeTtsHost controller={f.controller} active={false} />
        </>,
      );
    expect(destination).toHaveFocus();
    await act(async () => {
      creating.resolve(ttsSession());
      await creating.promise;
    });
    expect(screen.getByText("session-demo")).toBeInTheDocument();
    expect(destination).toHaveFocus();
    f.controller.dispose();
  },
);

it.each(["resolve", "reject"] as const)(
  "allows stopping local audio after remote done and shows cleanup %s honestly",
  async (outcome) => {
    let resolve!: () => void;
    let reject!: (error: Error) => void;
    let state: ReturnType<TtsPlayback["snapshot"]>["state"] = "running";
    const closing = new Promise<void>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    const socket = Object.assign(new EventTarget(), {
      readyState: 1,
      bufferedAmount: 0,
      send: vi.fn(),
      close: vi.fn(),
    });
    const playback: TtsPlayback = {
      push: vi.fn(),
      snapshot: () => ({ state, received: 3, rendered: 0 }),
      stop: vi.fn(() => {
        state = "closing";
        return closing.then(
          () => {
            state = "stopped";
          },
          (error: unknown) => {
            state = "failed";
            throw error;
          },
        );
      }),
    };
    const c = new TtsController(
      {
        create: vi.fn().mockResolvedValue(ttsSession()),
        current: vi.fn().mockResolvedValue(ttsSession()),
        cancel: vi.fn(),
      },
      new TtsJournal(storage()),
      { socket: () => socket, playback: vi.fn().mockResolvedValue(playback) },
    );
    const message = async (value: unknown) => {
      socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
      await c.settled();
    };
    await c.create(ttsRequest());
    await c.connect();
    await message(ttsReady());
    await c.start("utterance-1");
    await message(ttsStarted());
    await message(
      ttsEvent(
        "nexus.operation.done",
        { kind: "tts", status: "cancelled", cleanup_state: "complete" },
        3,
      ),
    );
    render(<RealtimeTtsHost controller={c} />);
    const start = screen.getByRole("button", { name: "开始一轮配音" });
    expect(start).toBeDisabled();
    const stop = screen.getByRole("button", { name: "停止本轮播放" });
    expect(stop).toBeEnabled();
    fireEvent.click(stop);
    expect(screen.getByTestId("tts-playback-state")).toHaveTextContent("正在停止");
    expect(start).toBeDisabled();
    expect(stop).toBeDisabled();
    // Local stop after a terminal event must not create a second remote cancellation.
    expect(socket.send).toHaveBeenCalledTimes(1);
    if (outcome === "resolve") {
      await act(async () => {
        resolve();
        await closing;
      });
      await waitFor(() => expect(start).toBeEnabled());
      expect(screen.getByTestId("tts-playback-state")).toHaveTextContent("stopped");
    } else {
      await act(async () => {
        reject(Error("synthetic cleanup failure"));
        await closing.catch(() => {});
      });
      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("尚未确认停止"));
      expect(start).toBeDisabled();
      expect(screen.getByTestId("tts-playback-state")).not.toHaveTextContent("stopped");
    }
    c.dispose();
  },
);

it("I2 re-enables explicit original-key replay after hidden/visible without automatic replay or playback", async () => {
  const s = storage(),
    j = new TtsJournal(s),
    client = {
      create: vi.fn().mockRejectedValueOnce(Error("lost")).mockResolvedValue(ttsSession()),
      current: vi.fn(),
      cancel: vi.fn(),
    },
    open = vi.fn();
  const c = new TtsController(client, j, { playback: open });
  await c.create(ttsRequest());
  render(<RealtimeTtsHost controller={c} />);
  const hidden = vi.spyOn(document, "hidden", "get");
  hidden.mockReturnValue(true);
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  hidden.mockReturnValue(false);
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(client.create).toHaveBeenCalledTimes(1);
  expect(open).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
  await waitFor(() => expect(client.create).toHaveBeenCalledTimes(2));
  expect(client.create.mock.calls[0]).toEqual(client.create.mock.calls[1]);
  expect(open).not.toHaveBeenCalled();
  hidden.mockRestore();
  c.dispose();
});

it("shows blank deployment references, restores plain text only, never auto-starts and guards controls", () => {
  const s = storage(),
    j = new TtsJournal(s);
  j.save({ ...j.load(), text: "<script>原文😀</script>" });
  render(<RealtimeTtsHost storage={s} />);
  expect(screen.getByLabelText("TTS profile ID")).toHaveValue("");
  expect(screen.getByLabelText("待追加文本")).toHaveValue("<script>原文😀</script>");
  expect(screen.getByRole("button", { name: "开始一轮配音" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "追加这段文本" })).toBeDisabled();
  expect(document.querySelector("script")).toBeNull();
});
it("fails closed on corrupt storage without overwriting it", () => {
  const s = storage();
  s.setItem(ttsJournalKey, "corrupt");
  render(<RealtimeTtsHost storage={s} />);
  expect(screen.getByRole("alert")).toHaveTextContent("恢复信息不可读取");
  expect(s.getItem(ttsJournalKey)).toBe("corrupt");
});
it("offers exact original-key recovery after unknown REST result", async () => {
  const s = storage(),
    j = new TtsJournal(s),
    client = {
      create: vi.fn().mockRejectedValueOnce(Error("lost")).mockResolvedValue(ttsSession()),
      current: vi.fn(),
      cancel: vi.fn(),
    };
  const c = new TtsController(client, j);
  await c.create(ttsRequest());
  render(<RealtimeTtsHost controller={c} />);
  fireEvent.click(screen.getByRole("button", { name: "按原键重放" }));
  await waitFor(() => expect(client.create).toHaveBeenCalledTimes(2));
  expect(client.create.mock.calls[0]).toEqual(client.create.mock.calls[1]);
  c.dispose();
});
