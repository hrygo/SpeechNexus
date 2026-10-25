import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/workbench-http.json";
import {
  WorkbenchClient,
  WorkbenchResponseError,
  type Contracts,
  parse,
} from "../manuscripts/workbench-client";
import { BooksHost } from "./BooksHost";

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

const book = parse("Book", {
  object: "nexus.book",
  id: "book_demo",
  project_id: "project-demo",
  title: "合成有声书",
  status: "draft",
  casting_revision: "3",
  created_at: "2026-10-24T12:00:00Z",
  updated_at: "2026-10-24T12:00:00Z",
});

function bookPage(items: Contracts["Book"][]) {
  return parse("BookList", {
    object: "nexus.list",
    data: structuredClone(items),
    has_more: false,
    next_cursor: null,
  });
}

function chapterPage(items: Contracts["Chapter"][]) {
  return parse("ChapterList", {
    object: "nexus.list",
    data: structuredClone(items),
    has_more: false,
    next_cursor: null,
  });
}

function chapter(id: string, ordinal: number, title: string) {
  return parse("Chapter", {
    object: "nexus.chapter",
    id,
    book_id: "book_demo",
    title,
    ordinal,
    text_revision: "1",
    created_at: "2026-10-24T12:00:00Z",
  });
}

function setup(
  remembered?: string,
  listed: Contracts["Book"][] = [book],
  listedChapters: Contracts["Chapter"][] = [],
) {
  const data = new Map<string, string>();
  if (remembered !== undefined) data.set(`speechnexus.workbench.book.project-demo`, remembered);
  const storage = {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
  const client = new WorkbenchClient(vi.fn());
  const projectList = parse("ProjectList", example("listProjects"));
  const projects = vi.spyOn(client, "projects").mockResolvedValue(projectList);
  const read = vi.spyOn(client, "book").mockResolvedValue(book);
  const create = vi.spyOn(client, "createBook").mockResolvedValue(book);
  const list = vi.spyOn(client, "books").mockResolvedValue(bookPage(listed));
  const chapters = vi.spyOn(client, "chapters").mockResolvedValue(chapterPage(listedChapters));
  const units = vi.spyOn(client, "renderUnits").mockResolvedValue(
    parse("RenderUnitList", {
      object: "nexus.list",
      data: [],
      has_more: false,
      next_cursor: null,
    }),
  );
  render(
    <StrictMode>
      <BooksHost client={client} storage={storage} />
    </StrictMode>,
  );
  return { client, projects, projectList, read, create, list, chapters, units, data };
}

describe("books host", () => {
  it("renames the selected project against the revision it read", async () => {
    const { client, projectList } = setup("book_demo", [book]);
    const selected = projectList.data[0]!;
    const update = vi.spyOn(client, "updateProject").mockResolvedValue({
      ...selected,
      name: "合成项目改名",
      metadata_revision: selected.metadata_revision + 1,
    });
    await screen.findByLabelText("新项目名称");
    fireEvent.change(screen.getByLabelText("新项目名称"), { target: { value: "合成项目改名" } });
    fireEvent.click(screen.getByRole("button", { name: "重命名项目" }));
    expect(await screen.findByRole("button", { name: "合成项目改名" })).toBeTruthy();
    expect(update).toHaveBeenCalledWith(
      selected.id,
      { expected_metadata_revision: selected.metadata_revision, name: "合成项目改名" },
      expect.any(String),
    );
  });

  it("returns focus to the name field after a rename clears it", async () => {
    const { client, projectList } = setup("book_demo", [book]);
    const selected = projectList.data[0]!;
    vi.spyOn(client, "updateProject").mockResolvedValue({
      ...selected,
      name: "合成项目改名",
      metadata_revision: selected.metadata_revision + 1,
    });
    await screen.findByLabelText("新项目名称");
    fireEvent.change(screen.getByLabelText("新项目名称"), { target: { value: "合成项目改名" } });
    fireEvent.click(screen.getByRole("button", { name: "重命名项目" }));
    await screen.findByRole("button", { name: "合成项目改名" });
    expect(document.activeElement).toBe(screen.getByLabelText("新项目名称"));
  });

  it("says a stale rename conflicted and keeps the key instead of guessing a new name", async () => {
    const { client, projectList } = setup("book_demo", [book]);
    const selected = projectList.data[0]!;
    const update = vi
      .spyOn(client, "updateProject")
      .mockRejectedValueOnce(
        new WorkbenchResponseError(409, {
          error: {
            code: "revision_conflict",
            type: "invalid_request_error",
            message: "合成冲突",
            param: null,
            request_id: "request_demo",
            retryable: false,
          },
        }),
      )
      .mockResolvedValue({
        ...selected,
        name: "合成项目改名",
        metadata_revision: selected.metadata_revision + 1,
      });
    await screen.findByLabelText("新项目名称");
    fireEvent.change(screen.getByLabelText("新项目名称"), { target: { value: "合成项目改名" } });
    fireEvent.click(screen.getByRole("button", { name: "重命名项目" }));
    const alert = await screen.findByRole("alert", { name: "项目错误" });
    expect(alert).toHaveTextContent("修订冲突");
    // The stale name is not shown as if it had been saved.
    expect(screen.queryByRole("button", { name: "合成项目改名" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重命名项目" }));
    await screen.findByRole("button", { name: "合成项目改名" });
    const keys = update.mock.calls.map((call) => call[2]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("adds a chapter to the open book and keeps the open chapter", async () => {
    const { client } = setup("book_demo", [book], []);
    const createChapter = vi
      .spyOn(client, "createChapter")
      .mockResolvedValue(chapter("chapter_new", 0, "第一章"));
    expect(await screen.findByText("这本书还没有章节。")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("新章节标题"), { target: { value: "第一章" } });
    fireEvent.change(screen.getByLabelText("新章节序号"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("新章节正文"), { target: { value: "合成章节正文" } });
    fireEvent.click(screen.getByRole("button", { name: "新增章节" }));
    expect(await screen.findByText(/第 1 章 · 第一章/)).toBeTruthy();
    expect(createChapter).toHaveBeenCalledWith(
      "book_demo",
      { title: "第一章", ordinal: 0, text: "合成章节正文" },
      expect.any(String),
    );
  });

  it("replays the same key after a rejected chapter instead of minting a new one", async () => {
    const { client } = setup("book_demo", [book], []);
    const createChapter = vi
      .spyOn(client, "createChapter")
      .mockRejectedValueOnce(new TypeError("合成连接失败"))
      .mockResolvedValue(chapter("chapter_new", 0, "第一章"));
    await screen.findByText("这本书还没有章节。");
    fireEvent.change(screen.getByLabelText("新章节标题"), { target: { value: "第一章" } });
    fireEvent.change(screen.getByLabelText("新章节序号"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("新章节正文"), { target: { value: "合成章节正文" } });
    fireEvent.click(screen.getByRole("button", { name: "新增章节" }));
    expect(await screen.findByRole("alert", { name: "章节错误" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "新增章节" }));
    await screen.findByText(/第 1 章 · 第一章/);
    const keys = createChapter.mock.calls.map((call) => call[2]);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("opens the book this browser remembered for the selected project", async () => {
    const { read } = setup("book_demo");
    expect(await screen.findByRole("heading", { name: "有声书导出" })).toBeTruthy();
    expect(read).toHaveBeenCalledWith("book_demo");
    expect(screen.getByText(/固定当前书目修订/)).toBeTruthy();
  });

  it("creates a book, remembers it and offers the export panel", async () => {
    const { create, data } = setup();
    fireEvent.change(await screen.findByLabelText("新有声书标题"), {
      target: { value: "新书" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建有声书" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0]![1]).toEqual({ title: "新书" });
    await screen.findByRole("heading", { name: "有声书导出" });
    expect(data.get("speechnexus.workbench.book.project-demo")).toBe("book_demo");
  });

  it("reports a book that cannot be read without inventing one", async () => {
    const { read } = setup("book_absent");
    read.mockRejectedValueOnce(
      new WorkbenchResponseError(404, {
        error: {
          code: "resource_not_found",
          type: "not_found_error",
          message: "private-synthetic",
          param: null,
          request_id: "request-demo",
          retryable: false,
        },
      }),
    );
    expect((await screen.findByRole("alert")).textContent).toContain("已不可读取");
    expect(screen.queryByRole("heading", { name: "有声书导出" })).toBeNull();
  });

  it("lists the project's books and opens one by click, without an id", async () => {
    const second = parse("Book", {
      object: "nexus.book",
      id: "book_other",
      project_id: "project-demo",
      title: "第二本有声书",
      status: "ready",
      casting_revision: "1",
      created_at: "2026-10-24T12:00:00Z",
      updated_at: "2026-10-24T12:00:00Z",
    });
    const { list, read } = setup(undefined, [book, second]);
    expect(await screen.findByText("第二本有声书")).toBeTruthy();
    expect(list).toHaveBeenCalledWith("project-demo");
    read.mockResolvedValue(second);
    fireEvent.click(screen.getByRole("button", { name: /第二本有声书/ }));
    await waitFor(() => expect(read).toHaveBeenCalledWith("book_other"));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /第二本有声书/ }).getAttribute("aria-pressed"),
      ).toBe("true"),
    );
  });

  it("shows the open book's chapters in reading order", async () => {
    setup(
      "book_demo",
      [book],
      [chapter("chapter_two", 1, "第二章"), chapter("chapter_one", 0, "第一章")],
    );
    expect(await screen.findByText(/第 1 章 · 第一章/)).toBeTruthy();
    const items = within(screen.getByRole("list", { name: "章节列表" }))
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(items[0]).toContain("第一章");
    expect(items[1]).toContain("第二章");
  });

  it("opens the render units of the chapter the user selects", async () => {
    const { units } = setup("book_demo", [book], [chapter("chapter_one", 0, "第一章")]);
    fireEvent.click(await screen.findByRole("button", { name: /第 1 章 · 第一章/ }));
    expect(await screen.findByRole("heading", { name: "渲染单元" })).toBeTruthy();
    expect(units).toHaveBeenCalledWith("chapter_one");
  });

  it("says the project has no books instead of inventing one", async () => {
    setup(undefined, []);
    expect(await screen.findByText("该项目还没有有声书，先创建一本。")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "书目列表" })).toBeNull();
  });
});
