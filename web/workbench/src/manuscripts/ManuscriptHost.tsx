import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { EditorJournal, type EditorStorage } from "./editor-journal";
import { EditorSession, type EditorProblem } from "./editor-session";
import { WorkbenchClient } from "./workbench-client";
import "./manuscripts.css";

const problems: Record<EditorProblem, string> = {
  unavailable: "当前部署尚未启用工作台，已有本地草稿仍保留。",
  read_failed: "读取未完成。已有草稿仍保留，请检查连接后重新读取。",
  unsaved: "请先保存当前草稿，或明确放弃本地修改后再切换。",
  unknown: "提交结果尚未确认。已保留原请求，请按原键重放，不要重新创建。",
  invalid: "输入不符合要求。请检查名称、正文和版本后再提交。",
  conflict: "服务端已有新版本。草稿仍保留，请比较后明确选择是否继续。",
  missing: "项目或稿件已不可读取。已保留本地草稿，请先复制需要的内容。",
  storage: "本地恢复信息未能保存。已暂停提交，内存中的草稿仍可复制；请修复存储后重试。",
  rejected: "本次请求已被拒绝。已保留输入，请核对后再操作。",
};

function initialize(client?: WorkbenchClient, storage?: EditorStorage) {
  try {
    return new EditorSession(
      client ?? new WorkbenchClient(),
      new EditorJournal(storage ?? window.sessionStorage),
    );
  } catch {
    return null;
  }
}

