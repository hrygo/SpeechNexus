/** Private bounded-memory identity for reselecting an upload, never a wire content digest. */
const blockBytes = 1024 * 1024;

export class UploadInputMismatchError extends Error {
  constructor() {
    super("Original upload input does not match");
  }
}

export function textUploadFile(text: string) {
  if (new TextDecoder().decode(new TextEncoder().encode(text)) !== text)
    throw new UploadInputMismatchError();
  return new File([text], "speech-text.txt", { type: "text/plain" });
}

export async function inputFingerprint(file: File, signal?: AbortSignal) {
  signal?.throwIfAborted();
  if (
    !Number.isSafeInteger(file.size) ||
    file.size < 0 ||
    file.name.length > 4096 ||
    file.type.length > 128
  )
    throw new UploadInputMismatchError();
  const header = new TextEncoder().encode(
    JSON.stringify(["SpeechNexus upload identity v1", blockBytes, file.name, file.type, file.size]),
  );
  let digest = new Uint8Array(await crypto.subtle.digest("SHA-256", header));
  for (let offset = 0; offset < file.size; offset += blockBytes) {
    signal?.throwIfAborted();
    const bytes = await file.slice(offset, offset + blockBytes).arrayBuffer();
    signal?.throwIfAborted();
    const part = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    const chain = new Uint8Array(64);
    chain.set(digest);
    chain.set(part, 32);
    digest = new Uint8Array(await crypto.subtle.digest("SHA-256", chain));
  }
  signal?.throwIfAborted();
  return (
    "sha256-chain-v1:" + Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("")
  );
}
