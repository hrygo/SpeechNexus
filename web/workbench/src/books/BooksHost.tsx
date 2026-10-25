import { useEffect, useRef, useState } from "react";
import {
  WorkbenchClient,
  WorkbenchResponseError,
  type Contracts,
} from "../manuscripts/workbench-client";
import { BookExportPanel } from "./BookExportPanel";
import { RenderUnitPanel } from "./RenderUnitPanel";
import "./books.css";

export interface BookStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const problems = {
  unavailable: "当前部署尚未启用工作台，或连接不可用。",
  unknown: "结果尚未确认。请核对连接后按原键重试，不要重新创建。",
  missing: "书目或项目已不可读取。",
  rejected: "请求被拒绝。请核对输入后重试。",
  conflict: "该项目已被修改（修订冲突）。请刷新后按新修订重试，本页不会自动重试。",
} as const;

type Problem = keyof typeof problems;

function problemOf(error: unknown): Problem {
  if (error instanceof WorkbenchResponseError) {
    if (error.status === 404) return "missing";
    if (error.code === "invalid_change") return "rejected";
    if (error.code === "revision_conflict") return "conflict";
    if (error.status >= 500 || error.code === "store_unavailable") return "unavailable";
    return "unknown";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

function remembered(storage: BookStorage, projectId: string): string | null {
  try {
    return storage.getItem(`speechnexus.workbench.book.${projectId}`);
  } catch {
    return null;
  }
}

/**
 * The project's books and the open book's chapters come from their list operations, so
 * nothing here depends on remembering an id in this browser.
 */
export function BooksHost({
  client,
  active = true,
  storage,
}: {
  client?: WorkbenchClient;
  active?: boolean;
  storage?: BookStorage;
}) {
  const [session] = useState(() => client ?? new WorkbenchClient());
  const store = storage ?? window.sessionStorage;
  const [bookKey, setBookKey] = useState(() => crypto.randomUUID());
  const [projects, setProjects] = useState<Contracts["Project"][]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [chapterTitle, setChapterTitle] = useState("");
  const [chapterOrdinal, setChapterOrdinal] = useState("");
  const [chapterText, setChapterText] = useState("");
  const [chapterKey, setChapterKey] = useState(() => crypto.randomUUID());
  const [chapterProblem, setChapterProblem] = useState<Problem | null>(null);
  // Renaming clears the field, which disables the button: focus goes back to the field so a
  // keyboard user is not dropped onto the document.
  const renameField = useRef<HTMLInputElement>(null);
  const [renameName, setRenameName] = useState("");
  const [renameKey, setRenameKey] = useState(() => crypto.randomUUID());
  const [renameProblem, setRenameProblem] = useState<Problem | null>(null);
  const [book, setBook] = useState<Contracts["Book"] | null>(null);
  const [listed, setListed] = useState<Contracts["Book"][]>([]);
  const [chapters, setChapters] = useState<Contracts["Chapter"][]>([]);
  const [chapter, setChapter] = useState<Contracts["Chapter"] | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);
  const loaded = useRef<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await session.projects();
        if (cancelled) return;
        setProjects(page.data);
        const first = page.data[0]?.id ?? null;
        setProjectId((current) => current ?? first);
        setProblem(null);
      } catch (error) {
        if (!cancelled) setProblem(problemOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session, active]);

  useEffect(() => {
    if (!active || projectId === null) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await session.books(projectId);
        if (cancelled) return;
        setListed(page.data);
        setProblem(null);
      } catch (error) {
        if (!cancelled) setProblem(problemOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session, active, projectId]);

  useEffect(() => {
    if (!active || book === null) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await session.chapters(book.id);
        if (cancelled) return;
        setChapters(page.data);
        setProblem(null);
      } catch (error) {
        if (!cancelled) setProblem(problemOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session, active, book]);

  useEffect(() => {
    if (!active || projectId === null) return;
    const known = remembered(store, projectId);
    if (known === null || loaded.current === `${projectId}:${known}`) return;
    let cancelled = false;
    void (async () => {
      try {
        const value = await session.book(known);
        if (cancelled) return;
        loaded.current = `${projectId}:${known}`;
        setBook(value);
        setProblem(null);
      } catch (error) {
        if (!cancelled) setProblem(problemOf(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session, active, projectId, store]);

  function remember(value: Contracts["Book"]) {
    loaded.current = `${value.project_id}:${value.id}`;
    setBook(value);
    setChapters([]);
    setChapter(null);
    setProblem(null);
    try {
      store.setItem(`speechnexus.workbench.book.${value.project_id}`, value.id);
    } catch {
      // Local memory is a convenience; it never blocks the book we already loaded.
    }
  }

  async function open(value: string) {
    if (projectId === null) return;
    setBusy(true);
    try {
      remember(await session.book(value));
    } catch (error) {
      setProblem(problemOf(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="books" aria-label="有声书与导出工作台">
      <h2>有声书与导出</h2>
      <p>
        选择项目后创建或打开一本有声书，再导出当前书目修订。书目与章节都来自各自的列表接口，
        本浏览器记住的只是上一次打开的那一本。
      </p>
      {problem !== null && (
        <p className="books-problem" role="alert">
          {problems[problem]}
        </p>
      )}
      {projectId !== null && (
        <form
          aria-label="重命名项目"
          onSubmit={(event) => {
            event.preventDefault();
            setBusy(true);
            setRenameProblem(null);
            void (async () => {
              const current = projects.find((item) => item.id === projectId);
              if (!current) {
                setRenameProblem("missing");
                setBusy(false);
                return;
              }
              try {
                const renamed = await session.updateProject(
                  current.id,
                  { expected_metadata_revision: current.metadata_revision, name: renameName },
                  renameKey,
                );
                // The deployment decides the new revision: show what it returned, not a guess.
                setProjects((items) =>
                  items.map((item) => (item.id === renamed.id ? renamed : item)),
                );
                setRenameName("");
                setRenameKey(crypto.randomUUID());
                renameField.current?.focus();
              } catch (error) {
                // A stale revision or an unknown reply keeps the key for an explicit replay.
                setRenameProblem(problemOf(error));
              } finally {
                setBusy(false);
              }
            })();
          }}
        >
          <label>
            新项目名称
            <input
              ref={renameField}
              value={renameName}
              onChange={(event) => setRenameName(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy || renameName === ""}>
            重命名项目
          </button>
        </form>
      )}
      {renameProblem !== null && (
        <p role="alert" aria-label="项目错误">
          {problems[renameProblem]}
        </p>
      )}
      <nav aria-label="项目">
        {projects.map((project) => (
          <button
            key={project.id}
            type="button"
            aria-pressed={project.id === projectId}
            onClick={() => {
              setProjectId(project.id);
              setBook(null);
              setListed([]);
              setChapters([]);
            }}
          >
            {project.name}
          </button>
        ))}
      </nav>
      {projectId !== null && (
        <div className="books-panel">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setBusy(true);
              void (async () => {
                try {
                  const created = await session.createBook(projectId, { title }, bookKey);
                  setBookKey(crypto.randomUUID());
                  remember(created);
                  setTitle("");
                  setListed((current) => [
                    ...current.filter((item) => item.id !== created.id),
                    created,
                  ]);
                } catch (error) {
                  setProblem(problemOf(error));
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            <label>
              新有声书标题
              <input value={title} onChange={(event) => setTitle(event.target.value)} required />
            </label>
            <button type="submit" disabled={busy || title === ""}>
              创建有声书
            </button>
          </form>
          {listed.length === 0 ? (
            <p className="books-empty">该项目还没有有声书，先创建一本。</p>
          ) : (
            <ul className="books-list" aria-label="书目列表">
              {listed.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    aria-pressed={item.id === book?.id}
                    disabled={busy}
                    onClick={() => {
                      void open(item.id);
                    }}
                  >
                    {item.title}
                    <span className="books-status">
                      {item.id === book?.id ? "已打开" : item.status}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {book !== null && (
        <>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setBusy(true);
              setChapterProblem(null);
              void (async () => {
                try {
                  const created = await session.createChapter(
                    book.id,
                    { title: chapterTitle, ordinal: Number(chapterOrdinal), text: chapterText },
                    chapterKey,
                  );
                  setChapters((current) => [
                    ...current.filter((item) => item.id !== created.id),
                    created,
                  ]);
                  setChapterTitle("");
                  setChapterOrdinal("");
                  setChapterText("");
                  // A new chapter is a new intent: the replayed key has been consumed.
                  setChapterKey(crypto.randomUUID());
                  setChapter(created);
                } catch (error) {
                  // The key stays as it is: the reply was unknown or rejected, not consumed.
                  setChapterProblem(problemOf(error));
                } finally {
                  setBusy(false);
                }
              })();
            }}
          >
            <label>
              新章节标题
              <input
                value={chapterTitle}
                onChange={(event) => setChapterTitle(event.target.value)}
                required
              />
            </label>
            <label>
              新章节序号
              <input
                type="number"
                min={0}
                value={chapterOrdinal}
                onChange={(event) => setChapterOrdinal(event.target.value)}
                required
              />
            </label>
            <label>
              新章节正文
              <textarea
                value={chapterText}
                onChange={(event) => setChapterText(event.target.value)}
                required
              />
            </label>
            <button
              type="submit"
              disabled={busy || chapterTitle === "" || chapterOrdinal === "" || chapterText === ""}
            >
              新增章节
            </button>
          </form>
          {chapterProblem !== null && (
            <p role="alert" aria-label="章节错误">
              {problems[chapterProblem]}
            </p>
          )}
          {chapters.length === 0 ? (
            <p className="books-empty">这本书还没有章节。</p>
          ) : (
            <ul className="books-chapters" aria-label="章节列表">
              {[...chapters]
                .sort((left, right) => left.ordinal - right.ordinal)
                .map((item) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      aria-pressed={item.id === chapter?.id}
                      disabled={busy}
                      onClick={() => setChapter(item)}
                    >
                      第 {item.ordinal + 1} 章 · {item.title}
                      <span className="books-status">正文修订 {item.text_revision}</span>
                    </button>
                  </li>
                ))}
            </ul>
          )}
          {chapter !== null && <RenderUnitPanel client={session} chapter={chapter} />}
          <BookExportPanel client={session} book={book} />
        </>
      )}
    </section>
  );
}

export default BooksHost;
