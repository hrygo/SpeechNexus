import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import { EditorSession } from "./editor-session";
import { EditorJournal, journalKey } from "./editor-journal";
import { WorkbenchClient, WorkbenchResponseError, parse } from "./workbench-client";

function example(operation: string) {
  return structuredClone(
    fixtures.examples.find(
      (e) =>
        e.operation === operation &&
        e.kind === "response" &&
        e.valid &&
        typeof e.status === "number" &&
        [200, 201].includes(e.status),
    )!.value.body,
  );
}
function view(revision = "1", text = "原文") {
  const value = parse("TranscriptView", example("getTranscript"));
  value.transcript.head_revision = revision;
  Object.assign(value.revision, {
    revision,
    text,
    status: "draft",
    source_revision: revision === "1" ? null : "1",
  });
  return value;
}
function failure(status: number, code = "revision_conflict") {
  return new WorkbenchResponseError(
    status,
    parse("ErrorResponse", {
      error: {
        code,
        type: "invalid_request_error",
        message: "private-synthetic",
        param: null,
        request_id: "request-demo",
        retryable: false,
      },
    }),
  );
}
function setup(data = new Map<string, string>()) {
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: vi.fn((k: string, v: string) => {
      data.set(k, v);
    }),
  };
  const client = new WorkbenchClient(vi.fn());
  const status = vi
    .spyOn(client, "status")
    .mockResolvedValue(parse("WorkbenchStatusV2", example("getWorkbenchStatusV2")));
  const projects = vi
    .spyOn(client, "projects")
    .mockResolvedValue(parse("ProjectList", example("listProjects")));
  const project = vi
    .spyOn(client, "project")
    .mockResolvedValue(parse("Project", example("createProject")));
  const transcripts = vi
    .spyOn(client, "transcripts")
    .mockResolvedValue(parse("TranscriptList", example("listTranscripts")));
  const read = vi.spyOn(client, "view").mockResolvedValue(view());
  const revise = vi.spyOn(client, "revise").mockResolvedValue(view("2", "校对").revision);
  const createProject = vi
    .spyOn(client, "createProject")
    .mockResolvedValue(parse("Project", example("createProject")));
  const createTranscript = vi.spyOn(client, "createTranscript").mockResolvedValue(view());
  const session = new EditorSession(client, new EditorJournal(storage));
  return {
    data,
    storage,
    client,
    session,
    status,
    projects,
    project,
    transcripts,
    read,
    revise,
    createProject,
    createTranscript,
  };
}
async function selected() {
  const f = setup();
  await f.session.start();
  await f.session.selectProject("project-demo");
  await f.session.selectTranscript("transcript-demo");
  return f;
}

