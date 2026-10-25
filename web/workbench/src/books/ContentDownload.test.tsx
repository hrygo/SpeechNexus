import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ContentDownload } from "./ContentDownload";

it("aborts a cancelled download and returns keyboard focus to the action", async () => {
  let signal: AbortSignal | undefined;
  const load = vi.fn((owner: AbortSignal) => {
    signal = owner;
    return new Promise<Blob>((_resolve, reject) => {
      owner.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")), {
        once: true,
      });
    });
  });
  render(
    <ContentDownload
      identity="asset-demo"
      filename="asset-demo.wav"
      label="校验并下载副本"
      load={load}
    />,
  );
  const action = screen.getByRole("button", { name: "校验并下载副本" });
  fireEvent.click(action);
  const cancel = screen.getByRole("button", { name: /取消下载/ });
  cancel.focus();
  fireEvent.click(cancel);
  await waitFor(() => expect(action).not.toBeDisabled());
  expect(signal?.aborted).toBe(true);
  expect(document.activeElement).toBe(action);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

it("aborts the old identity on unmount and never reports a late save", async () => {
  let owner: AbortSignal | undefined;
  let resolve!: (blob: Blob) => void;
  const pending = new Promise<Blob>((done) => {
    resolve = done;
  });
  const view = render(
    <ContentDownload
      identity="asset-demo"
      filename="asset-demo.wav"
      label="校验并下载副本"
      load={(signal) => {
        owner = signal;
        return pending;
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "校验并下载副本" }));
  view.unmount();
  expect(owner?.aborted).toBe(true);
  await act(async () => {
    resolve(new Blob(["synthetic"]));
    await pending;
  });
  expect(screen.queryByRole("status")).toBeNull();
});
