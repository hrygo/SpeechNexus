import fixtures from "../../../../../tests/fixtures/contracts/file-workflow.json";
import { parse, type FileContracts } from "../../../../../packages/contracts/src";

export type DemoScenario =
  | "success"
  | "create-unknown"
  | "cancel-unknown"
  | "conflict"
  | "history-expired"
  | "artifact-expired"
  | "persistent-events"
  | "operation-recovery"
  | "failed";

function sample<K extends keyof FileContracts>(schema: K, id: string) {
  return parse(schema, structuredClone(fixtures.examples.find((entry) => entry.id === id)!.value));
}

/** In-memory development transport only. It never calls fetch or stores user media. */
export function createDemoTransport(scenario: DemoScenario) {
  const stats = {
    executions: 0,
    createKeys: [] as string[],
    lastEventIds: [] as (string | null)[],
  };
  const intents = new Map<string, { body: string; job: FileContracts["Job"] }>();
  let job = sample("Job", "queued-job");
  let lostCreate = false;
  let lostCancel = false;
  let expiredHistory = false;
  const error = (status: number, code: string) => {
    const envelope = sample("ErrorResponse", "recover-operation");
    envelope.error.code = code;
    envelope.error.message = "Synthetic development scenario";
    return Response.json(parse("ErrorResponse", envelope), { status });
  };
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    request.signal.throwIfAborted();
    const path = new URL(request.url).pathname;
    if (path === "/v1/nexus/assets" && request.method === "POST") {
      const form = await request.formData();
      if (!(form.get("file") instanceof File) || form.get("purpose") !== "asr_input")
        return error(422, "invalid_request");
      return Response.json(sample("Asset", "ready-audio"), { status: 201 });
    }
    if (path === "/v1/nexus/assets/asset-demo")
      return Response.json(sample("Asset", "ready-audio"));
    if (path === "/v1/nexus/jobs" && request.method === "POST") {
      const body: unknown = await request.json();
      parse("JobCreateRequest", body);
      const key = request.headers.get("Idempotency-Key") ?? "";
      stats.createKeys.push(key);
      if (!key || scenario === "conflict") return error(409, "idempotency_conflict");
      const previous = intents.get(key);
      if (previous && previous.body !== JSON.stringify(body))
        return error(409, "idempotency_conflict");
      if (!previous) {
        stats.executions++;
        intents.set(key, { body: JSON.stringify(body), job: structuredClone(job) });
      }
      if (scenario === "operation-recovery") return error(409, "operation_in_progress");
      if (scenario === "create-unknown" && !lostCreate) {
        lostCreate = true;
        throw new TypeError("Synthetic lost create response");
      }
      return Response.json(intents.get(key)!.job, { status: 202 });
    }
    if (path === "/v1/nexus/jobs/job-demo/cancellations" && request.method === "POST") {
      job = parse("Job", { ...job, status: "cancelling" });
      if (scenario === "cancel-unknown" && !lostCancel) {
        lostCancel = true;
        throw new TypeError("Synthetic lost cancellation response");
      }
      return Response.json(sample("JobCancellation", "cancel-job"), { status: 202 });
    }
    if (path === "/v1/nexus/jobs/job-demo/events") {
      if (scenario === "persistent-events")
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(": connected\n\n"));
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      const after = request.headers.get("Last-Event-ID");
      stats.lastEventIds.push(after);
      if (scenario === "history-expired" && !expiredHistory) {
        expiredHistory = true;
        return error(410, "event_history_expired");
      }
      const event = sample("JobEvent", scenario === "failed" ? "job.failed" : "job.succeeded");
      const current = sample("Job", "succeeded-job");
      job =
        scenario === "failed"
          ? parse("Job", {
              ...job,
              status: "failed",
              error: sample("JobEvent", "job.failed").data.error,
              cleanup_state: "quarantined",
            })
          : current;
      const id = "9007199254740993";
      const message = parse("JobEvent", { ...event, id, job_id: job.id });
      const body =
        after && BigInt(after) >= BigInt(id)
          ? ": keepalive\n\n"
          : `id: ${id}\nevent: ${message.type}\ndata: ${JSON.stringify(message)}\n\n`;
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    if (path === "/v1/nexus/operations/op-demo")
      return Response.json(sample("Operation", "operation"));
    if (path === "/v1/nexus/jobs/job-demo") return Response.json(job);
    if (
      path === "/v1/nexus/artifacts/artifact-demo" ||
      path === "/v1/nexus/artifacts/artifact-demo/content"
    ) {
      if (scenario === "artifact-expired") return error(410, "artifact_expired");
      const content = new TextEncoder().encode('{"text":"合成转写，仅供演练。"}');
      if (path.endsWith("/content")) return new Response(content);
      return Response.json(
        parse("Artifact", { ...sample("Artifact", "artifact"), bytes: content.length }),
      );
    }
    throw new Error("Development transport refuses unhandled routes");
  };
  return { fetch, stats };
}
