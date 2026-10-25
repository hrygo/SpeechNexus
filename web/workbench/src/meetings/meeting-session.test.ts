import { describe, expect, it, vi } from "vitest";
import { MeetingSession } from "./meeting-session";
import { MeetingJournal } from "./meeting-journal";
import { WorkbenchClient, WorkbenchResponseError, parse } from "../manuscripts/workbench-client";
import { sample, meeting, job, source } from "./meeting-fixtures";

function setup(data = new Map<string, string>()) {
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: vi.fn((k: string, v: string) => {
      data.set(k, v);
    }),
  };
  const client = new WorkbenchClient(vi.fn());
  const status = vi.spyOn(client, "status").mockResolvedValue(
    parse("WorkbenchStatusV2", {
      ...parse("WorkbenchStatusV2", sample("getWorkbenchStatusV2")),
      llm_enabled: true,
      minutes_profile: { template_revision: "7", llm_model: "approved-model" },
    }),
  );
  vi.spyOn(client, "projects").mockResolvedValue(parse("ProjectList", sample("listProjects")));
  vi.spyOn(client, "project").mockResolvedValue(parse("Project", sample("createProject")));
  vi.spyOn(client, "transcripts").mockResolvedValue(
    parse("TranscriptList", sample("listTranscripts")),
  );
  const read = vi.spyOn(client, "view").mockResolvedValue(source());
  const current = vi.spyOn(client, "getMeeting").mockResolvedValue(meeting());
  const create = vi.spyOn(client, "createMeeting").mockResolvedValue(meeting());
  const seal = vi.spyOn(client, "sealMeeting").mockResolvedValue({
    ...meeting(),
    status: "sealed",
    sealed_revision: "2",
    ended_at: "2026-10-24T00:00:00Z",
  });
  const j = { ...job(), source_revision: "2", template_revision: "7", llm_model: "approved-model" };
  const minutes = vi.spyOn(client, "createMinutes").mockResolvedValue(j);
  const getMinutes = vi.spyOn(client, "getMinutes").mockResolvedValue(j);
  const session = new MeetingSession(client, new MeetingJournal(storage));
  return {
    session,
    client,
    storage,
    data,
    status,
    read,
    current,
    create,
    seal,
    minutes,
    getMinutes,
  };
}
async function selected() {
  const f = setup();
  await f.session.start();
  await f.session.selectProject("project-demo");
  await f.session.selectTranscript("transcript-demo");
  f.session.setForm("name", meeting().display_name);
  return f;
}
async function sealed() {
  const f = await selected();
  await f.session.create();
  f.session.edit(" 校对 e\u0301😀\n ");
  f.current.mockResolvedValue({
    ...meeting(),
    status: "sealed" as const,
    sealed_revision: "2",
    ended_at: "2026-10-24T00:00:00Z",
  });
  f.read.mockResolvedValue(source("2", " 校对 e\u0301😀\n "));
  await f.session.seal();
  return f;
}
function rejected() {
  return new WorkbenchResponseError(
    409,
    parse("ErrorResponse", {
      error: {
        code: "revision_conflict",
        type: "invalid_request_error",
        message: "synthetic",
        param: null,
        request_id: "request-demo",
        retryable: false,
      },
    }),
  );
}
describe("meeting session recovery and source facts", () => {
  it.each(["meeting404", "minutes404", "identity"])(
    "invalidates successful server minutes after %s without dropping the local draft",
    async (kind) => {
      const f = await sealed();
      const result = {
        ...job(),
        source_revision: "2",
        template_revision: "7",
        llm_model: "approved-model",
        status: "succeeded" as const,
        minutes_text: "cached-synthetic-minutes",
        artifact_id: null,
        error: null,
      };
      f.current.mockResolvedValue({
        ...meeting(),
        status: "minutes_ready",
        sealed_revision: "2",
        ended_at: "2026-10-24T00:00:00Z",
        minutes_id: result.id,
      });
      f.getMinutes.mockResolvedValue(result);
      await f.session.refresh();
      expect(f.session.getSnapshot().minutes?.minutes_text).toBe("cached-synthetic-minutes");
      const missing = new WorkbenchResponseError(
        404,
        parse("ErrorResponse", {
          error: {
            code: "resource_not_found",
            type: "invalid_request_error",
            message: "synthetic",
            param: null,
            request_id: "request-demo",
            retryable: false,
          },
        }),
      );
      if (kind === "meeting404") f.current.mockRejectedValueOnce(missing);
      if (kind === "minutes404") f.getMinutes.mockRejectedValueOnce(missing);
      if (kind === "identity")
        f.current.mockResolvedValueOnce({ ...meeting(), id: "foreign-meeting" });
      await f.session.refresh();
      expect(f.session.getSnapshot().minutes).toBeNull();
      expect(f.session.getSnapshot().record.draft?.text).toBe(" 校对 e\u0301😀\n ");
      expect(f.session.getSnapshot().verified).toBe(false);
    },
  );
  it("hides the prior success when a replacement Minutes intent is unknown and retains its original key", async () => {
    const f = await sealed();
    const result = {
      ...job(),
      source_revision: "2",
      template_revision: "7",
      llm_model: "approved-model",
      status: "succeeded" as const,
      minutes_text: "old-server-result",
      artifact_id: null,
      error: null,
    };
    f.current.mockResolvedValue({
      ...meeting(),
      status: "minutes_ready",
      sealed_revision: "2",
      ended_at: "2026-10-24T00:00:00Z",
      minutes_id: result.id,
    });
    f.getMinutes.mockResolvedValue(result);
    await f.session.refresh();
    f.minutes.mockRejectedValueOnce(Error("lost replacement reply"));
    await f.session.createMinutes();
    expect(f.session.getSnapshot().minutes).toBeNull();
    expect(f.session.getSnapshot().problem).toBe("unknown");
    expect(f.session.getSnapshot().record.pending?.key).toBe(f.minutes.mock.calls[0]![2]);
  });
  it("merges actual pagination once and rejects a repeated continuation cursor", async () => {
    const f = await selected();
    const base = parse("ProjectList", sample("listProjects"));
    const projects = vi.spyOn(f.client, "projects");
    projects.mockResolvedValueOnce({ ...base, has_more: true, next_cursor: "cursor-one" });
    await f.session.discover();
    projects.mockResolvedValueOnce({
      ...base,
      data: [{ ...base.data[0]!, id: "project-second" }],
      has_more: true,
      next_cursor: "cursor-two",
    });
    await f.session.moreProjects();
    expect(f.session.getSnapshot().projects?.data).toHaveLength(2);
    projects.mockResolvedValueOnce({ ...base, has_more: true, next_cursor: "cursor-two" });
    await f.session.moreProjects();
    expect(f.session.getSnapshot().problem).toBe("read_failed");
    expect(f.session.getSnapshot().projects?.data).toHaveLength(2);
  });
  it("opens only the requested meeting and its matching sealed source", async () => {
    const f = await selected();
    f.session.setForm("openId", meeting().id);
    await f.session.open();
    expect(f.session.getSnapshot().record.meeting?.id).toBe(meeting().id);
    expect(f.session.getSnapshot().verified).toBe(true);
    f.session.setForm("openId", "foreign-meeting");
    await f.session.open();
    expect(f.session.getSnapshot().problem).toBe("read_failed");
    expect(f.session.getSnapshot().record.meeting?.id).toBe(meeting().id);
  });
  it("does not reinterpret a GET 404 after an accepted write as a rejected write", async () => {
    const f = await selected();
    f.current.mockRejectedValueOnce(
      new WorkbenchResponseError(
        404,
        parse("ErrorResponse", {
          error: {
            code: "resource_not_found",
            type: "invalid_request_error",
            message: "synthetic",
            param: null,
            request_id: "request-demo",
            retryable: false,
          },
        }),
      ),
    );
    await f.session.create();
    expect(f.session.getSnapshot().problem).toBe("unknown");
    f.session.dismissRejected();
    expect(f.session.getSnapshot().record.pending?.kind).toBe("create");
  });
  it("ignores an aborted read that completes after another selection", async () => {
    const f = await selected();
    let resolve!: (v: ReturnType<typeof source>) => void;
    f.read.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const first = f.session.refresh();
    // A read may be superseded; no mutation is in flight.
    await f.session.discover();
    resolve(source("3", "迟到正文"));
    await first;
    expect(f.session.getSnapshot().record.view?.revision.text).toBe("原文");
    expect(f.session.getSnapshot().conflict).toBeNull();
  });
  it("blocks navigation with an edited draft until explicit discard", async () => {
    const f = await selected();
    f.session.edit("未保存");
    await f.session.selectProject("project-demo");
    expect(f.session.getSnapshot().problem).toBe("unsaved");
    expect(f.session.getSnapshot().record.draft?.text).toBe("未保存");
    f.session.discardDraft();
    await f.session.selectProject("project-demo");
    expect(f.session.getSnapshot().record.view).toBeNull();
  });
  it("does not request a new Minutes job from dirty or stale text", async () => {
    const f = await sealed();
    f.session.edit("尚未封存");
    await f.session.createMinutes();
    expect(f.minutes).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().problem).toBe("conflict");
  });
  it("discovers sources and journals original creation before POST", async () => {
    const f = await selected();
    f.create.mockImplementationOnce(() => {
      const r = new MeetingJournal(f.storage).load();
      expect(r.pending!.kind).toBe("create");
      expect(r.pending!.key).toBeTruthy();
      return Promise.resolve(meeting());
    });
    await f.session.create();
    expect(f.session.getSnapshot().record.meeting?.id).toBe(meeting().id);
    expect(f.session.getSnapshot().record.pending).toBeNull();
    expect(f.session.getSnapshot()).toBe(f.session.getSnapshot());
  });
  it("seals exact text and selects only the exposed pinned profile for new Minutes", async () => {
    const f = await sealed();
    expect(f.seal.mock.calls[0]![1]).toEqual({
      expected_transcript_revision: "1",
      text: " 校对 e\u0301😀\n ",
    });
    f.current.mockResolvedValue({
      ...{
        ...meeting(),
        status: "sealed" as const,
        sealed_revision: "2",
        ended_at: "2026-10-24T00:00:00Z",
      },
      status: "minutes_pending",
      minutes_id: job().id,
    });
    await f.session.createMinutes();
    expect(f.minutes.mock.calls[0]![1]).toEqual({
      template_revision: "7",
      llm_model: "approved-model",
      expected_source_revision: "2",
    });
    expect(f.getMinutes).toHaveBeenCalled();
  });
  it("restores unknown creation using the same body/key after refresh without auto submission", async () => {
    const f = await selected();
    f.create.mockRejectedValueOnce(Error("lost reply"));
    await f.session.create();
    const original = f.create.mock.calls[0]!;
    expect(f.session.getSnapshot().problem).toBe("unknown");
    const g = setup(f.data);
    await g.session.start();
    expect(g.create).not.toHaveBeenCalled();
    await g.session.replay();
    expect(g.create.mock.calls[0]).toEqual(original);
    expect(g.session.getSnapshot().record.pending).toBeNull();
  });
  it("replays accepted Minutes while LLM is now disabled and does not create a replacement", async () => {
    const f = await sealed();
    f.minutes.mockRejectedValueOnce(Error("lost reply"));
    await f.session.createMinutes();
    const original = f.minutes.mock.calls[0]!;
    const g = setup(f.data);
    g.status.mockResolvedValue(parse("WorkbenchStatusV2", sample("getWorkbenchStatusV2")));
    g.current.mockResolvedValue({
      ...meeting(),
      status: "minutes_pending",
      sealed_revision: "2",
      ended_at: "2026-10-24T00:00:00Z",
      minutes_id: job().id,
    });
    g.read.mockResolvedValue(source("2", " 校对 e\u0301😀\n "));
    await g.session.start();
    await g.session.createMinutes();
    expect(g.minutes).not.toHaveBeenCalled();
    await g.session.replay();
    expect(g.minutes.mock.calls[0]).toEqual(original);
  });
  it("requires profile discovery rather than guessing version one", async () => {
    const f = await sealed();
    f.status.mockResolvedValue({
      ...parse("WorkbenchStatusV2", sample("getWorkbenchStatusV2")),
      llm_enabled: true,
    });
    await f.session.discover();
    await f.session.createMinutes();
    expect(f.minutes).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().problem).toBe("profile");
  });
  it("blocks POST when storage fails and retries only persistence", async () => {
    const f = await selected();
    f.storage.setItem.mockImplementationOnce(() => {
      throw Error("quota");
    });
    await f.session.create();
    expect(f.create).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().blocked).toBe(true);
    f.session.retryStorage();
    expect(f.create).not.toHaveBeenCalled();
    await f.session.replay();
    expect(f.create).toHaveBeenCalledTimes(1);
  });
  it("keeps acknowledged state atomically when the final journal write fails", async () => {
    const f = await selected();
    f.create.mockImplementationOnce(() => {
      f.storage.setItem.mockImplementationOnce(() => {
        throw Error("quota");
      });
      return Promise.resolve(meeting());
    });
    await f.session.create();
    const s = f.session.getSnapshot();
    expect(s.blocked).toBe(true);
    expect(s.record.meeting?.id).toBe(meeting().id);
    expect(s.record.pending).toBeNull();
    expect(s.verified).toBe(true);
    f.session.retryStorage();
    expect(f.create).toHaveBeenCalledTimes(1);
  });
  it("preserves exact draft during CAS conflict until explicit comparison", async () => {
    const f = await selected();
    await f.session.create();
    f.session.edit("我的校对");
    f.seal.mockRejectedValueOnce(rejected());
    f.read.mockResolvedValue(source("3", "其他版本"));
    await f.session.seal();
    expect(f.session.getSnapshot().problem).toBe("rejected");
    expect(f.session.getSnapshot().record.pending).not.toBeNull();
    f.session.dismissRejected();
    await f.session.refresh();
    expect(f.session.getSnapshot().record.draft?.text).toBe("我的校对");
    expect(f.session.getSnapshot().record.draft?.baseRevision).toBe("1");
    expect(f.session.getSnapshot().conflict?.revision.revision).toBe("3");
    f.session.useLatest();
    expect(f.session.getSnapshot().record.draft).toEqual({ text: "我的校对", baseRevision: "3" });
  });
  it("does not clear an unknown request or switch sources/discard it", async () => {
    const f = await selected();
    f.create.mockRejectedValueOnce(Error("unknown"));
    await f.session.create();
    f.session.dismissRejected();
    f.session.discardDraft();
    await f.session.selectProject("other-project");
    expect(f.session.getSnapshot().record.pending).not.toBeNull();
    expect(f.create).toHaveBeenCalledTimes(1);
  });
  it("ignores a late mutation after disposal and leaves durable replay for next mount", async () => {
    const f = await selected();
    let resolve!: (m: ReturnType<typeof meeting>) => void;
    f.create.mockImplementationOnce(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const work = f.session.create();
    f.session.dispose();
    resolve(meeting());
    await work;
    expect(new MeetingJournal(f.storage).load().pending!.kind).toBe("create");
  });
  it("rejects a wrong meeting/project/source instead of overwriting the selection", async () => {
    const f = await selected();
    f.current.mockResolvedValue({ ...meeting(), transcript_id: "foreign-source" });
    await f.session.create();
    expect(f.session.getSnapshot().record.pending?.kind).toBe("create");
    expect(f.session.getSnapshot().problem).toBe("unknown");
  });
  it("never clears an existing draft when a refresh read fails", async () => {
    const f = await selected();
    await f.session.create();
    f.session.edit("我的内容");
    f.current.mockRejectedValueOnce(Error("offline"));
    await f.session.refresh();
    expect(f.session.getSnapshot().verified).toBe(false);
    expect(f.session.getSnapshot().record.draft?.text).toBe("我的内容");
  });
});
