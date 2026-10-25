import { meeting, job } from "./meeting-fixtures";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchClient } from "../manuscripts/workbench-client";

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("generated meeting consumer", () => {
  it("uses the same origin and original create intent with no guessed defaults", async () => {
    const m = meeting();
    const transport = vi.fn<typeof fetch>(() => Promise.resolve(response(m, 201)));
    const client = new WorkbenchClient(transport);
    const body = { display_name: m.display_name, transcript_id: m.transcript_id };
    expect(await client.createMeeting(m.project_id, body, "original-key")).toEqual(m);
    const request = transport.mock.calls[0]![0] as Request;
    expect(new URL(request.url).origin).toBe(location.origin);
    expect(request.redirect).toBe("error");
    expect(request.cache).toBe("no-store");
    expect(request.headers.get("Idempotency-Key")).toBe("original-key");
    expect(await request.json()).toEqual(body);
    expect(transport).toHaveBeenCalledTimes(1);
  });
  it("reads explicit meeting and exact source in seal and minutes without choosing a model", async () => {
    const m = meeting();
    const sealed = {
      ...m,
      status: "sealed",
      sealed_revision: "2",
      ended_at: "2026-10-24T00:01:00Z",
    };
    const j = {
      ...job(),
      meeting_id: m.id,
      source_revision: "2",
      template_revision: "7",
      llm_model: "approved-model",
      status: "queued",
      minutes_text: null,
      artifact_id: null,
      error: null,
    };
    const replies = [response(m), response(sealed), response(j, 202), response(j)];
    const transport = vi.fn<typeof fetch>(() => Promise.resolve(replies.shift()!));
    const client = new WorkbenchClient(transport);
    expect(await client.getMeeting(m.id)).toEqual(m);
    expect(
      await client.sealMeeting(m.id, { expected_transcript_revision: "1" }, "seal-key"),
    ).toEqual(sealed);
    expect(
      await client.createMinutes(
        m.id,
        { template_revision: "7", expected_source_revision: "2" },
        "minutes-key",
      ),
    ).toEqual(j);
    expect(await client.getMinutes(m.id, j.id)).toEqual(j);
    const body: unknown = await (transport.mock.calls[2]![0] as Request).json();
    expect(body).not.toHaveProperty("llm_model");
  });
  it.each(["meeting", "source", "profile", "job"])(
    "rejects mismatched %s replies",
    async (kind) => {
      const m = meeting();
      const j = {
        ...job(),
        meeting_id: m.id,
        status: "queued",
        source_revision: "2",
        template_revision: "7",
        llm_model: "approved-model",
        minutes_text: null,
        artifact_id: null,
        error: null,
      };
      const value =
        kind === "meeting"
          ? { ...m, id: "foreign-meeting" }
          : kind === "source"
            ? { ...j, source_revision: "4" }
            : kind === "profile"
              ? { ...j, llm_model: "other-model" }
              : { ...j, id: "foreign-job" };
      const transport = vi.fn<typeof fetch>(() =>
        Promise.resolve(response(value, kind === "source" || kind === "profile" ? 202 : 200)),
      );
      const c = new WorkbenchClient(transport);
      await expect(
        kind === "meeting"
          ? c.getMeeting(m.id)
          : kind === "job"
            ? c.getMinutes(m.id, j.id)
            : c.createMinutes(
                m.id,
                {
                  template_revision: "7",
                  llm_model: "approved-model",
                  expected_source_revision: "2",
                },
                "original-key",
              ),
      ).rejects.toThrow();
      expect(transport).toHaveBeenCalledTimes(1);
    },
  );
  it("rejects invalid source revisions before any network write", async () => {
    const transport = vi.fn<typeof fetch>();
    const c = new WorkbenchClient(transport);
    await expect(
      c.createMinutes(
        "meeting-demo",
        { template_revision: "7", expected_source_revision: "0" },
        "original-key",
      ),
    ).rejects.toThrow();
    expect(transport).not.toHaveBeenCalled();
  });
});
