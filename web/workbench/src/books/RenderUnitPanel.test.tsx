import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkbenchClient,
  WorkbenchResponseError,
  type Contracts,
  parse,
} from "../manuscripts/workbench-client";
import { RenderUnitPanel } from "./RenderUnitPanel";

function unit(changes: Partial<Contracts["RenderUnitV2"]> = {}) {
  return parse("RenderUnitV2", {
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
    ...changes,
  });
}

const chapter = parse("Chapter", {
  object: "nexus.chapter",
  id: "chapter_demo",
  book_id: "book_demo",
  title: "第一章",
  ordinal: 0,
  text_revision: "1",
  created_at: "2026-10-24T12:00:00Z",
});

function emptyCatalogue() {
  return parse("VoiceList", {
    object: "nexus.list",
    data: [],
    has_more: false,
    next_cursor: null,
  });
}

function setup(
  units: Contracts["RenderUnitV2"][] = [unit()],
  catalogue: () => Promise<Contracts["VoiceList"]> = () => Promise.resolve(emptyCatalogue()),
) {
  const client = new WorkbenchClient(vi.fn());
  const list = vi.spyOn(client, "renderUnits").mockResolvedValue(
    parse("RenderUnitListV2", {
      object: "nexus.list",
      data: units,
      has_more: false,
      next_cursor: null,
    }),
  );
  const voices = vi.spyOn(client, "voices").mockImplementation(catalogue);
  const generate = vi.spyOn(client, "generateRenderUnit").mockResolvedValue(
    parse("RenderUnitGenerationAcceptedV2", {
      render_unit: unit({
        status: "generating",
        revision: "2",
        speech_job_id: "job_demo",
      }),
      speech_job_id: "job_demo",
      accepted_at: "2026-10-24T12:00:00Z",
    }),
  );
  const read = vi
    .spyOn(client, "renderUnit")
    .mockResolvedValue(unit({ status: "generating", revision: "2", speech_job_id: "job_demo" }));
  const book = vi.spyOn(client, "book").mockResolvedValue(
    parse("Book", {
      object: "nexus.book",
      id: "book_demo",
      project_id: "project_demo",
      title: "合成书",
      status: "draft",
      casting_revision: "1",
      created_at: "2026-10-24T12:00:00Z",
      updated_at: "2026-10-24T12:00:00Z",
    }),
  );
  const save = vi.spyOn(client, "saveProjectAsset").mockResolvedValue(
    parse("SavedAsset", {
      object: "nexus.saved_asset",
      id: "asset_saved",
      project_id: "project_demo",
      display_name: null,
      media_type: "audio/wav",
      bytes: 1024,
      sha256: "sha256:" + "a".repeat(64),
      source_artifact_id: "artifact_1",
      source_receipt_id: "receipt_1",
      created_at: "2026-10-24T12:00:00Z",
    }),
  );
  const history = vi.spyOn(client, "renderUnitGenerations").mockResolvedValue(
    parse("RenderUnitGenerationListV2", {
      object: "nexus.list",
      unit_id: "unit_demo",
      data: [
        {
          id: "gen:1",
          job_id: "job_1",
          dependencies_sha256: "sha256:" + "a".repeat(64),
          revision: "2",
        },
      ],
      has_more: false,
    }),
  );
  const update = vi.spyOn(client, "updateRenderUnit").mockImplementation((id, body) =>
    Promise.resolve(
      unit({
        id,
        revision: "2",
        status: "stale",
        text: body.text ?? "合成旁白单元",
        voice: body.voice ?? { id: "voice_demo", revision: "1" },
      }),
    ),
  );
  const view = render(<RenderUnitPanel client={client} chapter={chapter} />);
  return { client, list, generate, read, book, save, history, update, voices, ...view };
}

