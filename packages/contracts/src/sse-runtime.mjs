import { ContractValidationError } from "./errors.mjs";

// Explicit per-frame byte policy. No buffering a full stream, reconnect or
// terminal-export inference from EOF. The caller owns its last yielded cursor.
export async function* readSse(stream, validate, path, cursor, maxEventBytes) {
  if (!Number.isSafeInteger(maxEventBytes) || maxEventBytes < 1) {
    throw new Error("A positive safe SSE byte limit is required");
  }
  const reader = stream.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
  let line = [];
  let data = [];
  let fields = Object.create(null);
  let size = 0;
  let first = true;
  let afterCR = false;
  let previous = cursor === undefined ? undefined : BigInt(cursor);
  function acceptLine() {
    let text = decoder.decode(Uint8Array.from(line));
    line = [];
    if (first) {
      text = text.replace(/^\uFEFF/, "");
      first = false;
    }
    if (text) {
      if (text.startsWith(":")) return;
      const colon = text.indexOf(":");
      const key = colon < 0 ? text : text.slice(0, colon);
      let value = colon < 0 ? "" : text.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (key === "data") data.push(value);
      else if (key === "id" || key === "event") fields[key] = value;
      return;
    }
    size = 0;
    if (!data.length) {
      fields = Object.create(null);
      return;
    }
    const value = validate(JSON.parse(data.join("\n")));
    for (const [key, expected] of Object.entries(path)) {
      if (value[key] !== expected) throw new Error();
    }
    if (
      (Object.hasOwn(fields, "id") && fields.id !== value.id) ||
      (Object.hasOwn(fields, "event") && fields.event !== value.type)
    )
      throw new Error();
    const number = BigInt(value.id);
    if (previous !== undefined && number <= previous) throw new Error();
    previous = number;
    data = [];
    fields = Object.create(null);
    return value;
  }
  try {
    for (;;) {
      const { value: chunk, done } = await reader.read();
      if (done) break;
      if (!(chunk instanceof Uint8Array)) throw new Error();
      for (const byte of chunk) {
        if (afterCR) {
          afterCR = false;
          if (byte === 10) continue;
        }
        // A CR reserves its optional following LF before dispatch.
        size += byte === 13 ? 2 : 1;
        if (size > maxEventBytes) throw new Error();
        if (byte === 10 || byte === 13) {
          afterCR = byte === 13;
          const value = acceptLine();
          if (value !== undefined) yield value;
        } else line.push(byte);
      }
    }
    if (line.length || data.length) throw new Error();
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new DOMException("Event stream cancelled", "AbortError");
    }
    throw new ContractValidationError("Invalid SSE stream");
  } finally {
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
