import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ContentDownload } from "./ContentDownload";

/** A load this test settles by hand, so cancellation closing stays deterministic. */
function deferred() {
  let settle!: (blob: Blob) => void, signal: AbortSignal | undefined;
  const load = vi.fn((owner: AbortSignal) => {
    signal = owner;
    return new Promise<Blob>((resolve) => {
      settle = resolve;
    });
  });
  return { load, settle: () => settle(new Blob(["synthetic"])), signal: () => signal };
}

/** Renders the control with a neighbour, starts a download and cancels it from the keyboard. */
function cancelDownload(load: (signal: AbortSignal) => Promise<Blob>) {
  render(
    <>
      <ContentDownload
        identity="asset-demo"
        filename="asset-demo.wav"
        label="校验并下载副本"
        load={load}
      />
      <button type="button">其他操作</button>
    </>,
  );
  const action = screen.getByRole("button", { name: "校验并下载副本" }),
    other = screen.getByRole("button", { name: "其他操作" });
  fireEvent.click(action);
  const cancel = screen.getByRole("button", { name: /取消下载/ });
  cancel.focus();
  fireEvent.click(cancel);
  return { action, other };
}

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
  await act(async () => {
    await Promise.resolve();
  });
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByRole("button", { name: /取消下载/ })).toBeNull();
  expect(action).not.toBeDisabled();
  expect(document.activeElement).toBe(action);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

it("keeps the focus a user reached while the cancelled load was still settling", async () => {
  const pending = deferred();
  const { action, other } = cancelDownload(pending.load);
  other.focus();
  await act(async () => {
    pending.settle();
    await Promise.resolve();
  });
  expect(pending.signal()?.aborted).toBe(true);
  expect(action).not.toBeDisabled();
  expect(document.activeElement).toBe(other);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

it("leaves focus alone when an outside click ended the user's place in the control", async () => {
  const pending = deferred();
  const { action } = cancelDownload(pending.load);
  fireEvent.pointerDown(document.body);
  await act(async () => {
    pending.settle();
    await Promise.resolve();
  });
  expect(action).not.toBeDisabled();
  expect(document.activeElement).toBe(document.body);
});

it("leaves focus alone when the window lost focus during cancellation", async () => {
  const pending = deferred();
  const { action } = cancelDownload(pending.load);
  fireEvent(window, new Event("blur"));
  await act(async () => {
    pending.settle();
    await Promise.resolve();
  });
  expect(action).not.toBeDisabled();
  expect(document.activeElement).toBe(document.body);
});

it("leaves focus alone when the page was hidden during cancellation", async () => {
  const pending = deferred();
  const { action } = cancelDownload(pending.load);
  Object.defineProperty(document, "hidden", { configurable: true, value: true });
  try {
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => {
      pending.settle();
      await Promise.resolve();
    });
    expect(action).not.toBeDisabled();
    expect(document.activeElement).toBe(document.body);
  } finally {
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  }
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