function ActiveEditor({ session }: { session: EditorSession }) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const { record } = state;
  const locked = state.busy || record.pending !== null;
  const writeDisabled = locked || state.blocked || !state.status?.workbench_enabled;
  const [history, setHistory] = useState<{ viewKey: string; value: string } | null>(null);
  const viewKey = `${record.view?.transcript.id ?? ""}:${record.view?.revision.revision ?? ""}`;
  const historyValue =
    history?.viewKey === viewKey ? history.value : (record.view?.revision.revision ?? "");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const discardLauncher = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);
  const cancelDiscard = useRef<HTMLButtonElement>(null);
  const mounted = useRef({ generation: 0 });
  useEffect(() => {
    const owner = mounted.current;
    const generation = ++owner.generation;
    void session.start();
    return () => {
      queueMicrotask(() => {
        if (owner.generation === generation) session.dispose();
      });
    };
  }, [session]);
  useEffect(() => {
    heading.current?.focus();
  }, [record.view?.transcript.id, record.view?.revision.revision]);
  useEffect(() => {
    if (confirmDiscard) cancelDiscard.current?.focus();
    else if (wasConfirming.current) (discardLauncher.current ?? title.current)?.focus();
    wasConfirming.current = confirmDiscard;
  }, [confirmDiscard]);

  return (
    <section className="manuscripts" aria-label="项目与稿件工作台" aria-busy={state.busy}>
      <header className="manuscripts-toolbar">
        <div>
          <h2 ref={title} tabIndex={-1}>
            项目与稿件
          </h2>
          <p>先保存原稿，再追加校对修订。原稿和历史版本始终保留。</p>
        </div>
        <button
          type="button"
          disabled={locked}
          onClick={() => {
            void session.discover();
          }}
        >
          刷新项目
        </button>
      </header>
      {state.busy && <p role="status">正在读取或提交，请稍候…</p>}
      {state.problem && <p role="alert">{problems[state.problem]}</p>}
      {state.saved && (
        <p role="status">
          {record.view && record.view.revision.revision !== "1"
            ? "新修订已保存至当前部署。"
            : record.view
              ? "原稿已保存至当前部署。"
              : "项目已保存至当前部署。"}
        </p>
      )}
      {record.pending && (
        <div className="manuscripts-recovery">
          <p>原请求已保存在当前标签页。重放沿用相同内容和原键，不会自动发送。</p>
          <button
            type="button"
            disabled={state.busy || state.blocked}
            onClick={() => {
              void session.replay();
            }}
          >
            按原键重放
          </button>
        </div>
      )}
      {state.blocked && (
        <button type="button" disabled={state.busy} onClick={() => session.retryStorage()}>
          重试本地保存
        </button>
      )}
      <div className="manuscripts-grid">
        <aside className="manuscripts-directory" aria-label="项目和稿件目录">
          <h3>共享项目</h3>
          {state.projects?.data.length === 0 && <p>当前部署还没有项目。</p>}
          <ul aria-label="项目列表">
            {state.projects?.data.map((project) => (
              <li key={project.id}>
                <button
                  type="button"
                  disabled={locked || state.blocked}
                  aria-pressed={record.project?.id === project.id}
                  onClick={() => {
                    void session.selectProject(project.id);
                  }}
                >
                  {project.name}
                </button>
              </li>
            ))}
          </ul>
          {state.projects?.has_more && (
            <button
              type="button"
              disabled={locked}
              onClick={() => {
                void session.moreProjects();
              }}
            >
              更多项目
            </button>
          )}
          <form
            aria-label="创建项目"
            onSubmit={(event) => {
              event.preventDefault();
              void session.createProject();
            }}
          >
            <label>
              项目名称
              <input
                value={record.forms.projectName}
                disabled={locked}
                onChange={(event) => session.form("projectName", event.currentTarget.value)}
              />
            </label>
            <button type="submit" disabled={writeDisabled}>
              创建项目
            </button>
          </form>
          {record.project && (
            <section aria-label="当前项目稿件">
              <h3>{record.project.name}的稿件</h3>
              <button
                type="button"
                disabled={locked || state.blocked}
                onClick={() => {
                  void session.checkCurrent();
                }}
              >
                核对当前版本
              </button>
              {state.transcripts === null && <p>请核对当前版本以读取稿件目录。</p>}
              {state.transcripts?.data.length === 0 && (
                <p>这个项目还没有稿件，可以先保存一份原稿。</p>
              )}
              <ul aria-label="稿件列表">
                {state.transcripts?.data.map((transcript) => (
                  <li key={transcript.id}>
                    <button
                      type="button"
                      disabled={locked || state.blocked}
                      aria-pressed={record.view?.transcript.id === transcript.id}
                      onClick={() => {
                        void session.selectTranscript(transcript.id);
                      }}
                    >
                      {transcript.display_name}
                    </button>
                  </li>
                ))}
              </ul>
              {state.transcripts?.has_more && (
                <button
                  type="button"
                  disabled={locked}
                  onClick={() => {
                    void session.moreTranscripts();
                  }}
                >
                  更多稿件
                </button>
              )}
            </section>
          )}
        </aside>
        <div className="manuscripts-document">
          {!record.project && <p>选择一个共享项目，或创建项目后保存原稿。</p>}
          {record.project && (
            <details className="manuscripts-create" open={record.view === null}>
              <summary>新建稿件</summary>
              <form
                aria-label="创建稿件"
                onSubmit={(event) => {
                  event.preventDefault();
                  void session.createTranscript();
                }}
              >
                <label>
                  稿件名称
                  <input
                    value={record.forms.transcriptName}
                    disabled={locked}
                    onChange={(event) => session.form("transcriptName", event.currentTarget.value)}
                  />
                </label>
                <label>
                  原稿正文
                  <textarea
                    rows={7}
                    value={record.forms.newText}
                    disabled={locked}
                    onChange={(event) => session.form("newText", event.currentTarget.value)}
                  />
                </label>
                <p>原稿保存后成为初始修订。后续修改会生成新修订，不覆盖原文。</p>
                <button type="submit" disabled={writeDisabled}>
                  保存原稿
                </button>
              </form>
            </details>
          )}
          {record.view && record.draft && (
            <section aria-label="稿件编辑">
              <h3 ref={heading} tabIndex={-1}>
                {record.view.transcript.display_name}
              </h3>
              <p className="manuscripts-version">
                编辑来源版本 {record.draft.sourceRevision}，保存基线版本 {record.draft.baseRevision}
                。
              </p>
              {record.view.revision.revision !== record.view.transcript.head_revision && (
                <p>正在查看历史版本。保存会追加新修订，保留所选来源。</p>
              )}
              {!state.verified && !record.pending && <p>尚未核对服务端当前版本，保存已暂停。</p>}
              <details>
                <summary>查看固定版本原文</summary>
                <pre aria-label="固定版本原文">{record.view.revision.text}</pre>
              </details>
              <label>
                校对草稿
                <textarea
                  rows={14}
                  value={record.draft.text}
                  disabled={locked}
                  onChange={(event) => session.edit(event.currentTarget.value)}
                />
              </label>
              <p>本地草稿仅在当前标签页保留，关闭标签页后可能丢失；点击保存才提交到共享部署。</p>
              <button
                type="button"
                disabled={writeDisabled || !state.verified || state.conflict !== null}
                onClick={() => {
                  void session.save();
                }}
              >
                保存新修订
              </button>
              <form
                className="manuscripts-history"
                aria-label="读取历史版本"
                onSubmit={(event) => {
                  event.preventDefault();
                  void session.selectTranscript(record.view!.transcript.id, historyValue);
                }}
              >
                <label>
                  历史版本号
                  <input
                    inputMode="numeric"
                    value={historyValue}
                    disabled={locked}
                    onChange={(event) => setHistory({ viewKey, value: event.currentTarget.value })}
                  />
                </label>
                <button type="submit" disabled={locked || state.blocked}>
                  读取固定版本
                </button>
              </form>
            </section>
          )}
          {state.conflict && (
            <section className="manuscripts-conflict" aria-label="版本冲突比较">
              <h3>服务端最新版本 {state.conflict.transcript.head_revision}</h3>
              <pre aria-label="服务端最新版本">{state.conflict.revision.text}</pre>
              <p>
                请先比较并调整上方草稿。继续会采用最新保存基线，保留草稿内容与原来源版本；不会自动合并或保存。
              </p>
              <button
                type="button"
                disabled={locked || state.blocked}
                onClick={() => session.rebase()}
              >
                保留草稿并基于最新版本继续
              </button>
            </section>
          )}
          {(record.view || record.forms.newText || record.forms.transcriptName) &&
            !record.pending && (
              <div className="manuscripts-discard">
                {!confirmDiscard ? (
                  <button
                    ref={discardLauncher}
                    type="button"
                    disabled={locked || state.blocked}
                    onClick={() => setConfirmDiscard(true)}
                  >
                    放弃本地修改
                  </button>
                ) : (
                  <div role="group" aria-label="放弃草稿确认">
                    <p>将放弃当前本地草稿与未提交输入，服务端已保存的版本不受影响。</p>
                    <button
                      ref={cancelDiscard}
                      type="button"
                      onClick={() => setConfirmDiscard(false)}
                    >
                      继续编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        session.discard();
                        setConfirmDiscard(false);
                      }}
                    >
                      确认放弃本地修改
                    </button>
                  </div>
                )}
              </div>
            )}
        </div>
      </div>
    </section>
  );
}

export function ManuscriptHost({
  client,
  storage,
}: {
  client?: WorkbenchClient;
  storage?: EditorStorage;
}) {
  const [session] = useState(() => initialize(client, storage));
  if (!session)
    return (
      <section className="manuscripts" aria-label="稿件恢复">
        <p role="alert">恢复信息不可用。已暂停提交，原记录未清除；请修复浏览器存储后重新载入。</p>
      </section>
    );
  return <ActiveEditor session={session} />;
}
export default ManuscriptHost;
