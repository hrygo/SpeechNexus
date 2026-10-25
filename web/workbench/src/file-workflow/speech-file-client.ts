// Consume Team A's public package entrypoint directly in this monorepo checkout.
// Package-manifest/lockfile integration remains coordinated by A; no DTO is copied here.
import {
  createSpeechClient,
  uploadAsset,
  parse,
  type FileContracts,
} from "../../../../packages/contracts/src";
import { readSse } from "@speechnexus/shared";
import { textUploadFile } from "./input-fingerprint";

export type { FileContracts } from "../../../../packages/contracts/src";
export { parse } from "../../../../packages/contracts/src";

export class SpeechResponseError extends Error {
  readonly code: string;
  constructor(
    readonly status: number,
    readonly detail: FileContracts["ErrorResponse"],
  ) {
    super(`Speech response ${status}: ${detail.error.code}`);
    this.code = detail.error.code;
  }
}
/** An Asset-only pending outcome; Location is a hint, never an arbitrary fetch target. */
export class SpeechUploadRecoveryError extends SpeechResponseError {
  constructor(
    status: number,
    detail: FileContracts["ErrorResponse"],
    readonly assetId: string | null,
  ) {
    super(status, detail);
  }
}

function assertResponse(response: Response, error: unknown) {
  if (!response.ok) throw new SpeechResponseError(response.status, parse("ErrorResponse", error));
}

function assertIdentity(actual: string, expected: string) {
  if (actual !== expected) throw new Error("Speech resource mismatch");
}

export class SpeechFileClient {
  readonly #client: ReturnType<typeof createSpeechClient>;
  readonly #transport: typeof globalThis.fetch;
  constructor(
    private readonly baseUrl: string,
    fetch?: typeof globalThis.fetch,
  ) {
    this.#transport = fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.#client = createSpeechClient(baseUrl, (input, init) =>
      this.#transport(new Request(input, { ...init, redirect: "error" })),
    );
  }

  async upload(file: File, key: string, signal?: AbortSignal) {
    return this.#upload(file, "asr_input", key, signal);
  }

  async uploadText(text: string, key: string, signal?: AbortSignal) {
    return this.#upload(textUploadFile(text), "text_input", key, signal);
  }

  async #upload(
    file: File,
    purpose: "asr_input" | "text_input",
    key: string,
    signal?: AbortSignal,
  ) {
    signal?.throwIfAborted();
    // A owns serialization; its helper has no signal option, so isolate this call's transport.
    const client = createSpeechClient(this.baseUrl, (input, init) =>
      this.#transport(
        new Request(input, { ...init, ...(signal ? { signal } : {}), redirect: "error" }),
      ),
    );
    const result = await uploadAsset(client, { file, purpose }, { idempotencyKey: key });
    if (!result.response.ok) {
      const detail = parse("ErrorResponse", result.error);
      if (
        (result.response.status === 409 && detail.error.code === "asset_upload_in_progress") ||
        (result.response.status === 503 &&
          ["asset_upload_unknown", "asset_metadata_unknown"].includes(detail.error.code))
      ) {
        const match = /^\/v1\/nexus\/assets\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$/.exec(
          result.response.headers.get("Location") ?? "",
        );
        throw new SpeechUploadRecoveryError(result.response.status, detail, match?.[1] ?? null);
      }
    }
    assertResponse(result.response, result.error);
    return parse("Asset", result.data);
  }

  async create(command: unknown, key: string, signal?: AbortSignal) {
    const result = await this.#client.POST("/v1/nexus/jobs", {
      body: parse("JobCreateRequest", command),
      headers: { "Idempotency-Key": key },
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    return parse("Job", result.data);
  }

  async getAsset(assetId: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/assets/{asset_id}", {
      params: { path: { asset_id: assetId } },
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    const asset = parse("Asset", result.data);
    assertIdentity(asset.id, assetId);
    return asset;
  }

  async getJob(jobId: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/jobs/{job_id}", {
      params: { path: { job_id: jobId } },
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    const job = parse("Job", result.data);
    assertIdentity(job.id, jobId);
    return job;
  }

  async cancel(jobId: string, key: string, signal?: AbortSignal) {
    const result = await this.#client.POST("/v1/nexus/jobs/{job_id}/cancellations", {
      params: { path: { job_id: jobId } },
      headers: { "Idempotency-Key": key },
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    const cancellation = parse("JobCancellation", result.data);
    assertIdentity(cancellation.job_id, jobId);
    return cancellation;
  }

  async getOperation(operationId: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/operations/{operation_id}", {
      params: { path: { operation_id: operationId } },
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    const operation = parse("Operation", result.data);
    assertIdentity(operation.id, operationId);
    return operation;
  }

  async *events(jobId: string, after?: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/jobs/{job_id}/events", {
      params: { path: { job_id: jobId } },
      headers: after ? { "Last-Event-ID": after } : {},
      parseAs: "stream",
      signal: signal ?? null,
    });
    assertResponse(result.response, result.error);
    if (
      !result.response.headers.get("content-type")?.startsWith("text/event-stream") ||
      !result.data
    )
      throw new Error("Invalid SSE response");
    for await (const frame of readSse(result.data, signal)) {
      const event = parse("JobEvent", JSON.parse(frame.data) as unknown);
      assertIdentity(event.job_id, jobId);
      if (frame.id !== event.id || (frame.event !== undefined && frame.event !== event.type))
        throw new Error("SSE envelope mismatch");
      yield event;
    }
  }

  async download(artifactId: string, signal?: AbortSignal) {
    const metadata = await this.#client.GET("/v1/nexus/artifacts/{artifact_id}", {
      params: { path: { artifact_id: artifactId } },
      signal: signal ?? null,
    });
    assertResponse(metadata.response, metadata.error);
    const artifact = parse("Artifact", metadata.data);
    assertIdentity(artifact.id, artifactId);
    if (artifact.status !== "ready") throw new Error("Artifact is not ready");
    // Explicit user download, bounded independently of the untrusted Content-Length.
    const limit = 16 * 1024 * 1024;
    if (artifact.bytes > limit) throw new Error("Artifact exceeds local download policy");
    const content = await this.#client.GET("/v1/nexus/artifacts/{artifact_id}/content", {
      params: { path: { artifact_id: artifactId } },
      signal: signal ?? null,
      parseAs: "stream",
    });
    assertResponse(content.response, content.error);
    if (!content.data) throw new Error("Missing artifact content");
    const reader = content.data.getReader();
    const abort = () => {
      void reader.cancel().catch(() => undefined);
    };
    signal?.addEventListener("abort", abort, { once: true });
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let bytes = 0;
    try {
      signal?.throwIfAborted();
      while (true) {
        const { value, done } = await reader.read();
        signal?.throwIfAborted();
        if (done) break;
        bytes += value.length;
        if (bytes > artifact.bytes || bytes > limit)
          throw new Error("Artifact exceeds declared size");
        chunks.push(new Uint8Array(value));
      }
      if (bytes !== artifact.bytes) throw new Error("Artifact content is incomplete");
      return { artifact, blob: new Blob(chunks, { type: artifact.media_type }) };
    } finally {
      signal?.removeEventListener("abort", abort);
      await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }
}
