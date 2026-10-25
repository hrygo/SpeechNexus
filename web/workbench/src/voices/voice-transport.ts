/** Metadata only: bound JSON independently of the shared generated DTO parser. */
export function voiceTransport(
  origin: string,
  fetch: typeof globalThis.fetch = (i, s) => globalThis.fetch(i, s),
): typeof globalThis.fetch {
  return async (input, init) => {
    const request = new Request(input, { ...init, redirect: "error" });
    if (new URL(request.url).origin !== origin) throw new Error("voice_origin");
    const response = await fetch(request);
    if (
      !/^application\/json(?:\s*;|$)/i.test(response.headers.get("Content-Type") ?? "") ||
      !response.body
    ) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error("voice_json");
    }
    const reader = response.body.getReader();
    const parts: Uint8Array[] = [];
    let length = 0;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > 1048576) throw new Error("voice_reply_budget");
        parts.push(value);
      }
    } catch (error) {
      await reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      reader.releaseLock();
    }
    const bytes = new Uint8Array(length);
    let at = 0;
    for (const p of parts) {
      bytes.set(p, at);
      at += p.length;
    }
    const headers = new Headers(response.headers);
    headers.delete("Content-Encoding");
    headers.delete("Content-Length");
    return new Response(bytes, { status: response.status, headers });
  };
}
