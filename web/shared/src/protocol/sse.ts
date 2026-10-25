/** Private framing only. JobEvent validation remains owned by the A00 boundary package. */
export async function* readSse(source: ReadableStream<Uint8Array>, signal?: AbortSignal) {
  const reader = source.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let data: string[] = [];
  let id: string | undefined;
  let event: string | undefined;
  let size = 0;
  const abort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal?.addEventListener("abort", abort, { once: true });
  try {
    signal?.throwIfAborted();
    while (true) {
      const { value, done } = await reader.read();
      signal?.throwIfAborted();
      buffer += decoder.decode(value, { stream: !done });
      while (true) {
        const boundary = buffer.search(/[\r\n]/);
        if (boundary < 0 || (!done && buffer[boundary] === "\r" && boundary === buffer.length - 1))
          break;
        const line = buffer.slice(0, boundary);
        const width = buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
        buffer = buffer.slice(boundary + width);
        size += line.length + width;
        if (size > 65536) throw new Error("SSE frame exceeds local memory policy");
        if (line === "") {
          if (data.length) yield { id, event, data: data.join("\n") };
          data = [];
          id = event = undefined;
          size = 0;
        } else if (!line.startsWith(":")) {
          const colon = line.indexOf(":");
          const field = colon < 0 ? line : line.slice(0, colon);
          const content = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
          if (field === "data") data.push(content);
          if (field === "id" && !content.includes("\0")) id = content;
          if (field === "event") event = content;
        }
      }
      if (size + buffer.length > 65536) throw new Error("SSE frame exceeds local memory policy");
      if (done) return; // An event without its terminating blank line is not delivered.
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
