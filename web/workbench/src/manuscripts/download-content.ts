/** Native binary consumers own their bounds, cancellation and end-to-end integrity. */
const MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024;
let active = false;

export async function verifiedDownload(
  response: Response,
  stream: ReadableStream<Uint8Array>,
  expected: { bytes: number; sha256: string; media_type: string },
  signal?: AbortSignal,
): Promise<Blob> {
  const reader = stream.getReader();
  let admitted = false;
  const abort = () => {
    void reader.cancel().catch(() => {});
  };
  try {
    if (active) throw new Error("Another download is being verified");
    active = admitted = true;
    if (
      !Number.isSafeInteger(expected.bytes) ||
      expected.bytes < 1 ||
      expected.bytes > MAX_DOWNLOAD_BYTES
    )
      throw new Error("Content exceeds the browser download limit");
    if (
      response.status !== 200 ||
      response.headers.get("Content-Length") !== String(expected.bytes) ||
      response.headers.get("ETag") !== `"${expected.sha256}"`
    )
      throw new Error("Download metadata mismatch");
    signal?.throwIfAborted();
    signal?.addEventListener("abort", abort, { once: true });
    const chunks: ArrayBuffer[] = [];
    let size = 0;
    while (true) {
      signal?.throwIfAborted();
      const part = await reader.read();
      signal?.throwIfAborted();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > expected.bytes) throw new Error("Download length mismatch");
      chunks.push(part.value.slice().buffer);
    }
    if (size !== expected.bytes) throw new Error("Download length mismatch");
    if (!crypto.subtle) throw new Error("A secure browser context is required to verify downloads");
    const blob = new Blob(chunks, { type: expected.media_type });
    const hash = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
    signal?.throwIfAborted();
    const digest =
      "sha256:" +
      Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (digest !== expected.sha256) throw new Error("Download digest mismatch");
    return blob;
  } finally {
    signal?.removeEventListener("abort", abort);
    try {
      await reader.cancel();
    } catch {
      /* Preserve the original failure. */
    }
    reader.releaseLock();
    if (admitted) active = false;
  }
}
