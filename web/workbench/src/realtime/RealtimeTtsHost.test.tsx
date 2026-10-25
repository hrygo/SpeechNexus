import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import RealtimeTtsHost from "./RealtimeTtsHost";
import { TtsController } from "./tts-session";
import { TtsJournal, ttsJournalKey } from "./tts-journal";
import { storage } from "./realtime-fixtures";
import { ttsRequest, ttsSession } from "./tts-fixtures";

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