describe("render unit panel", () => {
  it("lists the chapter's units in reading order and says when there are none", async () => {
    const { list } = setup([unit({ id: "unit_two", ordinal: 1 }), unit({ id: "unit_one" })]);
    await screen.findByRole("list", { name: "单元列表" });
    expect(list).toHaveBeenCalledWith("chapter_demo");
    const items = within(screen.getByRole("list", { name: "单元列表" }))
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(items[0]).toContain("单元 1");
    expect(items[1]).toContain("单元 2");

    const empty = new WorkbenchClient(vi.fn());
    vi.spyOn(empty, "renderUnits").mockResolvedValue(
      parse("RenderUnitListV2", {
        object: "nexus.list",
        data: [],
        has_more: false,
        next_cursor: null,
      }),
    );
    render(<RenderUnitPanel client={empty} chapter={chapter} />);
    expect(await screen.findByText("该章节还没有渲染单元。")).toBeTruthy();
  });

  it("sends the changed pins with the revision the user saw", async () => {
    const { update } = setup();
    const textarea = await screen.findByLabelText("单元 1 文本");
    fireEvent.change(textarea, { target: { value: "改写后的旁白" } });
    fireEvent.change(screen.getByLabelText("单元 1 声音修订"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    const [id, body, key] = update.mock.calls[0]!;
    expect(id).toBe("unit_demo");
    expect(body).toEqual({
      expected_revision: "1",
      text: "改写后的旁白",
      voice: { id: "voice_demo", revision: "2" },
    });
    expect(typeof key).toBe("string");
    expect((await screen.findByText(/修订 1 → 2/)).textContent).toContain("stale");
  });

  it("reports a revision conflict instead of retrying it", async () => {
    const { list, update } = setup();
    update.mockRejectedValue(
      new WorkbenchResponseError(409, {
        error: {
          code: "revision_conflict",
          type: "invalid_request_error",
          message: "private-synthetic",
          param: null,
          request_id: "request-demo",
          retryable: false,
        },
      }),
    );
    fireEvent.change(await screen.findByLabelText("单元 1 文本"), {
      target: { value: "改写后的旁白" },
    });
    fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
    expect((await screen.findByRole("alert")).textContent).toContain("修订冲突");
    expect(update).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新单元" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  });

  it("refuses to submit a unit the user did not change", async () => {
    const { update } = setup();
    await screen.findByRole("list", { name: "单元列表" });
    fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
    expect((await screen.findByText(/没有变化/)).textContent).toContain("未提交");
    expect(update).not.toHaveBeenCalled();
  });
});

it("pins the published revision the user picked from the catalogue", async () => {
  const { update } = setup([unit()], () =>
    Promise.resolve(
      parse("VoiceList", {
        object: "nexus.list",
        data: [
          parse("VoiceSummary", {
            voice_id: "voice_published",
            revision: "4",
            display_name: "已发布旁白",
            source_type: "designed",
            status: "published",
            languages: ["zh-CN"],
            audio_formats: ["wav"],
          }),
        ],
        has_more: false,
        next_cursor: null,
      }),
    ),
  );
  const select = await screen.findByLabelText("单元 1 已发布音色");
  await waitFor(() =>
    expect(within(select).getByRole("option", { name: /已发布旁白/ })).toBeVisible(),
  );
  fireEvent.change(select, { target: { value: "voice_published@4" } });
  expect(select).toHaveValue("voice_published@4");
  fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
  await waitFor(() =>
    expect(update).toHaveBeenCalledWith(
      "unit_demo",
      { expected_revision: "1", voice: { id: "voice_published", revision: "4" } },
      expect.any(String),
    ),
  );
});

it("reports an absent catalogue instead of offering invented voices", async () => {
  setup([unit()], () =>
    Promise.reject(
      new WorkbenchResponseError(
        503,
        parse("ErrorResponse", {
          error: {
            code: "voice_catalogue_unavailable",
            type: "service_unavailable_error",
            message: "Voice catalogue is unavailable.",
            request_id: "request-demo",
            param: null,
            retryable: true,
          },
        }),
      ),
    ),
  );
  const select = await screen.findByLabelText("单元 1 已发布音色");
  await waitFor(() => expect(screen.getByText(/音色目录不可用/)).toBeVisible());
  // Only the unit's current pin remains: the picker shows no catalogue it does not have.
  expect(within(select).getAllByRole("option")).toHaveLength(1);
  expect(within(select).getByRole("option")).toHaveTextContent("当前固定：voice_demo@1");
});

it("shows the retained saved copy of a unit", async () => {
  // The contract couples a saved copy to the source pair it was made from.
  setup([
    unit({
      saved_asset_id: "asset-copy",
      source_artifact_id: "artifact-audio",
      source_receipt_id: "receipt-demo",
    }),
  ]);
  expect(await screen.findByText("已保存副本 asset-copy")).toBeVisible();
});

it("says a unit has no saved copy instead of implying one exists", async () => {
  setup([unit()]);
  expect(await screen.findByText("尚无已保存副本")).toBeVisible();
});

it("reads a voice revision with its retirement and disable facts", async () => {
  const { client } = setup([unit()]);
  const read = vi.spyOn(client, "voiceRevision").mockResolvedValue(
    parse("VoiceRevisionV2", {
      voice_id: "voice_demo",
      revision: "2",
      display_name: "合成音色",
      status: "disabled",
      languages: ["zh"],
      audio_formats: ["wav"],
      model_binding: { id: "binding_demo", revision: "3" },
      published_at: "2026-10-25T00:00:00Z",
      retired_at: "2026-10-26T01:00:00Z",
      disabled_reason: "停".repeat(2000),
    }),
  );
  await screen.findByLabelText("核对音色 id");
  expect(read).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("核对音色 id"), { target: { value: "voice_demo" } });
  fireEvent.change(screen.getByLabelText("核对音色修订"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "读取该音色修订" }));
  const info = await screen.findByRole("status", { name: "音色修订信息" });
  expect(info).toHaveTextContent("合成音色");
  expect(info).toHaveTextContent("disabled");
  expect(info).toHaveTextContent("停".repeat(2000));
  expect(info).toHaveTextContent("2026-10-26T01:00:00Z");
  expect(info).toHaveTextContent("binding_demo@3");
  expect(read).toHaveBeenCalledWith("voice_demo", "2");
});

it("reports an unavailable voice revision instead of showing a stale one", async () => {
  const { client } = setup([unit()]);
  vi.spyOn(client, "voiceRevision").mockRejectedValue(
    new WorkbenchResponseError(503, {
      error: {
        code: "voice_catalogue_unavailable",
        type: "service_unavailable_error",
        message: "合成不可用",
        param: null,
        request_id: "request_demo",
        retryable: true,
      },
    }),
  );
  await screen.findByLabelText("核对音色 id");
  fireEvent.change(screen.getByLabelText("核对音色 id"), { target: { value: "voice_demo" } });
  fireEvent.change(screen.getByLabelText("核对音色修订"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "读取该音色修订" }));
  const alert = await screen.findByRole("alert", { name: "音色修订错误" });
  expect(alert).toHaveTextContent("音色目录不可用");
  expect(screen.queryByRole("status", { name: "音色修订信息" })).toBeNull();
});

it("adds a render unit with the pin, model and binding the operator entered", async () => {
  const { client } = setup([]);
  const create = vi
    .spyOn(client, "createRenderUnit")
    .mockResolvedValue(
      unit({ id: "unit_new", text: "新增旁白", voice: { id: "voice_new", revision: "4" } }),
    );
  await screen.findByText("该章节还没有渲染单元。");
  fireEvent.change(screen.getByLabelText("新单元序号"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("新单元文本"), { target: { value: "新增旁白" } });
  fireEvent.change(screen.getByLabelText("新单元音色 id"), { target: { value: "voice_new" } });
  fireEvent.change(screen.getByLabelText("新单元音色修订"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("新单元模型"), { target: { value: "tts-quality" } });
  fireEvent.change(screen.getByLabelText("新单元绑定 id"), { target: { value: "binding_new" } });
  fireEvent.change(screen.getByLabelText("新单元绑定修订"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "新增渲染单元" }));
  expect(await screen.findByText("新增旁白")).toBeTruthy();
  expect(create).toHaveBeenCalledWith(
    "chapter_demo",
    {
      ordinal: 0,
      text: "新增旁白",
      voice: { id: "voice_new", revision: "4" },
      model: "tts-quality",
      model_binding: { id: "binding_new", revision: "2" },
    },
    expect.any(String),
  );
});

it("returns focus to the first field after a created unit clears the form", async () => {
  const { client } = setup([]);
  vi.spyOn(client, "createRenderUnit").mockResolvedValue(unit({ id: "unit_new" }));
  await screen.findByText("该章节还没有渲染单元。");
  fireEvent.change(screen.getByLabelText("新单元序号"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("新单元文本"), { target: { value: "新增旁白" } });
  fireEvent.change(screen.getByLabelText("新单元音色 id"), { target: { value: "voice_new" } });
  fireEvent.change(screen.getByLabelText("新单元音色修订"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("新单元模型"), { target: { value: "tts-quality" } });
  fireEvent.change(screen.getByLabelText("新单元绑定 id"), { target: { value: "binding_new" } });
  fireEvent.change(screen.getByLabelText("新单元绑定修订"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "新增渲染单元" }));
  await screen.findByText("合成旁白单元");
  // The cleared form disables the button; the keyboard user keeps a focus target.
  expect(document.activeElement).toBe(screen.getByLabelText("新单元序号"));
});

it("keeps the new-unit key after a rejected creation so it can be replayed", async () => {
  const { client } = setup([]);
  const create = vi
    .spyOn(client, "createRenderUnit")
    .mockRejectedValueOnce(new TypeError("合成连接失败"))
    .mockResolvedValue(unit({ id: "unit_new" }));
  await screen.findByText("该章节还没有渲染单元。");
  fireEvent.change(screen.getByLabelText("新单元序号"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("新单元文本"), { target: { value: "新增旁白" } });
  fireEvent.change(screen.getByLabelText("新单元音色 id"), { target: { value: "voice_new" } });
  fireEvent.change(screen.getByLabelText("新单元音色修订"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("新单元模型"), { target: { value: "tts-quality" } });
  fireEvent.change(screen.getByLabelText("新单元绑定 id"), { target: { value: "binding_new" } });
  fireEvent.change(screen.getByLabelText("新单元绑定修订"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "新增渲染单元" }));
  expect(await screen.findByRole("alert", { name: "新增单元错误" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "按原键重放单元请求" }));
  await screen.findByText("合成旁白单元");
  const keys = create.mock.calls.map((call) => call[2]);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
});

it("reads a saved copy back by id only when asked, and shows its own record", async () => {
  const { client } = setup([
    unit({
      saved_asset_id: "asset_saved_one",
      source_artifact_id: "artifact_one",
      source_receipt_id: "receipt_one",
    }),
  ]);
  const read = vi.spyOn(client, "savedAsset").mockResolvedValue(
    parse("SavedAsset", {
      object: "nexus.saved_asset",
      id: "asset_saved_one",
      project_id: "project_demo",
      display_name: null,
      media_type: "audio/wav",
      bytes: 1024,
      sha256: `sha256:${"a".repeat(64)}`,
      source_artifact_id: "artifact_one",
      source_receipt_id: "receipt_one",
      created_at: "2026-10-25T00:00:00Z",
    }),
  );
  await screen.findByText("已保存副本 asset_saved_one");
  // Nothing is fetched until the operator asks.
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "单元 1 读取副本" }));
  const info = await screen.findByRole("status", { name: "单元 1 副本信息" });
  expect(info).toHaveTextContent("audio/wav");
  expect(info).toHaveTextContent("1024 字节");
  expect(info).toHaveTextContent(`sha256:${"a".repeat(64)}`);
  expect(read).toHaveBeenCalledWith("asset_saved_one");
});

it("reports an unreadable saved copy instead of inventing its record", async () => {
  const { client } = setup([
    unit({
      saved_asset_id: "asset_saved_gone",
      source_artifact_id: "artifact_one",
      source_receipt_id: "receipt_one",
    }),
  ]);
  vi.spyOn(client, "savedAsset").mockRejectedValue(
    new WorkbenchResponseError(404, {
      error: {
        code: "resource_not_found",
        type: "invalid_request_error",
        message: "合成缺失",
        param: null,
        request_id: "request_demo",
        retryable: false,
      },
    }),
  );
  await screen.findByText("已保存副本 asset_saved_gone");
  fireEvent.click(screen.getByRole("button", { name: "单元 1 读取副本" }));
  const alert = await screen.findByRole("alert", { name: "单元 1 副本错误" });
  expect(alert).toHaveTextContent("已不可读取");
  expect(screen.queryByRole("status", { name: "单元 1 副本信息" })).toBeNull();
});

beforeEach(() => sessionStorage.clear());

it("replays the original unit patch and key after a committed reply is lost and the panel remounts", async () => {
  const view = setup();
  const accepted = unit({ text: "合成改写", revision: "2", status: "stale" });
  const committed = new Map<string, typeof accepted>();
  view.update.mockImplementation((_id, _patch, key) => {
    if (!committed.has(key)) {
      committed.set(key, accepted);
      return Promise.reject(new TypeError("synthetic lost reply"));
    }
    return Promise.resolve(committed.get(key)!);
  });
  fireEvent.change(await screen.findByLabelText("单元 1 文本"), { target: { value: "合成改写" } });
  fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
  await screen.findByRole("alert");
  view.unmount();
  view.list.mockResolvedValue(
    parse("RenderUnitListV2", {
      object: "nexus.list",
      data: [accepted],
      has_more: false,
      next_cursor: null,
    }),
  );
  render(<RenderUnitPanel client={view.client} chapter={chapter} />);
  const replay = await screen.findByRole("button", { name: "按原键重放单元请求" });
  expect(view.update).toHaveBeenCalledTimes(1);
  fireEvent.click(replay);
  await waitFor(() => expect(view.update).toHaveBeenCalledTimes(2));
  expect(view.update.mock.calls[1]).toEqual(view.update.mock.calls[0]);
  expect(committed.size).toBe(1);
});
it("does not append a late unit creation to the chapter opened afterwards", async () => {
  const view = setup([]);
  let finish!: (result: Contracts["RenderUnitV2"]) => void;
  vi.spyOn(view.client, "createRenderUnit").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await screen.findByText("该章节还没有渲染单元。");
  for (const [label, value] of [
    ["新单元序号", "0"],
    ["新单元文本", "A章合成正文"],
    ["新单元音色 id", "voice_demo"],
    ["新单元音色修订", "1"],
    ["新单元模型", "tts-quality"],
    ["新单元绑定 id", "binding_demo"],
    ["新单元绑定修订", "3"],
  ]) {
    fireEvent.change(screen.getByLabelText(label!), { target: { value } });
  }
  fireEvent.click(screen.getByRole("button", { name: "新增渲染单元" }));
  await waitFor(() => expect(finish).toBeTypeOf("function"));
  view.rerender(
    <RenderUnitPanel
      client={view.client}
      chapter={{ ...chapter, id: "chapter_other", title: "第二章" }}
    />,
  );
  await waitFor(() => expect(view.list).toHaveBeenCalledWith("chapter_other"));
  await act(async () => {
    finish(unit({ id: "late_unit", text: "A章合成正文" }));
    await Promise.resolve();
  });
  expect(screen.queryByRole("list", { name: "单元列表" })).toBeNull();
});

it("blocks a unit patch before external effects if its private storage fails", async () => {
  const { update } = setup();
  fireEvent.change(await screen.findByLabelText("单元 1 文本"), { target: { value: "合成修改" } });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("synthetic quota");
  });
  fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
  await screen.findByRole("alert");
  expect(update).not.toHaveBeenCalled();
});
it("keeps the original unit patch after an unknown outcome followed by a 409", async () => {
  const { update } = setup();
  update.mockRejectedValueOnce(new TypeError("synthetic lost reply")).mockRejectedValueOnce(
    new WorkbenchResponseError(409, {
      error: {
        code: "revision_conflict",
        type: "invalid_request_error",
        message: "synthetic ingress refusal",
        param: null,
        request_id: "request_demo",
        retryable: false,
      },
    }),
  );
  fireEvent.change(await screen.findByLabelText("单元 1 文本"), { target: { value: "合成修改" } });
  fireEvent.click(screen.getByRole("button", { name: "重做单元 1" }));
  const replay = await screen.findByRole("button", { name: "按原键重放单元请求" });
  await waitFor(() => expect(replay).not.toBeDisabled());
  fireEvent.click(replay);
  await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
  expect(update.mock.calls[1]).toEqual(update.mock.calls[0]);
  expect(screen.queryByRole("button", { name: "结束已拒绝单元请求" })).toBeNull();
  expect(screen.getByRole("button", { name: "重做单元 1" })).toBeDisabled();
});

