import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import RealtimeTtsHost from "./RealtimeTtsHost";
import { TtsController } from "./tts-session";
import { TtsJournal, ttsJournalKey } from "./tts-journal";
import { storage } from "./realtime-fixtures";
import { ttsEvent, ttsReady, ttsRequest, ttsSession, ttsStarted } from "./tts-fixtures";
import type { TtsPlayback } from "./tts-playback";

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
