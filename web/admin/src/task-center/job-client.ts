import { createSpeechClient, parse, type FileContracts } from "../../../../packages/contracts/src";
import { readSse } from "@speechnexus/shared";

export type Job = FileContracts["Job"];
export type JobList = FileContracts["JobList"];
export type JobCancellation = FileContracts["JobCancellation"];
export type JobEvent = FileContracts["JobEvent"];

export class TaskResponseError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(`Task request rejected (${status})`);
  }
}

export interface TaskApi {
  list(cursor?: string, signal?: AbortSignal): Promise<JobList>;
  getJob(id: string, signal?: AbortSignal): Promise<Job>;
  cancel(id: string, key: string, signal?: AbortSignal): Promise<JobCancellation>;
  events?(id: string, after?: string, signal?: AbortSignal): AsyncIterable<JobEvent>;
}

// Local UI request policy; a deadline on POST leaves its outcome unknown.
const requestSignal = (signal?: AbortSignal) =>
  AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(15_000)]);

/** Public protocol consumer; the deployment URL is supplied by its composition root. */
export class AdminJobClient implements TaskApi {
  readonly #client: ReturnType<typeof createSpeechClient>;
  constructor(baseUrl: string, fetch: typeof globalThis.fetch = globalThis.fetch) {
    this.#client = createSpeechClient(baseUrl, (input, init) =>
      fetch(input, { ...init, redirect: "error" }),
    );
  }

  #check(response: Response, error: unknown) {
    if (response.ok) return;
    const detail = parse("ErrorResponse", error);
    // Do not surface raw response messages, content or deployment URLs to the view.
    throw new TaskResponseError(response.status, detail.error.code);
  }

  async list(cursor?: string, signal?: AbortSignal): Promise<JobList> {
    const result = await this.#client.GET("/v1/nexus/jobs", {
      params: { query: { limit: 50, ...(cursor ? { cursor } : {}) } },
      signal: requestSignal(signal),
    });
    this.#check(result.response, result.error);
    const page = parse("JobList", result.data);
    if (
      new Set(page.data.map((job) => job.id)).size !== page.data.length ||
      (page.has_more && (!page.next_cursor || page.next_cursor === cursor))
    )
      throw new Error("Unusable task list snapshot");
    return page;
  }

  async getJob(id: string, signal?: AbortSignal): Promise<Job> {
    const result = await this.#client.GET("/v1/nexus/jobs/{job_id}", {
      params: { path: { job_id: id } },
      signal: requestSignal(signal),
    });
    this.#check(result.response, result.error);
    const job = parse("Job", result.data);
    if (job.id !== id) throw new Error("Task resource mismatch");
    return job;
  }

  async cancel(id: string, key: string, signal?: AbortSignal): Promise<JobCancellation> {
    const result = await this.#client.POST("/v1/nexus/jobs/{job_id}/cancellations", {
      params: { path: { job_id: id } },
      headers: { "Idempotency-Key": key },
      signal: requestSignal(signal),
    });
    this.#check(result.response, result.error);
    const cancellation = parse("JobCancellation", result.data);
    if (cancellation.job_id !== id) throw new Error("Task resource mismatch");
    return cancellation;
  }

  async *events(id: string, after?: string, signal?: AbortSignal): AsyncIterable<JobEvent> {
    const owner = new AbortController();
    const abort = () => owner.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, 15_000);
    try {
      signal?.throwIfAborted();
      const result = await this.#client.GET("/v1/nexus/jobs/{job_id}/events", {
        params: { path: { job_id: id } },
        headers: { Accept: "text/event-stream", ...(after ? { "Last-Event-ID": after } : {}) },
        parseAs: "stream",
        cache: "no-store",
        signal: owner.signal,
      });
      clearTimeout(timer);
      this.#check(result.response, result.error);
      if (
        result.response.status !== 200 ||
        result.response.headers.get("content-type")?.split(";")[0]?.trim().toLowerCase() !==
          "text/event-stream" ||
        !result.data
      )
        throw new Error("Invalid task event response");
      for await (const frame of readSse(result.data, owner.signal)) {
        const event = parse("JobEvent", JSON.parse(frame.data) as unknown);
        if (
          event.job_id !== id ||
          frame.id !== event.id ||
          (frame.event !== undefined && frame.event !== event.type)
        )
          throw new Error("Task event identity mismatch");
        yield event;
      }
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      owner.abort();
    }
  }
}