it("dispatches generation for the revision shown and names the speech job", async () => {
  const { generate } = setup();
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 生成配音" }));
  await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
  const first = generate.mock.calls[0]!;
  expect(first[0]).toBe("unit_demo");
  expect(first[1]).toBe("1");
  expect(first[2]).toMatch(/^[0-9a-f-]{36}$/);
  await screen.findByText(/已提交生成，语音作业 job_demo/);
});

it("keeps the original key when the dispatch result is unknown", async () => {
  const { generate } = setup();
  generate.mockRejectedValueOnce(
    new WorkbenchResponseError(503, {
      error: {
        code: "outcome_unknown",
        type: "service_unavailable_error",
        message: "Mutation outcome is unknown.",
        param: null,
        request_id: "request-demo",
        retryable: false,
      },
    }),
  );
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 生成配音" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("结果尚未确认");
  fireEvent.click(screen.getByRole("button", { name: "按原键重放单元请求" }));
  await waitFor(() => expect(generate).toHaveBeenCalledTimes(2));
  expect(generate.mock.calls[1]![2]).toBe(generate.mock.calls[0]![2]);
  await screen.findByText(/已提交生成，语音作业 job_demo/);
});

it("polls a generating unit until the terminal status is recorded", async () => {
  vi.useFakeTimers();
  try {
    const { read } = setup([
      unit({ status: "generating", revision: "2", speech_job_id: "job_demo" }),
    ]);
    read.mockResolvedValue(
      unit({
        status: "ready",
        revision: "3",
        speech_job_id: "job_demo",
        saved_asset_id: "asset_1",
        source_artifact_id: "artifact_1",
        source_receipt_id: "receipt_1",
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(read).toHaveBeenCalledWith("unit_demo");
    expect(screen.getByText(/生成状态 ready/)).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("saves a ready unit's copy with its producer receipt and a stable key", async () => {
  const ready = unit({
    status: "ready",
    revision: "3",
    speech_job_id: "job_demo",
    source_artifact_id: "artifact_1",
    source_receipt_id: "receipt_1",
  });
  const { read, book, save } = setup([ready]);
  read.mockResolvedValue({ ...ready, saved_asset_id: "asset_saved" });
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 保存副本" }));
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
  const [projectId, body, key] = save.mock.calls[0]!;
  expect(projectId).toBe("project_demo");
  expect(body).toEqual({ source_artifact_id: "artifact_1", source_receipt_id: "receipt_1" });
  expect(key).toMatch(/^[0-9a-f-]{36}$/);
  expect(book).toHaveBeenCalledWith("book_demo");
  expect(read).toHaveBeenCalledWith("unit_demo");
  await screen.findByText(/已保存独立副本 asset_saved/);
  await screen.findByRole("button", { name: "单元 1 读取副本" });
});

it("reports a rejected copy without claiming it was saved", async () => {
  const ready = unit({
    status: "ready",
    revision: "3",
    speech_job_id: "job_demo",
    source_artifact_id: "artifact_1",
    source_receipt_id: "receipt_1",
  });
  const { save } = setup([ready]);
  save.mockRejectedValueOnce(
    new WorkbenchResponseError(422, {
      error: {
        code: "saved_asset_rejected",
        type: "invalid_request_error",
        message: "private-synthetic",
        param: null,
        request_id: "request-demo",
        retryable: false,
      },
    }),
  );
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 保存副本" }));
  const alert = await screen.findByRole("alert", { name: "单元 1 保存副本错误" });
  expect(alert.textContent).toContain("保存被拒绝");
  expect(screen.queryByText(/已保存独立副本/)).toBeNull();
});

it("shows the recorded generation history of one unit", async () => {
  const { history } = setup();
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 生成历史" }));
  await screen.findByRole("list", { name: "单元 1 生成记录" });
  expect(history).toHaveBeenCalledWith("unit_demo", 100);
  const records = screen.getByRole("list", { name: "单元 1 生成记录" });
  expect(records.textContent).toContain("修订 2");
  expect(records.textContent).toContain("job_1");
});

it("reports an unreadable history without inventing records", async () => {
  const { history } = setup();
  history.mockRejectedValueOnce(new Error("offline"));
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 生成历史" }));
  const alert = await screen.findByRole("alert", { name: "单元 1 生成历史错误" });
  expect(alert.textContent).toContain("连接不可用");
  expect(screen.queryByRole("list", { name: "单元 1 生成记录" })).toBeNull();
});

it("labels a capped history instead of pretending it is complete", async () => {
  const { history } = setup();
  history.mockResolvedValueOnce(
    parse("RenderUnitGenerationListV2", {
      object: "nexus.list",
      unit_id: "unit_demo",
      data: [
        {
          id: "gen:1",
          job_id: "job_1",
          dependencies_sha256: "sha256:" + "a".repeat(64),
          revision: "2",
        },
      ],
      has_more: true,
    }),
  );
  await screen.findByRole("list", { name: "单元列表" });
  fireEvent.click(screen.getByRole("button", { name: "单元 1 生成历史" }));
  await screen.findByText(/仅显示最近 1 条记录/);
});
