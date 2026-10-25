import { webcrypto } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { verifiedDownload } from "./download-content";

afterEach(() => vi.unstubAllGlobals());
const bytes = new Uint8Array([0, 255, 1, 2]);
const digest = "sha256:76d56a3a29f9bc241c464fc5576745cebb196cf48a25140b9b1b9000996a800b";

describe("verified download", () => {
  it("preserves native binary bytes after length and digest verification", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const actual =
      "sha256:" + Buffer.from(await webcrypto.subtle.digest("SHA-256", bytes)).toString("hex");
    const response = new Response(bytes, {
      headers: { "Content-Length": "4", ETag: `"${actual}"` },
    });
    const blob = await verifiedDownload(response, response.body!, {
      bytes: 4,
      sha256: actual,
      media_type: "audio/wav",
    });
    expect(blob.size).toBe(4);
    expect(blob.type).toBe("audio/wav");
  });

  it("refuses a same-length corrupt body and cancels its stream", async () => {
    vi.stubGlobal("crypto", webcrypto);
    const response = new Response(bytes, {
      headers: { "Content-Length": "4", ETag: `"${digest}"` },
    });
    await expect(
      verifiedDownload(response, response.body!, {
        bytes: 4,
        sha256: digest,
        media_type: "audio/wav",
      }),
    ).rejects.toThrow();
  });

  it("rejects metadata mismatch before reading and rejects excess streamed bytes", async () => {
    vi.stubGlobal("crypto", webcrypto);
    for (const headers of [
      { "Content-Length": "5", ETag: `"${digest}"` },
      { "Content-Length": "4", ETag: '"wrong"' },
    ]) {
      const cancel = vi.fn();
      const stream = new ReadableStream<Uint8Array>({ cancel });
      await expect(
        verifiedDownload(new Response(null, { headers }), stream, {
          bytes: 4,
          sha256: digest,
          media_type: "audio/wav",
        }),
      ).rejects.toThrow();
      expect(cancel).toHaveBeenCalledOnce();
    }
    const response = new Response(new Uint8Array(5), {
      headers: { "Content-Length": "4", ETag: `"${digest}"` },
    });
    await expect(
      verifiedDownload(response, response.body!, {
        bytes: 4,
        sha256: digest,
        media_type: "audio/wav",
      }),
    ).rejects.toThrow();
  });

  it("cancels a stalled read when the owning view aborts", async () => {
    const controller = new AbortController();
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ cancel });
    const response = new Response(null, {
      headers: { "Content-Length": "4", ETag: `"${digest}"` },
    });
    const reading = verifiedDownload(
      response,
      stream,
      { bytes: 4, sha256: digest, media_type: "audio/wav" },
      controller.signal,
    );
    controller.abort();
    await expect(reading).rejects.toThrow();
    expect(cancel).toHaveBeenCalledOnce();
  });
});
