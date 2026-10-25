import fixtures from "../../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../speech-file-client";

export type TtsDemoScenario = "success" | "create-unknown" | "cancel-unknown" | "artifact-expired";
const sample = (id: string) => structuredClone(fixtures.examples.find((e) => e.id === id)!.value);

/** Short synthetic tone, never a model-generated voice or a real deployment capability. */
function tone() {
  const buffer = new ArrayBuffer(44 + 2400 * 2);
  const view = new DataView(buffer);
  for (const [offset, text] of [
    [0, "RIFF"],
    [8, "WAVEfmt "],
    [36, "data"],
  ] as const)
    [...text].forEach((letter, index) => view.setUint8(offset + index, letter.charCodeAt(0)));
  view.setUint32(4, buffer.byteLength - 8, true);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 24000, true);
  view.setUint32(28, 48000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  view.setUint32(40, buffer.byteLength - 44, true);
  for (let index = 0; index < 2400; index++)
    view.setInt16(
      44 + index * 2,
      Math.round(2000 * Math.sin((index * Math.PI * 880) / 24000)),
      true,
    );
  return new Uint8Array(buffer);
}

/** Explicit development transport. No fetch fallback, storage, or real speech execution. */
export function createTtsDemoTransport(scenario: TtsDemoScenario) {
  const audio = tone();
  const stats = {
    uploads: 0,
    executions: 0,
    createKeys: [] as string[],
    cancelKeys: [] as string[],
    bodies: [] as string[],
    lastEventIds: [] as (string | null)[],
  };
  let job = parse("Job", {
    ...sample("queued-job"),
    kind: "tts",
    effective_configuration: {
      models: { tts: { id: "tts-quality", revision: "1" } },
      policy_revision: "1",
      catalog_revision: "1",
    },
  });
  const originals = new Map<string, string>();
  let lostCreate = false;
  let lostCancel = false;
  const artifact = async () =>
    parse("Artifact", {
      ...sample("artifact"),
      id: "artifact-audio",
      kind: "audio",
      media_type: "audio/wav",
      bytes: audio.length,
      sha256:
        "sha256:" +
        Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", audio)), (byte) =>
          byte.toString(16).padStart(2, "0"),
        ).join(""),
    });
  const error = (code: string, status: number) =>
    Response.json(
      parse("ErrorResponse", {
        ...sample("recover-operation"),
        error: {
          ...parse("ErrorResponse", sample("recover-operation")).error,
          code,
          message: "Synthetic development scenario",
        },
      }),
      { status },
    );
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    request.signal.throwIfAborted();
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") {
      const form = await request.formData();
      const file = form.get("file");
      if (form.get("purpose") !== "text_input" || !(file instanceof File))
        return error("invalid_request", 422);
      stats.uploads++;
      return Response.json(
        parse("Asset", {
          ...sample("ready-audio"),
          id: "asset-text",
          purpose: "text_input",
          media_type: "text/plain",
          bytes: file.size,
          duration_ms: null,
          sample_rate_hz: null,
          channels: null,
        }),
        { status: 201 },
      );
    }
    if (path === "/v1/nexus/jobs" && request.method === "POST") {
      const command = parse("JobCreateRequest", await request.json());
      if (command.kind !== "tts") return error("invalid_request", 422);
      const body = JSON.stringify(command);
      const key = request.headers.get("Idempotency-Key") ?? "";
      stats.createKeys.push(key);
      stats.bodies.push(body);
      if (!key || (originals.has(key) && originals.get(key) !== body))
        return error("idempotency_conflict", 409);
      if (!originals.has(key)) {
        originals.set(key, body);
        stats.executions++;
      }
      if (scenario === "create-unknown" && !lostCreate) {
        lostCreate = true;
        throw new TypeError("Synthetic lost response");
      }
      return Response.json(job, { status: 202 });
    }
    if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method === "POST") {
      stats.cancelKeys.push(request.headers.get("Idempotency-Key") ?? "");
      job = parse("Job", { ...job, status: "cancelling" });
      if (scenario === "cancel-unknown" && !lostCancel) {
        lostCancel = true;
        throw new TypeError("Synthetic lost response");
      }
      return Response.json(parse("JobCancellation", sample("cancel-job")), { status: 202 });
    }
    if (path === "/v1/nexus/jobs/job-demo/events") {
      const metadata = await artifact();
      job = parse("Job", {
        ...job,
        status: "succeeded",
        cleanup_state: "complete",
        artifacts: [
          {
            artifact_id: metadata.id,
            kind: metadata.kind,
            media_type: metadata.media_type,
            bytes: metadata.bytes,
            sha256: metadata.sha256,
            receipt_id: "receipt-demo",
          },
        ],
        result: { kind: "tts", audio_artifact_id: metadata.id, receipt_id: "receipt-demo" },
      });
      const id = "9007199254740993";
      const event = parse("JobEvent", {
        ...sample("job.succeeded"),
        id,
        job_id: job.id,
        data: {
          status: "succeeded",
          artifacts: job.artifacts,
          result: job.result,
          cleanup_state: job.cleanup_state,
        },
      });
      const after = request.headers.get("Last-Event-ID");
      stats.lastEventIds.push(after);
      return new Response(
        after && BigInt(after) >= BigInt(id)
          ? ": keepalive\n\n"
          : `id: ${id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
        { headers: { "content-type": "text/event-stream" } },
      );
    }
    if (path === "/v1/nexus/jobs/job-demo") return Response.json(job);
    if (
      path === "/v1/nexus/artifacts/artifact-audio" ||
      path === "/v1/nexus/artifacts/artifact-audio/content"
    ) {
      if (scenario === "artifact-expired") return error("artifact_expired", 410);
      return path.endsWith("/content")
        ? new Response(audio, { headers: { "content-type": "audio/wav" } })
        : Response.json(await artifact());
    }
    throw new Error("Development transport refuses unhandled routes");
  };
  return { fetch, stats };
}
