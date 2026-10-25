import { describe, expect, it } from "vitest";
import { readSse } from "@speechnexus/shared";

function stream(parts: string[]) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(new TextEncoder().encode(part));
      controller.close();
    },
  });
}

describe("bounded SSE reader", () => {
  it("handles CRLF split across chunks, comments and multiline data", async () => {
    const frames = [];
    for await (const frame of readSse(
      stream([
        ": keepalive\r",
        "\nid: 9007199254740993\r",
        '\ndata: {"a":\r\ndata: 1}\r\n\r',
        "\n",
      ]),
    ))
      frames.push(frame);
    expect(frames).toEqual([{ id: "9007199254740993", event: undefined, data: '{"a":\n1}' }]);
  });

  it("discards incomplete events at EOF and rejects oversized frames", async () => {
    const frames = [];
    for await (const frame of readSse(stream(["data: unfinished"]))) frames.push(frame);
    expect(frames).toEqual([]);
    await expect(
      (async () => {
        for await (const frame of readSse(stream([`data: ${"x".repeat(65537)}`]))) void frame;
      })(),
    ).rejects.toThrow("SSE frame");
  });

  it("settles an aborted pending read and cancels its stream", async () => {
    let cancelled = false;
    const source = new ReadableStream<Uint8Array>({
      cancel() {
        cancelled = true;
      },
    });
    const abort = new AbortController();
    const read = readSse(source, abort.signal).next();
    abort.abort();
    await expect(read).rejects.toThrow();
    expect(cancelled).toBe(true);
  });
});
