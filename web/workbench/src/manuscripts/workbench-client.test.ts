import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
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
function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const failure = {
  error: {
    code: "revision_conflict",
    type: "invalid_request_error",
    message: "private-synthetic",
    param: null,
    request_id: "request-demo",
    retryable: false,
  },
};

describe("generated same-origin Workbench consumer", () => {
  it("consumes exact create intent without normalization, retries or redirects", async () => {
    const body = example("createTranscript") as {
      transcript: { display_name: string };
      revision: { text: string };
    };
    body.revision.text = " e\u0301😀\n ";
    body.transcript.display_name = "合成";
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(body, 201)));
    const client = new WorkbenchClient(fetch);
    await client.createTranscript(
      "project-demo",
      {
        display_name: "合成",
        source: {
          kind: "manual",
          text: body.revision.text,
        },
      },
      "original-key",
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).origin).toBe(location.origin);
    expect(request.redirect).toBe("error");
    expect(request.cache).toBe("no-store");
    expect(request.headers.get("Idempotency-Key")).toBe("original-key");
    const sent = (await request.json()) as { source: { text: string } };
    expect(sent.source.text).toBe(body.revision.text);
  });

  it("rejects invalid generated requests before fetch", async () => {
    const fetch = vi.fn(() => Promise.resolve(reply(example("createProject"), 201)));
    const client = new WorkbenchClient(fetch);
    await expect(client.createProject({ name: "" }, "original-key")).rejects.toThrow();
    await expect(
      client.createTranscript(
        "invalid/id",
        {
          display_name: "合成",
          source: {
            kind: "manual",
            text: "合成",
          },
        },
        "original-key",
      ),
    ).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("accepts the draft original a manual transcript starts with, and rejects a sealed one", async () => {
    const body = example("createTranscript") as {
      transcript: { display_name: string };
      revision: { status: string; text: string };
    };
    body.transcript.display_name = "合成";
    body.revision.status = "draft";
    body.revision.text = "合成原稿";
    const create = {
      display_name: "合成",
      source: { kind: "manual" as const, text: "合成原稿" },
    };
    const draftFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(body, 201)));
    await new WorkbenchClient(draftFetch).createTranscript("project-demo", create, "original-key");
    expect(draftFetch).toHaveBeenCalledTimes(1);

    // The contract only seals a speech import, so a sealed manual original is a mismatch.
    const sealed = structuredClone(body);
    sealed.revision.status = "sealed";
    const sealedFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(sealed, 201)));
    await expect(
      new WorkbenchClient(sealedFetch).createTranscript("project-demo", create, "original-key"),
    ).rejects.toThrow();
  });

  it("preserves revision strings beyond Number's exact range", async () => {
    const view = example("getTranscript") as {
      transcript: { head_revision: string };
      revision: { revision: string };
    };
    const revision = "9007199254740993";
    view.transcript.head_revision = revision;
    view.revision.revision = revision;
    // A non-first revision is a draft with an explicit source.
    Object.assign(view.revision, { status: "draft", source_revision: "1" });
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(view)));
    await new WorkbenchClient(fetch).view("transcript-demo", revision);
    expect(new URL((fetch.mock.calls[0]![0] as Request).url).searchParams.get("revision")).toBe(
      revision,
    );
  });

  it("rejects cross-resource views, inconsistent heads and wrong parent list items", async () => {
    for (const change of [
      { transcript: { id: "other-transcript" } },
      { revision: { transcript_id: "other-transcript" } },
      { transcript: { head_revision: "2" } },
    ]) {
      const view = example("getTranscript") as Record<string, Record<string, unknown>>;
      for (const [key, patch] of Object.entries(change)) Object.assign(view[key]!, patch);
      await expect(
        new WorkbenchClient(() => Promise.resolve(reply(view))).view("transcript-demo"),
      ).rejects.toThrow();
    }
    const list = example("listTranscripts");
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(list))).transcripts("other-project"),
    ).rejects.toThrow();
  });

  it("rejects wrong revision receipt text/source and never retries", async () => {
    const response = example("createTranscriptRevision");
    const fetch = vi.fn(() => Promise.resolve(reply(response, 201)));
    await expect(
      new WorkbenchClient(fetch).revise(
        "transcript-demo",
        {
          expected_revision: "1",
          source_revision: "1",
          text: "校对",
        },
        "original-key",
      ),
    ).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("exposes only bounded failure identity, without error content", async () => {
    const fetch = vi.fn(() => Promise.resolve(reply(failure, 409)));
    const err = await new WorkbenchClient(fetch)
      .createProject({ name: "合成" }, "original-key")
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(WorkbenchResponseError);
    expect(err).toMatchObject({ status: 409, code: "revision_conflict" });
    expect(String(err)).not.toContain("private-synthetic");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects malformed success, inconsistent paging and duplicate resource identities", async () => {
    for (const data of [
      {},
      { ...example("listProjects"), has_more: true },
      { ...example("listProjects"), data: [example("createProject"), example("createProject")] },
    ]) {
      await expect(
        new WorkbenchClient(() => Promise.resolve(reply(data))).projects(),
      ).rejects.toThrow();
    }
  });
});