describe("manuscript editor lifecycle", () => {
  it("imports a speech original and replays the exact durable source after a lost reply", async () => {
    const f = setup();
    await f.session.start();
    await f.session.selectProject("project-demo");
    f.session.form("transcriptName", "语音原稿");
    f.createTranscript.mockRejectedValueOnce(new Error("lost reply"));
    const source = {
      kind: "speech_artifact",
      artifact_id: "artifact-source",
      receipt_id: "receipt-source",
    } as const;
    await f.session.createTranscript(source);
    expect(f.session.getSnapshot().record.pending).toMatchObject({ body: { source } });
    expect(f.session.getSnapshot().problem).toBe("unknown");
    const original = f.createTranscript.mock.calls[0];
    expect(original?.[1]).toEqual({ display_name: "语音原稿", source });
    f.session.dispose();
    const restored = new EditorSession(f.client, new EditorJournal(f.storage));
    await restored.start();
    expect(f.createTranscript).toHaveBeenCalledTimes(1);
    await restored.replay();
    expect(f.createTranscript.mock.calls[1]).toEqual(original);
    expect(restored.getSnapshot().record.pending).toBeNull();
    restored.dispose();
  });

  it("does not discard a manual input when importing a speech source", async () => {
    const f = setup();
    await f.session.start();
    await f.session.selectProject("project-demo");
    f.session.form("transcriptName", "语音原稿");
    f.session.form("newText", "未保存的手工正文");
    await f.session.createTranscript({
      kind: "speech_artifact",
      artifact_id: "a",
      receipt_id: "r",
    });
    expect(f.createTranscript).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().problem).toBe("unsaved");
    expect(f.session.getSnapshot().record.forms.newText).toBe("未保存的手工正文");
    f.session.dispose();
  });
  it("discovers actual facts and selects authoritative text without mutations", async () => {
    const f = await selected();
    const s = f.session.getSnapshot();
    expect(s.record.view?.revision.text).toBe("原文");
    expect(s.verified).toBe(true);
    expect(s.projects?.data).toHaveLength(1);
    expect(s.transcripts?.data).toHaveLength(1);
    expect(f.createProject).not.toHaveBeenCalled();
    expect(f.revise).not.toHaveBeenCalled();
    expect(f.session.getSnapshot()).toBe(s);
  });

  it("persists the original command before sending and saves exact appended text", async () => {
    const f = await selected();
    f.session.edit(" e\u0301😀\n ");
    f.revise.mockImplementationOnce((_id, body, key) => {
      const stored = JSON.parse(f.data.get(journalKey)!) as {
        pending: { key: string; body: unknown };
      };
      expect(stored.pending).toMatchObject({ key, body });
      expect(body).toEqual({ expected_revision: "1", source_revision: "1", text: " e\u0301😀\n " });
      return Promise.resolve(view("2", body.text).revision);
    });
    f.read.mockResolvedValueOnce(view("2", " e\u0301😀\n "));
    await f.session.save();
    expect(f.session.getSnapshot().record.pending).toBeNull();
    expect(f.session.getSnapshot().record.view?.revision.revision).toBe("2");
    expect(f.revise).toHaveBeenCalledTimes(1);
  });

  it("restores unknown mutations without auto-send, then replays the exact key/body", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.revise.mockRejectedValueOnce(new Error("private-synthetic"));
    await f.session.save();
    const pending = structuredClone(f.session.getSnapshot().record.pending);
    expect(pending?.kind).toBe("revise");
    expect(f.session.getSnapshot().problem).toBe("unknown");
    f.session.dispose();
    const restored = setup(f.data);
    await restored.session.start();
    expect(restored.revise).not.toHaveBeenCalled();
    expect(restored.session.getSnapshot().record.pending).toEqual(pending);
    restored.read.mockResolvedValueOnce(view("2", "校对"));
    await restored.session.replay();
    expect(restored.revise).toHaveBeenCalledWith("transcript-demo", pending!.body, pending!.key);
    expect(restored.session.getSnapshot().record.pending).toBeNull();
  });

  it("does not submit another command or navigate away while outcome is unknown", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.revise.mockRejectedValue(new Error("lost"));
    await f.session.save();
    const pending = structuredClone(f.session.getSnapshot().record.pending);
    await f.session.save();
    await f.session.selectProject("other-project");
    f.session.discard();
    expect(f.session.getSnapshot().record.pending).toEqual(pending);
    expect(f.revise).toHaveBeenCalledTimes(1);
  });

  it("retains the draft on CAS conflict; explicit rebase creates a new intent", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.revise.mockRejectedValueOnce(failure(409));
    f.read.mockResolvedValueOnce(view("2", "其他编辑"));
    await f.session.save();
    const oldKey = f.revise.mock.calls[0]![2];
    const s = f.session.getSnapshot();
    expect(s.record.draft?.text).toBe("校对");
    expect(s.conflict?.revision.text).toBe("其他编辑");
    expect(s.record.pending).toBeNull();
    expect(s.problem).toBe("conflict");
    f.session.rebase();
    expect(f.session.getSnapshot().record.draft).toEqual({
      text: "校对",
      baseRevision: "2",
      sourceRevision: "1",
    });
    f.revise.mockResolvedValueOnce(view("3", "校对").revision);
    f.read.mockResolvedValueOnce(view("3", "校对"));
    await f.session.save();
    expect(f.revise.mock.calls[1]![1].expected_revision).toBe("2");
    expect(f.revise.mock.calls[1]![2]).not.toBe(oldKey);
  });

  it("restores unsaved draft and detects an advanced head without overwriting it", async () => {
    const f = await selected();
    f.session.edit("未保存");
    f.session.dispose();
    const restored = setup(f.data);
    restored.read
      .mockResolvedValueOnce({ ...view("2", "原文"), revision: view().revision })
      .mockResolvedValueOnce(view("2", "远端新稿"));
    await restored.session.start();
    expect(restored.session.getSnapshot().record.draft?.text).toBe("未保存");
    expect(restored.session.getSnapshot().conflict?.revision.text).toBe("远端新稿");
    expect(restored.revise).not.toHaveBeenCalled();
  });

  it("reads explicit history and appends a new revision without rewriting its source", async () => {
    const f = await selected();
    f.read.mockResolvedValueOnce({ ...view("2", "原文"), revision: view().revision });
    await f.session.selectTranscript("transcript-demo", "1");
    f.session.edit("根据历史校对");
    f.revise.mockResolvedValueOnce(view("3", "根据历史校对").revision);
    f.read.mockResolvedValueOnce(view("3", "根据历史校对"));
    await f.session.save();
    expect(f.revise.mock.calls[0]![1]).toEqual({
      expected_revision: "2",
      source_revision: "1",
      text: "根据历史校对",
    });
    expect(f.read).toHaveBeenCalledWith("transcript-demo", "1", expect.any(AbortSignal));
  });

  it("blocks submission after quota failure and retains memory for explicit local retry", async () => {
    const f = await selected();
    f.storage.setItem.mockImplementationOnce(() => {
      throw new Error("quota");
    });
    f.session.edit("草稿尚在");
    expect(f.session.getSnapshot().blocked).toBe(true);
    await f.session.save();
    expect(f.revise).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().record.draft?.text).toBe("草稿尚在");
    f.session.retryStorage();
    expect(f.session.getSnapshot().blocked).toBe(false);
    expect(new EditorJournal(f.storage).load().draft?.text).toBe("草稿尚在");
  });

  it("retains original intent if persistence fails before the first network send", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.storage.setItem.mockImplementationOnce(() => {
      throw new Error("quota");
    });
    await f.session.save();
    expect(f.revise).not.toHaveBeenCalled();
    const pending = structuredClone(f.session.getSnapshot().record.pending);
    f.session.retryStorage();
    f.read.mockResolvedValueOnce(view("2", "校对"));
    await f.session.replay();
    expect(f.revise).toHaveBeenCalledWith("transcript-demo", pending!.body, pending!.key);
  });

  it("preserves pending if a successful mutation cannot be read back", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.read.mockRejectedValueOnce(new Error("read failed"));
    await f.session.save();
    expect(f.session.getSnapshot().record.pending?.kind).toBe("revise");
    expect(f.session.getSnapshot().problem).toBe("unknown");
  });

  it("will not replace another writer's recovery record or send its new mutation", async () => {
    const f = await selected();
    f.session.edit("校对");
    const other = new EditorJournal(f.storage);
    const next = other.load();
    next.forms.projectName = "其他写入";
    other.save(next);
    const raw = f.data.get(journalKey);
    await f.session.save();
    expect(f.revise).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().blocked).toBe(true);
    expect(f.data.get(journalKey)).toBe(raw);
  });

  it("requires explicit discard before changing an unsaved selection", async () => {
    const f = await selected();
    f.session.edit("未保存");
    await f.session.selectProject("project-demo");
    expect(f.session.getSnapshot().problem).toBe("unsaved");
    expect(f.session.getSnapshot().record.draft?.text).toBe("未保存");
    f.session.discard();
    await f.session.selectProject("project-demo");
    expect(f.session.getSnapshot().record.view).toBeNull();
  });

  it("ignores late reads and late mutation responses after disposal", async () => {
    const f = await selected();
    f.session.edit("校对");
    let finish!: (value: ReturnType<typeof view>["revision"]) => void;
    f.revise.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const task = f.session.save();
    const raw = f.data.get(journalKey);
    f.session.dispose();
    finish(view("2", "校对").revision);
    await task;
    expect(f.data.get(journalKey)).toBe(raw);
    expect(new EditorJournal(f.storage).load().pending?.kind).toBe("revise");
  });

  it.each([500, 503])("keeps outcome unknown on %s without retry", async (status) => {
    const f = await selected();
    f.session.edit("校对");
    f.revise.mockRejectedValueOnce(failure(status, "outcome_unknown"));
    await f.session.save();
    expect(f.session.getSnapshot().record.pending?.kind).toBe("revise");
    expect(f.revise).toHaveBeenCalledTimes(1);
  });

  it("creates projects and manuscripts from durable exact form intent", async () => {
    const f = setup();
    await f.session.start();
    f.session.form("projectName", "Synthetic project");
    f.createProject.mockImplementationOnce((body, key) => {
      expect(new EditorJournal(f.storage).load().pending).toEqual({ kind: "project", body, key });
      return Promise.resolve(parse("Project", example("createProject")));
    });
    await f.session.createProject();
    expect(f.session.getSnapshot().record.project?.id).toBe("project-demo");
    f.session.form("transcriptName", "Synthetic transcript");
    f.session.form("newText", " 原稿😀\n ");
    f.createTranscript.mockImplementationOnce((id, body, key) => {
      expect(new EditorJournal(f.storage).load().pending).toEqual({
        kind: "transcript",
        projectId: id,
        body,
        key,
      });
      return Promise.resolve(view("1", body.source.kind === "manual" ? body.source.text : ""));
    });
    await f.session.createTranscript();
    expect(f.session.getSnapshot().record.view?.revision.text).toBe(" 原稿😀\n ");
    expect(f.session.getSnapshot().record.forms.newText).toBe("");
  });

  it("treats invalid form input as correctable rather than a storage failure", async () => {
    const f = await selected();
    await f.session.createProject();
    await f.session.createTranscript();
    expect(f.createProject).not.toHaveBeenCalled();
    expect(f.createTranscript).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().problem).toBe("invalid");
    expect(f.session.getSnapshot().blocked).toBe(false);
  });

  it("does not replace unsaved creation text with another selected project", async () => {
    const f = await selected();
    f.session.form("newText", "未提交原稿");
    await f.session.selectProject("project-demo");
    expect(f.session.getSnapshot().problem).toBe("unsaved");
    expect(f.session.getSnapshot().record.forms.newText).toBe("未提交原稿");
  });

  it("ignores an earlier selected manuscript response that arrives late", async () => {
    const f = await selected();
    let finish!: (value: ReturnType<typeof view>) => void;
    f.read.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const old = f.session.selectTranscript("transcript-demo");
    const newer = view();
    newer.transcript.id = "transcript-newer";
    newer.revision.transcript_id = "transcript-newer";
    f.read.mockResolvedValueOnce(newer);
    await f.session.selectTranscript("transcript-newer");
    finish(view());
    await old;
    expect(f.session.getSnapshot().record.view?.transcript.id).toBe("transcript-newer");
  });

  it("merges live paging without duplicate identities and rejects a stuck cursor", async () => {
    const f = setup();
    const first = parse("ProjectList", example("listProjects"));
    first.has_more = true;
    first.next_cursor = "cursor-one";
    f.projects.mockResolvedValueOnce(first);
    await f.session.start();
    const second = parse("ProjectList", example("listProjects"));
    second.data[0]!.name = "最新名称";
    f.projects.mockResolvedValueOnce(second);
    await f.session.moreProjects();
    expect(f.session.getSnapshot().projects?.data).toHaveLength(1);
    expect(f.session.getSnapshot().projects?.data[0]?.name).toBe("最新名称");
    f.projects.mockResolvedValueOnce(first);
    await f.session.discover();
    f.projects.mockResolvedValueOnce(first);
    await f.session.moreProjects();
    expect(f.session.getSnapshot().problem).toBe("read_failed");
    expect(f.session.getSnapshot().projects?.next_cursor).toBe("cursor-one");
  });

  it("retains durable pending if acknowledged response cannot be persisted", async () => {
    const f = await selected();
    f.session.edit("校对");
    f.revise.mockImplementationOnce(() => {
      f.storage.setItem.mockImplementationOnce(() => {
        throw new Error("quota");
      });
      return Promise.resolve(view("2", "校对").revision);
    });
    f.read.mockResolvedValueOnce(view("2", "校对"));
    await f.session.save();
    expect(f.session.getSnapshot().blocked).toBe(true);
    expect(new EditorJournal(f.storage).load().pending?.kind).toBe("revise");
    f.session.retryStorage();
    expect(new EditorJournal(f.storage).load().pending).toBeNull();
    expect(f.revise).toHaveBeenCalledTimes(1);
  });

  it("blocks new saves once an authoritative read says the selected manuscript is gone", async () => {
    const f = await selected();
    f.session.edit("仍需保留");
    f.read.mockRejectedValueOnce(failure(404, "resource_not_found"));
    await f.session.checkCurrent();
    expect(f.session.getSnapshot().problem).toBe("missing");
    expect(f.session.getSnapshot().verified).toBe(false);
    await f.session.save();
    expect(f.revise).not.toHaveBeenCalled();
    expect(f.session.getSnapshot().record.draft?.text).toBe("仍需保留");
  });

  it("does not carry another manuscript's conflict through a failed selection save", async () => {
    const f = await selected();
    f.read
      .mockResolvedValueOnce({ ...view("2"), revision: view().revision })
      .mockResolvedValueOnce(view("2", "A REMOTE"));
    await f.session.checkCurrent();
    expect(f.session.getSnapshot().conflict?.transcript.id).toBe("transcript-demo");
    const other = view("1", "B ORIGINAL");
    other.transcript.id = "transcript-other";
    other.revision.transcript_id = "transcript-other";
    f.read.mockResolvedValueOnce(other);
    f.storage.setItem.mockImplementationOnce(() => {
      throw new Error("quota");
    });
    await f.session.selectTranscript("transcript-other");
    expect(f.session.getSnapshot().blocked).toBe(true);
    expect(f.session.getSnapshot().conflict).toBeNull();
    f.session.retryStorage();
    f.session.rebase();
    f.session.edit("B EDITED");
    const saved = structuredClone(other);
    saved.transcript.head_revision = "2";
    Object.assign(saved.revision, {
      revision: "2",
      text: "B EDITED",
      status: "draft",
      source_revision: "1",
    });
    f.revise.mockResolvedValueOnce(saved.revision);
    f.read.mockResolvedValueOnce(saved);
    await f.session.save();
    expect(f.revise).toHaveBeenCalledWith(
      "transcript-other",
      {
        expected_revision: "1",
        source_revision: "1",
        text: "B EDITED",
      },
      expect.any(String),
    );
    expect(f.session.getSnapshot().record.view?.transcript.id).toBe("transcript-other");
  });

  it.each([false, true])(
    "requires explicit rebase when acknowledgement readback sees a newer head (replay=%s)",
    async (replay) => {
      const f = await selected();
      f.session.edit("MY SAVED R2");
      if (replay) {
        f.revise.mockRejectedValueOnce(new Error("lost receipt"));
        await f.session.save();
        expect(f.session.getSnapshot().record.pending?.kind).toBe("revise");
      }
      f.revise.mockResolvedValueOnce(view("2", "MY SAVED R2").revision);
      f.read
        .mockResolvedValueOnce({ ...view("3"), revision: view("2", "MY SAVED R2").revision })
        .mockResolvedValueOnce(view("3", "OTHER R3"));
      if (replay) await f.session.replay();
      else await f.session.save();
      const state = f.session.getSnapshot();
      expect(state.record.pending).toBeNull();
      expect(state.record.draft).toEqual({
        text: "MY SAVED R2",
        baseRevision: "2",
        sourceRevision: "2",
      });
      expect(state.conflict?.revision.text).toBe("OTHER R3");
      expect(state.verified).toBe(false);
      f.session.edit("MY NEXT DRAFT");
      const writes = f.revise.mock.calls.length;
      await f.session.save();
      expect(f.revise).toHaveBeenCalledTimes(writes);
      const restored = setup(f.data);
      restored.read
        .mockResolvedValueOnce({ ...view("3"), revision: view("2", "MY SAVED R2").revision })
        .mockResolvedValueOnce(view("3", "OTHER R3"));
      await restored.session.start();
      expect(restored.session.getSnapshot().conflict?.revision.text).toBe("OTHER R3");
      expect(restored.session.getSnapshot().record.draft?.text).toBe("MY NEXT DRAFT");
      f.session.rebase();
      expect(f.session.getSnapshot().record.draft?.baseRevision).toBe("3");
      expect(f.session.getSnapshot().record.draft?.text).toBe("MY NEXT DRAFT");
      expect(f.session.getSnapshot().verified).toBe(true);
    },
  );
});
