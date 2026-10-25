// @vitest-environment node
import { describe, expect, it } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { SpeechFileClient, SpeechResponseError } from "./speech-file-client";

const example = (id: string) =>
  structuredClone(fixtures.examples.find((item) => item.id === id)!.value);
const origin = "http://127.0.0.1:8000";

describe("A00 file client consumer", () => {
  it("uploads actual multipart bytes through the generated client", async () => {
    const client = new SpeechFileClient(origin, async (request) => {
      const incoming = request as Request;
      expect(incoming.url).toBe(`${origin}/v1/nexus/assets`);
      expect(incoming.headers.get("Idempotency-Key")).toBe("upload-one");
      const form = await incoming.formData();
      expect(form.get("purpose")).toBe("asr_input");
      const file = form.get("file") as File;
      expect(Array.from(new Uint8Array(await file.arrayBuffer()))).toEqual([1, 2, 3]);
      return Response.json(example("ready-audio"), { status: 201 });
    });
    await expect(
      client.upload(new File([new Uint8Array([1, 2, 3])], "synthetic.wav"), "upload-one"),
    ).resolves.toHaveProperty("id", "asset-demo");
  });

  it("validates wire responses and cross-resource identity instead of trusting static types", async () => {
    const malformed = new SpeechFileClient(origin, () =>
      Promise.resolve(Response.json({ id: "job-demo" })),
    );
    await expect(malformed.getJob("job-demo")).rejects.toThrow("Invalid Job");
    const wrong = new SpeechFileClient(origin, () =>
      Promise.resolve(Response.json(example("queued-job"))),
    );
    await expect(wrong.getJob("other-job")).rejects.toThrow("resource mismatch");
  });

  it("preserves unknown mutation outcomes without automatically retrying", async () => {
    let requests = 0;
    const client = new SpeechFileClient(origin, () => {
      requests++;
      return Promise.reject(new TypeError("synthetic connection loss"));
    });
    await expect(client.create(example("asr-request"), "original-key")).rejects.toThrow();
    expect(requests).toBe(1);
  });

  it("parses stable errors without displaying untrusted server messages", async () => {
    const client = new SpeechFileClient(origin, () =>
      Promise.resolve(Response.json(example("recover-operation"), { status: 409 })),
    );
    try {
      await client.create(example("asr-request"), "original-key");
      expect.fail("Expected response error");
    } catch (error) {
      expect(error).toBeInstanceOf(SpeechResponseError);
      expect(error).toMatchObject({ status: 409, code: "operation_in_progress" });
      expect((error as Error).message).not.toContain("Query the original");
    }
  });

  it("does not download a non-ready artifact", async () => {
    let calls = 0;
    const client = new SpeechFileClient(origin, () => {
      calls++;
      return Promise.resolve(Response.json({ ...example("artifact"), status: "expired" }));
    });
    await expect(client.download("artifact-demo")).rejects.toThrow();
    expect(calls).toBe(1);
  });

  it("rejects over-budget metadata before downloading and cancels oversized streams", async () => {
    let calls = 0;
    const large = new SpeechFileClient(origin, () => {
      calls++;
      return Promise.resolve(
        Response.json({ ...example("artifact"), bytes: 16 * 1024 * 1024 + 1 }),
      );
    });
    await expect(large.download("artifact-demo")).rejects.toThrow("local download policy");
    expect(calls).toBe(1);
    let cancelled = false;
    const overflow = new SpeechFileClient(origin, (input) => {
      if ((input as Request).url.endsWith("/content"))
        return Promise.resolve(
          new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(new Uint8Array(4));
              },
              cancel() {
                cancelled = true;
              },
            }),
          ),
        );
      return Promise.resolve(Response.json({ ...example("artifact"), bytes: 3 }));
    });
    await expect(overflow.download("artifact-demo")).rejects.toThrow("declared size");
    expect(cancelled).toBe(true);
  });

  it("rejects an SSE envelope that disagrees with its validated payload", async () => {
    const event = { ...example("job.queued"), job_id: "job-demo" };
    const client = new SpeechFileClient(origin, () =>
      Promise.resolve(
        new Response(`id: 2\ndata: ${JSON.stringify(event)}\n\n`, {
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );
    await expect(client.events("job-demo").next()).rejects.toThrow("envelope mismatch");
  });
});
