import { createValidatedClient } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "../../../../packages/contracts/src/workbench";
import { checkDeletion, targetKey } from "../deletions/deletion-plan";
import { verifiedDownload } from "./download-content";

export { parse, type Contracts } from "../../../../packages/contracts/src/workbench";

export class WorkbenchResponseError extends Error {
  readonly code: string;
  constructor(
    readonly status: number,
    detail: Contracts["ErrorResponse"],
  ) {
    super(`Workbench request failed (${status})`);
    this.code = detail.error.code;
  }
}

function result<K extends keyof Contracts>(
  name: K,
  value: {
    response: Response;
    data?: unknown;
    error?: unknown;
  },
): Contracts[K] {
  if (!value.response.ok)
    throw new WorkbenchResponseError(value.response.status, parse("ErrorResponse", value.error));
  return parse(name, value.data);
}

function identity(valid: boolean): asserts valid {
  if (!valid) throw new Error("Workbench resource mismatch");
}

function page<T extends { id: string }>(value: {
  data: T[];
  has_more: boolean;
  next_cursor: string | null;
}) {
  identity(value.has_more === (value.next_cursor !== null));
  identity(new Set(value.data.map((item) => item.id)).size === value.data.length);
}

function checkView(view: Contracts["TranscriptView"], id: string, revision?: string) {
  identity(view.transcript.id === id && view.revision.transcript_id === id);
  identity(view.revision.revision === (revision ?? view.transcript.head_revision));
  identity(BigInt(view.revision.revision) <= BigInt(view.transcript.head_revision));
  return view;
}

