import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Blob as NodeBlob } from "node:buffer";
import { afterEach, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { FixedTranscriptDownload } from "./FixedTranscriptDownload";
import { parse } from "./workbench-client";

afterEach(() => vi.unstubAllGlobals());

function setup(text: string, disabled = false) {
  const sample = fixtures.examples.find(
    (e) => e.operation === "getTranscript" && e.valid && e.kind === "response" && e.status === 200,
  )!;
  const view = parse("TranscriptView", structuredClone(sample.value.body));
  view.revision.text = text;
  const create = vi.fn<(blob: Blob) => string>(() => "blob:synthetic");
  const revoke = vi.fn();
  vi.stubGlobal("Blob", NodeBlob);
  vi.stubGlobal(
    "URL",
    class extends URL {
      static override createObjectURL = create;
      static override revokeObjectURL = revoke;
    },
  );
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  render(<FixedTranscriptDownload view={view} disabled={disabled} />);
  fireEvent.click(screen.getByRole("button", { name: "下载固定版本 TXT" }));
  return { create, revoke, click };
}

it("exports BOM, Unicode and newlines without altering exact UTF-8 bytes", async () => {
  const text = "\ufeff合成 e\u0301 😀\r\n ";
  const f = setup(text);
  expect(f.create).toHaveBeenCalledTimes(1);
  const blob = f.create.mock.calls[0]![0] as unknown as NodeBlob;
  expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual(
    Array.from(new TextEncoder().encode(text)),
  );
  expect(f.click).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(f.revoke).toHaveBeenCalledExactlyOnceWith("blob:synthetic"));
});

it("refuses lossy encoding without initiating a download", () => {
  const f = setup("invalid\ud800");
  expect(f.create).not.toHaveBeenCalled();
  expect(f.click).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("下载未开始");
});

it("does not download an unverified recovery snapshot", () => {
  const f = setup("synthetic", true);
  expect(f.create).not.toHaveBeenCalled();
  expect(f.click).not.toHaveBeenCalled();
});