// Book and export bodies are built from the generated contract types here: these
// operations are covered by the contract's own request fixtures, and the client's
// parse() is what rejects a wire shape that drifts from the frozen document.
const bookBody = {
  object: "nexus.book",
  id: "book_demo",
  project_id: "project_demo",
  title: "合成有声书",
  status: "draft",
  casting_revision: "3",
  created_at: "2026-10-24T12:00:00Z",
  updated_at: "2026-10-24T12:00:00Z",
};
function exportBody(overrides: Record<string, unknown> = {}) {
  return {
    object: "nexus.export_job",
    id: "export_demo",
    book_id: "book_demo",
    status: "queued",
    format: "zip",
    manifest_revision: "3",
    progress_completed: 0,
    progress_total: 2,
    artifact: null,
    error: null,
    created_at: "2026-10-24T12:00:00Z",
    updated_at: "2026-10-24T12:00:00Z",
    ...overrides,
  };
}

describe("book export consumer", () => {
  it("creates an export pinned to the revision it was given", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(exportBody(), 202)));
    const body = parse("ExportCreateRequest", { expected_casting_revision: "3", format: "zip" });
    const job = await new WorkbenchClient(fetch).createBookExport("book_demo", body, "key-demo");
    expect(job.status).toBe("queued");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(request.method).toBe("POST");
    expect(new URL(request.url).pathname).toBe("/v1/workbench/books/book_demo/exports");
    expect(request.headers.get("Idempotency-Key")).toBe("key-demo");
    expect(await request.clone().json()).toEqual({ expected_casting_revision: "3", format: "zip" });
  });

  it("refuses a queued job that already claims a produced artifact", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(() =>
      Promise.resolve(
        reply(
          exportBody({
            status: "queued",
            artifact: {
              object: "nexus.export_artifact",
              id: "artifact_demo",
              export_id: "export_demo",
              manifest_revision: "3",
              media_type: "application/zip",
              bytes: 1,
              sha256: "sha256:" + "ab".repeat(32),
              created_at: "2026-10-24T12:00:05Z",
            },
          }),
          202,
        ),
      ),
    );
    await expect(
      new WorkbenchClient(fetch).createBookExport(
        "book_demo",
        parse("ExportCreateRequest", { expected_casting_revision: "3", format: "zip" }),
        "key-demo",
      ),
    ).rejects.toThrow();
  });

  it("reads and cancels the same export without accepting a foreign job", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(exportBody(), 200)));
    const client = new WorkbenchClient(fetch);
    expect((await client.bookExport("export_demo")).id).toBe("export_demo");
    fetch.mockResolvedValueOnce(reply(exportBody({ status: "cancelling", format: "mp3" }), 200));
    const cancelled = await client.cancelBookExport("export_demo", "cancel-key");
    expect(cancelled.status).toBe("cancelling");

    fetch.mockResolvedValueOnce(reply(exportBody(), 202));
    // 202 means the handler had nothing to do; the body is still a valid job.
    expect((await client.cancelBookExport("export_demo", "cancel-key-2")).status).toBe("queued");
  });

  it("loads and creates books under the addressed project", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(bookBody, 201)));
    const client = new WorkbenchClient(fetch);
    const book = await client.createBook("project_demo", { title: "合成有声书" }, "book-key");
    expect(book.id).toBe("book_demo");
    expect(book.casting_revision).toBe("3");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).pathname).toBe("/v1/workbench/projects/project_demo/books");

    fetch.mockResolvedValueOnce(reply(bookBody, 200));
    expect((await client.book("book_demo")).project_id).toBe("project_demo");
  });

  it("reads one immutable voice revision and refuses a mismatched one", async () => {
    const revision = {
      voice_id: "voice_demo",
      revision: "2",
      display_name: "合成音色",
      status: "retired",
      languages: ["zh"],
      audio_formats: ["wav"],
      model_binding: { id: "binding_demo", revision: "3" },
      published_at: "2026-10-25T00:00:00Z",
      retired_at: "2026-10-25T01:00:00Z",
      disabled_reason: null,
    };
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(revision)));
    const client = new WorkbenchClient(fetch);
    const read = await client.voiceRevision("voice_demo", "2");
    expect(read.status).toBe("retired");
    expect(read.retired_at).toBe("2026-10-25T01:00:00Z");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).pathname).toBe("/v1/workbench/voices/voice_demo/revisions/2");

    fetch.mockResolvedValueOnce(reply({ ...revision, revision: "3" }));
    await expect(client.voiceRevision("voice_demo", "2")).rejects.toThrow();
  });

  it("renames a project against the metadata revision the caller saw", async () => {
    const renamed = {
      object: "nexus.workbench_project",
      id: "project_demo",
      workspace_id: "workspace_demo",
      name: "合成项目改名",
      description: null,
      status: "active",
      metadata_revision: 2,
      created_at: "2026-10-25T00:00:00Z",
      updated_at: "2026-10-25T00:00:01Z",
    };
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(renamed)));
    const client = new WorkbenchClient(fetch);
    const project = await client.updateProject(
      "project_demo",
      { expected_metadata_revision: 1, name: "合成项目改名" },
      "rename-key",
    );
    expect(project.name).toBe("合成项目改名");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).pathname).toBe("/v1/workbench/projects/project_demo");
    expect(request.method).toBe("PATCH");
    expect(request.headers.get("Idempotency-Key")).toBe("rename-key");

    // A reply that does not advance the revision means the change was not applied.
    fetch.mockResolvedValueOnce(reply({ ...renamed, metadata_revision: 1 }));
    await expect(
      client.updateProject(
        "project_demo",
        { expected_metadata_revision: 1, name: "合成项目改名" },
        "rename-key-2",
      ),
    ).rejects.toThrow();
  });

  it("creates a render unit under its chapter and refuses one from another chapter", async () => {
    const created = {
      object: "nexus.render_unit",
      id: "unit_demo",
      chapter_id: "chapter_demo",
      ordinal: 0,
      revision: "1",
      text: "合成旁白单元",
      voice: { id: "voice_demo", revision: "1" },
      model: "tts-quality",
      model_binding: { id: "binding_demo", revision: "3" },
      status: "pending",
      updated_at: "2026-10-25T00:00:00Z",
    };
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(created, 201)));
    const client = new WorkbenchClient(fetch);
    const unit = await client.createRenderUnit(
      "chapter_demo",
      {
        ordinal: 0,
        text: "合成旁白单元",
        voice: { id: "voice_demo", revision: "1" },
        model: "tts-quality",
        model_binding: { id: "binding_demo", revision: "3" },
      },
      "unit-key",
    );
    expect(unit.id).toBe("unit_demo");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).pathname).toBe("/v1/workbench/chapters/chapter_demo/render-units");
    expect(request.method).toBe("POST");
    expect(request.headers.get("Idempotency-Key")).toBe("unit-key");

    fetch.mockResolvedValueOnce(reply({ ...created, chapter_id: "chapter_other" }, 201));
    await expect(
      client.createRenderUnit(
        "chapter_demo",
        {
          ordinal: 0,
          text: "合成旁白单元",
          voice: { id: "voice_demo", revision: "1" },
          model: "tts-quality",
          model_binding: { id: "binding_demo", revision: "3" },
        },
        "unit-key-2",
      ),
    ).rejects.toThrow();
  });

  it("creates a chapter under the book it belongs to and replays the given key", async () => {
    const created = {
      object: "nexus.chapter",
      id: "chapter_demo",
      book_id: "book_demo",
      title: "第一章",
      ordinal: 0,
      text_revision: "1",
      created_at: "2026-10-25T00:00:00Z",
    };
    const fetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(created, 201)));
    const client = new WorkbenchClient(fetch);
    const chapter = await client.createChapter(
      "book_demo",
      { title: "第一章", ordinal: 0, text: "合成章节正文" },
      "chapter-key",
    );
    expect(chapter.id).toBe("chapter_demo");
    const request = fetch.mock.calls[0]![0] as Request;
    expect(new URL(request.url).pathname).toBe("/v1/workbench/books/book_demo/chapters");
    expect(request.method).toBe("POST");
    expect(request.headers.get("Idempotency-Key")).toBe("chapter-key");

    // A chapter that belongs to another book must not be presented as this book's chapter.
    fetch.mockResolvedValueOnce(reply({ ...created, book_id: "book_other" }, 201));
    await expect(
      client.createChapter("book_demo", { title: "第一章", ordinal: 0, text: "合成章节正文" }, "k"),
    ).rejects.toThrow();
  });

  it("lists books, chapters and render units inside their own parent", async () => {
    const books = example("listBooks");
    const bookFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(books)));
    await expect(
      new WorkbenchClient(bookFetch).books("project_demo", "cursor-demo"),
    ).resolves.toBeTruthy();
    const bookRequest = bookFetch.mock.calls[0]![0] as Request;
    expect(new URL(bookRequest.url).pathname).toBe("/v1/workbench/projects/project_demo/books");
    expect(new URL(bookRequest.url).searchParams.get("cursor")).toBe("cursor-demo");
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(books))).books("project-other"),
    ).rejects.toThrow();

    const chapters = example("listChapters");
    const chapterFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(chapters)));
    await expect(new WorkbenchClient(chapterFetch).chapters("book_demo")).resolves.toBeTruthy();
    const chapterRequest = chapterFetch.mock.calls[0]![0] as Request;
    expect(new URL(chapterRequest.url).pathname).toBe("/v1/workbench/books/book_demo/chapters");
    expect(new URL(chapterRequest.url).searchParams.get("cursor")).toBeNull();
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(chapters))).chapters("book-other"),
    ).rejects.toThrow();

    const units = example("listRenderUnits");
    const unitFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(units)));
    await expect(new WorkbenchClient(unitFetch).renderUnits("chapter_demo")).resolves.toBeTruthy();
    const unitRequest = unitFetch.mock.calls[0]![0] as Request;
    expect(new URL(unitRequest.url).pathname).toBe(
      "/v1/workbench/chapters/chapter_demo/render-units",
    );
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(units))).renderUnits("chapter-other"),
    ).rejects.toThrow();
  });

  it("copies a committed artifact into a project through its own operation", async () => {
    const asset = example("saveProjectAsset") as {
      id: string;
      project_id: string;
      source_artifact_id: string;
      source_receipt_id: string;
    };
    const body = {
      source_artifact_id: asset.source_artifact_id,
      source_receipt_id: asset.source_receipt_id,
    };
    const saveFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(asset, 201)));
    const saved = await new WorkbenchClient(saveFetch).saveProjectAsset(
      asset.project_id,
      body,
      "key-demo",
    );
    expect(saved.id).toBe(asset.id);
    const request = saveFetch.mock.calls[0]![0] as Request;
    expect(request.method).toBe("POST");
    expect(new URL(request.url).pathname).toBe(
      `/v1/workbench/projects/${asset.project_id}/saved-assets`,
    );
    expect(request.headers.get("Idempotency-Key")).toBe("key-demo");
    // A copy recorded under another project is not this project's copy.
    await expect(
      new WorkbenchClient(() =>
        Promise.resolve(reply({ ...asset, project_id: "project-other" }, 201)),
      ).saveProjectAsset(asset.project_id, body, "key-demo"),
    ).rejects.toThrow();
  });

  it("reads the published voice catalogue from its own operation only", async () => {
    const voices = example("listVoices");
    const voiceFetch = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(voices)));
    await expect(new WorkbenchClient(voiceFetch).voices()).resolves.toBeTruthy();
    const voiceRequest = voiceFetch.mock.calls[0]![0] as Request;
    expect(new URL(voiceRequest.url).pathname).toBe("/v1/workbench/voices");
    expect(voiceRequest.redirect).toBe("error");

    // The same voice pinned at two revisions is a legitimate page; the same voice and
    // revision twice is not, and the client refuses to pass it on as a catalogue.
    const doubled = structuredClone(voices) as { data: unknown[] };
    doubled.data = [doubled.data[0], doubled.data[0]];
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(doubled))).voices(),
    ).rejects.toThrow();

    const twoRevisions = structuredClone(voices) as { data: { revision: string }[] };
    twoRevisions.data = [twoRevisions.data[0]!, { ...twoRevisions.data[0]!, revision: "9" }];
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(twoRevisions))).voices(),
    ).resolves.toBeTruthy();
  });

  it("revises a render unit exactly one revision and never retries a conflict", async () => {
    const unit = parse("RenderUnit", {
      object: "nexus.render_unit",
      id: "unit_demo",
      chapter_id: "chapter_demo",
      ordinal: 0,
      revision: "1",
      text: "合成旁白单元",
      voice: { id: "voice_demo", revision: "1" },
      model: "tts-quality",
      model_binding: { id: "binding_demo", revision: "3" },
      status: "pending",
      updated_at: "2026-10-24T12:00:00Z",
    });
    const revised = { ...structuredClone(unit), revision: "2", status: "stale" };
    const patch = { expected_revision: "1", text: "改写后的旁白" };
    const fetch = vi.fn<typeof globalThis.fetch>(() =>
      Promise.resolve(reply({ ...revised, text: patch.text })),
    );
    const client = new WorkbenchClient(fetch);
    await expect(client.updateRenderUnit(unit.id, patch, "redo-key")).resolves.toBeTruthy();
    const request = fetch.mock.calls[0]![0] as Request;
    expect(request.method).toBe("PATCH");
    expect(new URL(request.url).pathname).toBe(`/v1/workbench/render-units/${unit.id}`);

    // A response that stays behind the expected revision is refused.
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(unit))).updateRenderUnit(
        unit.id,
        { expected_revision: "9", text: "改写后的旁白" },
        "redo-key",
      ),
    ).rejects.toThrow();

    // The same revision coming back is a no-op, not an error.
    await expect(
      new WorkbenchClient(() =>
        Promise.resolve(reply({ ...unit, text: patch.text })),
      ).updateRenderUnit(unit.id, patch, "redo-key"),
    ).resolves.toBeTruthy();

    const conflict = vi.fn<typeof globalThis.fetch>(() => Promise.resolve(reply(failure, 409)));
    await expect(
      new WorkbenchClient(conflict).updateRenderUnit(
        unit.id,
        { expected_revision: "1", text: "改写后的旁白" },
        "redo-key",
      ),
    ).rejects.toThrow();
    expect(conflict).toHaveBeenCalledTimes(1);
  });
});