/** Deployment origin is fixed; generated boundaries own wire acceptance and byte limits. */
export class WorkbenchClient {
  readonly #api: ReturnType<typeof createValidatedClient>;
  constructor(transport: typeof globalThis.fetch = (input, init) => globalThis.fetch(input, init)) {
    const origin = location.origin;
    this.#api = createValidatedClient(origin, 8 * 1024 * 1024, (input, init) => {
      const request = new Request(input, {
        ...init,
        redirect: "error",
        cache: "no-store",
        credentials: "same-origin",
      });
      identity(new URL(request.url).origin === origin);
      return transport(request);
    });
  }

  async status(signal?: AbortSignal) {
    return result(
      "WorkbenchStatusV2",
      await this.#api.GET("/v2/workbench/status", { signal: signal ?? null }),
    );
  }

  async previewDeletion(body: Contracts["DeletionPlanRequest"], key: string) {
    const value = result(
      "DeletionPlan",
      await this.#api.POST("/v1/workbench/deletion-plans", {
        body: parse("DeletionPlanRequest", body),
        params: { header: { "Idempotency-Key": key } },
      }),
    );
    identity(value.mode === body.mode && targetKey(value.target) === targetKey(body.target));
    return value;
  }

  async acceptDeletion(body: Contracts["DeletionCreateRequest"], key: string) {
    const value = result(
      "Deletion",
      await this.#api.POST("/v1/workbench/deletions", {
        body: parse("DeletionCreateRequest", body),
        params: { header: { "Idempotency-Key": key } },
      }),
    );
    checkDeletion(value, body);
    return value;
  }

  async deletion(id: string, signal?: AbortSignal) {
    const value = result(
      "Deletion",
      await this.#api.GET("/v1/workbench/deletions/{deletion_id}", {
        params: { path: { deletion_id: id } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === id);
    return value;
  }

  async projects(cursor?: string, signal?: AbortSignal) {
    const value = result(
      "ProjectList",
      await this.#api.GET("/v1/workbench/projects", {
        params: { query: cursor === undefined ? {} : { cursor } },
        signal: signal ?? null,
      }),
    );
    page(value);
    return value;
  }

  async project(id: string, signal?: AbortSignal) {
    const value = result(
      "Project",
      await this.#api.GET("/v1/workbench/projects/{project_id}", {
        params: { path: { project_id: id } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === id);
    return value;
  }

  async createProject(body: Contracts["ProjectCreateRequest"], key: string) {
    const value = result(
      "Project",
      await this.#api.POST("/v1/workbench/projects", {
        body: parse("ProjectCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.name === body.name);
    if (body.workspace_id !== undefined) identity(value.workspace_id === body.workspace_id);
    if (body.description !== undefined) identity((value.description ?? null) === body.description);
    return value;
  }

  async transcripts(projectId: string, cursor?: string, signal?: AbortSignal) {
    const value = result(
      "TranscriptList",
      await this.#api.GET("/v1/workbench/projects/{project_id}/transcripts", {
        params: { path: { project_id: projectId }, query: cursor === undefined ? {} : { cursor } },
        signal: signal ?? null,
      }),
    );
    page(value);
    identity(value.data.every((item) => item.project_id === projectId));
    return value;
  }

  async createTranscript(
    projectId: string,
    body: Contracts["TranscriptCreateRequest"],
    key: string,
  ) {
    const value = result(
      "TranscriptView",
      await this.#api.POST("/v1/workbench/projects/{project_id}/transcripts", {
        params: { path: { project_id: projectId } },
        body: parse("TranscriptCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    checkView(value, value.transcript.id, "1");
    identity(
      value.transcript.project_id === projectId &&
        value.transcript.display_name === body.display_name,
    );
    identity(value.revision.source_revision === null);
    if (body.source.kind === "manual") {
      // A manual transcript starts as a draft revision; only a speech import is sealed.
      identity(value.revision.status === "draft");
      identity(value.revision.text === body.source.text);
    } else {
      identity(value.revision.status === "sealed");
      identity(
        value.transcript.source_artifact_id === body.source.artifact_id &&
          value.transcript.source_receipt_id === body.source.receipt_id,
      );
    }
    return value;
  }

  async view(id: string, revision?: string, signal?: AbortSignal) {
    const value = result(
      "TranscriptView",
      await this.#api.GET("/v1/workbench/transcripts/{transcript_id}", {
        params: { path: { transcript_id: id }, query: revision === undefined ? {} : { revision } },
        signal: signal ?? null,
      }),
    );
    return checkView(value, id, revision);
  }

  async revise(id: string, body: Contracts["TranscriptRevisionCreateRequest"], key: string) {
    const value = result(
      "TranscriptRevision",
      await this.#api.POST("/v1/workbench/transcripts/{transcript_id}/revisions", {
        params: { path: { transcript_id: id } },
        body: parse("TranscriptRevisionCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(
      value.transcript_id === id && value.revision === String(BigInt(body.expected_revision) + 1n),
    );
    identity(value.text === body.text && value.status === "draft");
    identity(value.source_revision === (body.source_revision ?? body.expected_revision));
    return value;
  }

  async createMeeting(projectId: string, body: Contracts["MeetingCreateRequest"], key: string) {
    const value = result(
      "Meeting",
      await this.#api.POST("/v1/workbench/projects/{project_id}/meetings", {
        params: { path: { project_id: projectId } },
        body: parse("MeetingCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.project_id === projectId && value.display_name === body.display_name);
    if (body.transcript_id !== undefined) identity(value.transcript_id === body.transcript_id);
    if (body.save_audio !== undefined) identity(value.save_audio === body.save_audio);
    identity(value.status === "capturing" && value.sealed_revision === null);
    return value;
  }

  async getMeeting(id: string, signal?: AbortSignal) {
    const value = result(
      "Meeting",
      await this.#api.GET("/v1/workbench/meetings/{meeting_id}", {
        params: { path: { meeting_id: id } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === id);
    return value;
  }

  async sealMeeting(id: string, body: Contracts["MeetingSealRequest"], key: string) {
    const value = result(
      "Meeting",
      await this.#api.POST("/v1/workbench/meetings/{meeting_id}/seal", {
        params: { path: { meeting_id: id } },
        body: parse("MeetingSealRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(
      value.id === id &&
        value.status === "sealed" &&
        value.sealed_revision === String(BigInt(body.expected_transcript_revision) + 1n),
    );
    return value;
  }

  async createMinutes(id: string, body: Contracts["MinutesCreateRequest"], key: string) {
    const value = result(
      "MinutesJob",
      await this.#api.POST("/v1/workbench/meetings/{meeting_id}/minutes", {
        params: { path: { meeting_id: id } },
        body: parse("MinutesCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(
      value.meeting_id === id &&
        value.template_revision === body.template_revision &&
        value.status === "queued",
    );
    if (body.llm_model !== undefined) identity(value.llm_model === body.llm_model);
    if (body.expected_source_revision !== undefined)
      identity(value.source_revision === body.expected_source_revision);
    return value;
  }

  async books(projectId: string, cursor?: string, signal?: AbortSignal) {
    const value = result(
      "BookList",
      await this.#api.GET("/v1/workbench/projects/{project_id}/books", {
        params: {
          path: { project_id: projectId },
          query: cursor === undefined ? {} : { cursor },
        },
        signal: signal ?? null,
      }),
    );
    page(value);
    identity(value.data.every((item) => item.project_id === projectId));
    return value;
  }

  async chapters(bookId: string, cursor?: string, signal?: AbortSignal) {
    const value = result(
      "ChapterList",
      await this.#api.GET("/v1/workbench/books/{book_id}/chapters", {
        params: { path: { book_id: bookId }, query: cursor === undefined ? {} : { cursor } },
        signal: signal ?? null,
      }),
    );
    page(value);
    identity(value.data.every((item) => item.book_id === bookId));
    return value;
  }

  /**
   * Chapters are addressed by the book the caller already has open; the response is checked
   * against that book so a server-side mix-up cannot be shown as this book's chapter.
   */
  async createChapter(
    bookId: string,
    body: { title: string; ordinal: number; text: string },
    key: string,
    signal?: AbortSignal,
  ) {
    const value = result(
      "Chapter",
      await this.#api.POST("/v1/workbench/books/{book_id}/chapters", {
        params: { path: { book_id: bookId } },
        headers: { "Idempotency-Key": key },
        body,
        signal: signal ?? null,
      }),
    );
    identity(value.book_id === bookId);
    return value;
  }

  async renderUnits(chapterId: string, cursor?: string, signal?: AbortSignal) {
    const value = result(
      "RenderUnitListV2",
      await this.#api.GET("/v2/workbench/chapters/{chapter_id}/render-units", {
        params: {
          path: { chapter_id: chapterId },
          query: cursor === undefined ? {} : { cursor },
        },
        signal: signal ?? null,
      }),
    );
    page(value);
    identity(value.data.every((item) => item.chapter_id === chapterId));
    return value;
  }

  /**
   * Metadata changes are version-checked: the caller states the revision it saw, and the
   * deployment refuses a stale one instead of silently overwriting a newer name.
   */
  async updateProject(
    projectId: string,
    body: {
      expected_metadata_revision: number;
      name?: string;
      description?: string | null;
      status?: "active" | "archived";
    },
    key: string,
    signal?: AbortSignal,
  ) {
    const value = result(
      "Project",
      await this.#api.PATCH("/v1/workbench/projects/{project_id}", {
        params: { path: { project_id: projectId } },
        headers: { "Idempotency-Key": key },
        body,
        signal: signal ?? null,
      }),
    );
    identity(value.id === projectId);
    identity(value.metadata_revision > body.expected_metadata_revision);
    return value;
  }

  /**
   * A unit is created under the chapter the caller has open; the deployment decides the
   * identity, so the response is checked against that chapter before it is shown.
   */
  async createRenderUnit(
    chapterId: string,
    body: Contracts["RenderUnitCreateRequestV2"],
    key: string,
    signal?: AbortSignal,
  ) {
    const value = result(
      "RenderUnitV2",
      await this.#api.POST("/v2/workbench/chapters/{chapter_id}/render-units", {
        params: { path: { chapter_id: chapterId } },
        headers: { "Idempotency-Key": key },
        body,
        signal: signal ?? null,
      }),
    );
    identity(value.chapter_id === chapterId);
    return value;
  }

  /**
   * The saved copy the workbench itself wrote: read back by id, never by a client-supplied
   * path or bucket. The digest and length are the workbench's own record of the copy.
   */
  async savedAsset(assetId: string, signal?: AbortSignal) {
    const value = result(
      "SavedAsset",
      await this.#api.GET("/v1/workbench/saved-assets/{asset_id}", {
        params: { path: { asset_id: assetId } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === assetId);
    return value;
  }

  async downloadSavedAsset(asset: Contracts["SavedAsset"], signal?: AbortSignal) {
    const pin = parse("SavedAsset", asset);
    const deadline = AbortSignal.timeout(120_000);
    const bounded = signal ? AbortSignal.any([signal, deadline]) : deadline;
    const reply = await this.#api.GET("/v1/workbench/saved-assets/{asset_id}/content", {
      params: { path: { asset_id: pin.id } },
      parseAs: "stream",
      signal: bounded,
    });
    if (!reply.response.ok)
      throw new WorkbenchResponseError(reply.response.status, parse("ErrorResponse", reply.error));
    identity(reply.data !== undefined && reply.data !== null);
    return verifiedDownload(reply.response, reply.data, pin, bounded);
  }

  /**
   * The deployment's teleprompter state is scoped to one project and records the script
   * revision, the visible position and the manual takeover — not the script text. A 404
   * means "unknown project or nothing saved yet", which is not position zero.
   */
  async teleprompterState(projectId: string, signal?: AbortSignal) {
    const value = result(
      "TeleprompterState",
      await this.#api.GET("/v1/workbench/projects/{project_id}/teleprompter", {
        params: { path: { project_id: projectId } },
        signal: signal ?? null,
      }),
    );
    identity(value.project_id === projectId);
    return value;
  }

  async updateTeleprompter(
    projectId: string,
    body: Contracts["TeleprompterUpdateRequest"],
    key: string,
  ) {
    const value = result(
      "TeleprompterState",
      await this.#api.PUT("/v1/workbench/projects/{project_id}/teleprompter", {
        params: { path: { project_id: projectId } },
        body: parse("TeleprompterUpdateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.project_id === projectId);
    // The stored revision never goes backwards: an older request is a conflict, not a write.
    identity(BigInt(value.script_revision) >= BigInt(body.script_revision));
    return value;
  }

  /**
   * The workbench owns the business turn; the speech base runs the job. Creation is
   * accepted, not completed: the caller polls the turn and cancels by id.
   */
  async createAssistantTurn(
    projectId: string,
    body: Contracts["AssistantTurnCreateRequest"],
    key: string,
  ) {
    const value = result(
      "AssistantTurn",
      await this.#api.POST("/v1/workbench/projects/{project_id}/assistant-turns", {
        params: { path: { project_id: projectId } },
        body: parse("AssistantTurnCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(
      value.project_id === projectId &&
        value.status === "queued" &&
        value.output_text === null &&
        value.error === null,
    );
    return value;
  }

  async assistantTurn(turnId: string, signal?: AbortSignal) {
    return result(
      "AssistantTurn",
      await this.#api.GET("/v1/workbench/assistant-turns/{turn_id}", {
        params: { path: { turn_id: turnId } },
        signal: signal ?? null,
      }),
    );
  }

  async cancelAssistantTurn(turnId: string, key: string) {
    const value = result(
      "AssistantTurn",
      await this.#api.POST("/v1/workbench/assistant-turns/{turn_id}/cancellations", {
        params: { path: { turn_id: turnId } },
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.id === turnId);
    return value;
  }

  /**
   * The catalogue is the deployment's read of the speech base's published voices. It stays
   * read-only: design, clone, validate and publish belong to the base, not to this client.
   */
  async voices(signal?: AbortSignal) {
    const value = result(
      "VoiceList",
      await this.#api.GET("/v1/workbench/voices", { signal: signal ?? null }),
    );
    identity(value.has_more === (value.next_cursor !== null));
    // One row per published revision: the same voice pinned twice in one page is a
    // protocol violation, while two revisions of one voice are legitimately distinct.
    identity(
      new Set(value.data.map((item) => `${item.voice_id}@${item.revision}`)).size ===
        value.data.length,
    );
    return value;
  }

  /**
   * One immutable voice revision. Retirement and disable reasons only exist here, not in the
   * published catalogue summary, so a pinned reference is checked against the revision itself.
   */
  async voiceRevision(voiceId: string, revision: string, signal?: AbortSignal) {
    const value = result(
      "VoiceRevisionV2",
      await this.#api.GET("/v2/workbench/voices/{voice_id}/revisions/{revision}", {
        params: { path: { voice_id: voiceId, revision } },
        signal: signal ?? null,
      }),
    );
    identity(value.voice_id === voiceId && value.revision === revision);
    return value;
  }

  /**
   * Copies a committed speech artifact into a project as the workbench's own saved copy.
   * The source pair is exactly what the base returned for the job, and the copy is written
   * into the workbench object zone and recorded: no client-supplied destination is used.
   */
  async saveProjectAsset(
    projectId: string,
    body: Contracts["SavedAssetCreateRequest"],
    key: string,
  ) {
    const value = result(
      "SavedAsset",
      await this.#api.POST("/v1/workbench/projects/{project_id}/saved-assets", {
        params: { path: { project_id: projectId } },
        body: parse("SavedAssetCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.project_id === projectId && value.object === "nexus.saved_asset");
    identity(value.source_artifact_id === body.source_artifact_id);
    identity(value.source_receipt_id === body.source_receipt_id);
    return value;
  }

  async book(id: string, signal?: AbortSignal) {
    const value = result(
      "Book",
      await this.#api.GET("/v1/workbench/books/{book_id}", {
        params: { path: { book_id: id } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === id && value.object === "nexus.book");
    return value;
  }

  async createBook(projectId: string, body: Contracts["BookCreateRequest"], key: string) {
    const value = result(
      "Book",
      await this.#api.POST("/v1/workbench/projects/{project_id}/books", {
        params: { path: { project_id: projectId } },
        body: parse("BookCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.project_id === projectId && value.title === body.title);
    return value;
  }

  async updateRenderUnit(unitId: string, body: Contracts["RenderUnitPatchV2"], key: string) {
    const value = result(
      "RenderUnitV2",
      await this.#api.PATCH("/v2/workbench/render-units/{unit_id}", {
        params: { path: { unit_id: unitId } },
        body: parse("RenderUnitPatchV2", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.id === unitId && value.object === "nexus.render_unit");
    // A revise either changes nothing (the same revision comes back) or moves the
    // unit exactly one revision forward and unpins its produced result.
    const expected = Number(body.expected_revision);
    const revision = Number(value.revision);
    identity(revision === expected || (revision === expected + 1 && value.status === "stale"));
    if (body.text !== undefined) identity(value.text === body.text);
    if (body.voice !== undefined) {
      identity(value.voice.id === body.voice.id);
      identity(value.voice.revision === body.voice.revision);
    }
    if (body.model !== undefined) identity(value.model === body.model);
    return value;
  }

  async createBookExport(bookId: string, body: Contracts["ExportCreateRequest"], key: string) {
    const value = result(
      "ExportJob",
      await this.#api.POST("/v1/workbench/books/{book_id}/exports", {
        params: { path: { book_id: bookId } },
        body: parse("ExportCreateRequest", body),
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.book_id === bookId && value.object === "nexus.export_job");
    // The export pins the revision it was asked for, and starts with nothing produced.
    identity(value.manifest_revision === body.expected_casting_revision);
    identity(value.format === body.format && value.status === "queued");
    identity(value.progress_completed === 0 && value.artifact === null && value.error === null);
    return value;
  }

  async bookExport(exportId: string, signal?: AbortSignal) {
    const value = result(
      "ExportJob",
      await this.#api.GET("/v1/workbench/exports/{export_id}", {
        params: { path: { export_id: exportId } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === exportId);
    return value;
  }

  async downloadBookExport(job: Contracts["ExportJob"], signal?: AbortSignal) {
    const pin = parse("ExportJob", job);
    identity(
      pin.status === "succeeded" &&
        pin.artifact != null &&
        pin.artifact.export_id === pin.id &&
        pin.artifact.manifest_revision === pin.manifest_revision,
    );
    const deadline = AbortSignal.timeout(120_000);
    const bounded = signal ? AbortSignal.any([signal, deadline]) : deadline;
    const reply = await this.#api.GET("/v1/workbench/exports/{export_id}/content", {
      params: { path: { export_id: pin.id } },
      parseAs: "stream",
      signal: bounded,
    });
    if (!reply.response.ok)
      throw new WorkbenchResponseError(reply.response.status, parse("ErrorResponse", reply.error));
    identity(reply.data !== undefined && reply.data !== null);
    return verifiedDownload(reply.response, reply.data, pin.artifact, bounded);
  }

  async cancelBookExport(exportId: string, key: string) {
    const value = result(
      "ExportJob",
      await this.#api.POST("/v1/workbench/exports/{export_id}/cancellations", {
        params: { path: { export_id: exportId } },
        headers: { "Idempotency-Key": key },
      }),
    );
    identity(value.id === exportId);
    return value;
  }

  async getMinutes(meetingId: string, minutesId: string, signal?: AbortSignal) {
    const value = result(
      "MinutesJob",
      await this.#api.GET("/v1/workbench/meetings/{meeting_id}/minutes/{minutes_id}", {
        params: { path: { meeting_id: meetingId, minutes_id: minutesId } },
        signal: signal ?? null,
      }),
    );
    identity(value.id === minutesId && value.meeting_id === meetingId);
    return value;
  }
}
