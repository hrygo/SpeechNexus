import fixtures from "../../../../../tests/fixtures/contracts/file-workflow.json";
import { parse, type FileContracts } from "../../../../../packages/contracts/src";

export type TaskScenario =
  | "normal"
  | "cancel-unknown"
  | "empty"
  | "list-failed"
  | "job-failed"
  | "events"
  | "events-expired"
  | "events-disconnected"
  | "events-wait";
const sample = <K extends keyof FileContracts>(schema: K, id: string) =>
  parse(schema, structuredClone(fixtures.examples.find((entry) => entry.id === id)!.value));

/** Deterministic in-memory public transport; never invokes native fetch. */
export function createTaskDemoTransport(scenario: TaskScenario) {
  const stats = {
    cancelKeys: [] as string[],
    cancelTargets: [] as string[],
    eventAfter: [] as (string | null)[],
  };
  const jobs = new Map<string, FileContracts["Job"]>();
  for (const id of ["job-demo", "job-second"])
    jobs.set(id, parse("Job", { ...sample("Job", "queued-job"), id }));
  if (scenario === "job-failed")
    jobs.set(
      "job-demo",
      parse("Job", {
        ...jobs.get("job-demo"),
        status: "failed",
        cleanup_state: "quarantined",
        error: sample("JobEvent", "job.failed").data.error,
      }),
    );
  let failedRead = false;
  let lostCancel = false;
  const receipts = new Map<string, FileContracts["JobCancellation"]>();
  const fetch: typeof globalThis.fetch = async (input, init) => {
    // Model an asynchronous transport turn so disposal can cancel before access.
    await Promise.resolve();
    const request = new Request(input, init);
    request.signal.throwIfAborted();
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/v1/nexus/jobs") {
      if (scenario === "list-failed" && !failedRead) {
        failedRead = true;
        throw new TypeError("Synthetic read failure");
      }
      const second = url.searchParams.get("cursor") === "synthetic-page-2";
      return Response.json(
        parse("JobList", {
          object: "nexus.list",
          data: scenario === "empty" ? [] : [jobs.get(second ? "job-second" : "job-demo")],
          has_more: scenario !== "empty" && !second,
          next_cursor: scenario !== "empty" && !second ? "synthetic-page-2" : null,
        }),
      );
    }
    const id = url.pathname.split("/")[4];
    if (
      id &&
      jobs.has(id) &&
      request.method === "GET" &&
      url.pathname === `/v1/nexus/jobs/${id}/events`
    ) {
      const after = request.headers.get("Last-Event-ID");
      stats.eventAfter.push(after);
      if (scenario === "events-disconnected") throw new TypeError("Synthetic event interruption");
      if (scenario === "events-expired" && after) {
        const error = sample("ErrorResponse", "recover-operation");
        return Response.json(
          { ...error, error: { ...error.error, code: "event_history_expired" } },
          { status: 410 },
        );
      }
      if (scenario === "events-wait") {
        let stop: (() => void) | undefined;
        return new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(": synthetic keepalive\n\n"));
              stop = () => controller.close();
              request.signal.addEventListener("abort", stop, { once: true });
            },
            cancel() {
              if (stop) request.signal.removeEventListener("abort", stop);
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      jobs.set(id, parse("Job", { ...jobs.get(id), status: after ? "cancelling" : "running" }));
      const value = parse("JobEvent", {
        ...sample("JobEvent", "job.queued"),
        job_id: id,
        id: after ? "9007199254740994" : "9007199254740993",
      });
      return new Response(
        `id: ${value.id}\nevent: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`,
        { headers: { "Content-Type": "text/event-stream" } },
      );
    }
    if (id && jobs.has(id) && request.method === "GET" && url.pathname === `/v1/nexus/jobs/${id}`)
      return Response.json(jobs.get(id));
    if (
      id &&
      jobs.has(id) &&
      request.method === "POST" &&
      url.pathname === `/v1/nexus/jobs/${id}/cancellations`
    ) {
      const key = request.headers.get("Idempotency-Key");
      if (!key) throw new Error("Missing synthetic cancellation key");
      stats.cancelKeys.push(key);
      stats.cancelTargets.push(id);
      const identity = `${id}:${key}`;
      if (!receipts.has(identity)) {
        jobs.set(id, parse("Job", { ...jobs.get(id), status: "cancelling" }));
        receipts.set(
          identity,
          parse("JobCancellation", { ...sample("JobCancellation", "cancel-job"), job_id: id }),
        );
      }
      if (scenario === "cancel-unknown" && !lostCancel) {
        lostCancel = true;
        throw new TypeError("Synthetic lost cancel response");
      }
      return Response.json(receipts.get(identity), { status: 202 });
    }
    throw new Error("Development task transport refuses unhandled routes");
  };
  return { fetch, stats };
}
