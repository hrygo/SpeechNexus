import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { WorkbenchClient, type Contracts } from "../manuscripts/workbench-client";
import { FollowSession, restoreCheckpoint } from "./follow-session";
import { TeleprompterStateSession, type TeleprompterProblem } from "./teleprompter-state";
import "./teleprompter.css";

const SERVER_PROBLEMS: Record<TeleprompterProblem, string> = {
  missing: "该部署中此项目尚无已保存的提词状态（404）：这不代表位置为 0，也不是空状态。",
  conflict:
    "服务端保存的脚本修订比本次提交更新，写入被拒绝（409）。请先读取当前状态；本页不会自动重试。",
  rejected: "请求被拒绝（4xx）。请核对修订、位置与请求键后重试。",
  unavailable: "部署或连接不可用，写入结果未确认。请按原请求键重放，不要新建一次写入。",
  unknown: "结果尚未确认。请核对连接后按原请求键重试。",
};

const STORAGE_KEY = "speechnexus.workbench.prompter.v1";
type Snapshot = ReturnType<FollowSession["snapshot"]>;

function revealChunk(chunk: HTMLSpanElement | null) {
  const reader = chunk?.parentElement;
  if (!reader || !chunk) return;
  const viewport = reader.getBoundingClientRect(),
    current = chunk.getBoundingClientRect();
  const top = viewport.top + reader.clientTop,
    bottom = top + reader.clientHeight;
  // Scroll only the reading viewport; keep editing controls and keyboard focus in place.
  if (current.top < top || current.height > reader.clientHeight)
    reader.scrollTop += Math.floor(current.top - top);
  else if (current.bottom > bottom) reader.scrollTop += Math.ceil(current.bottom - bottom);
}

export function TeleprompterPanel({ client }: { client?: WorkbenchClient } = {}) {
  const draftId = useId();
  const session = useRef<FollowSession | null>(null);
  const currentChunk = useRef<HTMLSpanElement>(null);
  const [draft, setDraft] = useState("");
  const [state, setState] = useState<Snapshot | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const body = state?.script.text ?? "";
  const length = Array.from(body).length;
  const chunks = useMemo(() => {
    const result: { text: string; start: number; end: number }[] = [];
    let buffer = "",
      start = 0,
      end = 0;
    for (const part of new Intl.Segmenter("und", { granularity: "grapheme" }).segment(body)) {
      buffer += part.segment;
      end += Array.from(part.segment).length;
      if (end - start >= 80 || part.segment.includes("\n")) {
        result.push({ text: buffer, start, end });
        buffer = "";
        start = end;
      }
    }
    if (buffer) result.push({ text: buffer, start, end });
    return result;
  }, [body]);
  const selected = Math.max(
    0,
    chunks.findIndex((chunk) => chunk.end > (state?.position ?? 0)),
  );
  const selectedIndex = state?.position === length ? chunks.length - 1 : selected;
  useEffect(() => {
    revealChunk(currentChunk.current);
  }, [state?.position, state?.script]);
  useEffect(() => {
    const reader = currentChunk.current?.parentElement;
    if (!reader || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => revealChunk(currentChunk.current));
    observer.observe(reader);
    return () => observer.disconnect();
  }, [state?.script]);
  useEffect(() => {
    const holder = session;
    const pause = () => {
      if (document.hidden) holder.current?.pause();
    };
    const leave = () => holder.current?.pause();
    document.addEventListener("visibilitychange", pause);
    window.addEventListener("pagehide", leave);
    return () => {
      document.removeEventListener("visibilitychange", pause);
      window.removeEventListener("pagehide", leave);
      holder.current?.pause();
    };
  }, []);
  function useDraft() {
    try {
      const next = new FollowSession({
        id: state?.script.id ?? crypto.randomUUID(),
        revision: (state?.script.revision ?? 0) + 1,
        text: draft,
      });
      session.current?.pause();
      session.current = next;
      setState(next.snapshot());
      setError("");
      setMessage("已使用此稿，尚未保存。");
    } catch {
      setError("稿件无效：请输入非空正文，最多 20000 个字符，且不含无效字符。");
    }
  }
  function move(position: number) {
    if (!session.current) return;
    try {
      session.current.seek(Math.max(0, Math.min(length, position)));
      setState(session.current.snapshot());
      setError("");
      setMessage("位置已更改，尚未保存。");
    } catch {
      setError("阅读位置无效。");
    }
  }
  function save() {
    if (!session.current) return;
    try {
      localStorage.setItem(STORAGE_KEY, session.current.checkpoint());
      setError("");
      setMessage("已保存到此浏览器。编辑框中未应用的修改未保存。");
    } catch {
      setError("保存失败：浏览器存储不可用或空间不足。当前稿件仍可阅读。");
    }
  }
  const [workbench] = useState(() => client ?? new WorkbenchClient());
  const server = useMemo(() => new TeleprompterStateSession(workbench), [workbench]);
  const serverState = useSyncExternalStore(server.subscribe, server.getSnapshot);
  const [projects, setProjects] = useState<readonly Contracts["Project"][]>([]);
  const [projectId, setProjectId] = useState("");
  const [revision, setRevision] = useState("1");
  const [position, setPosition] = useState("0");
  const [hold, setHold] = useState(false);
  const [serverKey, setServerKey] = useState("");
  const [serverMessage, setServerMessage] = useState("");
  useEffect(() => () => server.dispose(), [server]);

  async function readProjects() {
    try {
      const page = await workbench.projects();
      setProjects(page.data);
      setServerMessage(page.data.length === 0 ? "此部署尚无项目。" : "已读取部署内项目。");
    } catch {
      setProjects([]);
      setServerMessage("读取项目失败：部署或连接不可用。");
    }
  }
  function applyLoadedState(value: Contracts["TeleprompterState"]) {
    setRevision(value.script_revision);
    setPosition(String(value.visible_position));
    setHold(value.manual_hold);
  }

  function restore() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw === null) {
        setError("此浏览器尚无已保存稿件。");
        return;
      }
      const next = restoreCheckpoint(raw);
      session.current?.pause();
      session.current = next;
      setState(next.snapshot());
      setDraft(next.snapshot().script.text);
      setError("");
      setMessage("已恢复为手动阅读，不启动语音跟随。");
    } catch {
      setError("恢复失败：存储不可用或稿件记录无效。当前稿件未改变。");
    }
  }

  return (
    <section className="file-task prompter" aria-label="提词阅读">
      <h2>手动提词阅读</h2>
      <p>粘贴稿件后手动阅读。自动语音跟随尚未接入，不会请求麦克风。</p>
      <div className="file-task-grid">
        <div className="file-task-input">
          <div className="file-task-field">
            <label htmlFor={draftId}>阅读稿</label>
            <textarea
              id={draftId}
              value={draft}
              maxLength={40000}
              rows={8}
              onChange={(event) => setDraft(event.target.value)}
            />
          </div>
          <div className="file-task-actions">
            <button type="button" onClick={useDraft}>
              使用此稿
            </button>
            <button type="button" onClick={restore}>
              恢复并替换当前稿
            </button>
          </div>
          <p>“使用此稿”固定当前阅读版本。输入框内的修改需再次使用才生效。</p>
          <p>
            保存会将固定稿及位置写入此浏览器的同源存储；不保存到服务端，也不按使用者隔离。离开或刷新会清空未保存内容。
          </p>
          <button type="button" disabled={!state} onClick={save}>
            保存到此浏览器
          </button>
          {error && (
            <p role="alert" aria-label="本地提词错误">
              {error}
            </p>
          )}
          <p role="status" aria-label="本地提词状态">
            {message || "输入阅读稿后开始。"}
          </p>
        </div>
        <div className="file-task-detail">
          {state ? (
            <>
              <p>本地稿第 {state.script.revision} 版</p>
              <label className="file-task-field">
                阅读位置
                <input
                  type="range"
                  min={0}
                  max={length}
                  step={1}
                  value={state.position}
                  onChange={(event) => move(Number(event.target.value))}
                />
              </label>
              <p>
                {state.position} / {length} 字符。阅读区支持方向键、PageUp / PageDown、Home / End。
              </p>
              <div className="file-task-actions">
                <button type="button" onClick={() => move(state.position - 80)}>
                  上一段
                </button>
                <button type="button" onClick={() => move(state.position + 80)}>
                  下一段
                </button>
                <button type="button" onClick={() => move(0)}>
                  回到起点
                </button>
              </div>
              <div
                className="prompter-reading"
                role="region"
                aria-label="固定阅读稿"
                tabIndex={0}
                onKeyDown={(event) => {
                  const positions: Record<string, number> = {
                    ArrowDown: state.position + 20,
                    ArrowUp: state.position - 20,
                    PageDown: state.position + 80,
                    PageUp: state.position - 80,
                    Home: 0,
                    End: length,
                  };
                  const position = positions[event.key];
                  if (
                    typeof position === "number" &&
                    !event.ctrlKey &&
                    !event.metaKey &&
                    !event.altKey
                  ) {
                    event.preventDefault();
                    move(position);
                  }
                }}
              >
                {chunks.map((chunk, index) => (
                  <span
                    key={chunk.start}
                    ref={index === selectedIndex ? currentChunk : undefined}
                    data-current={index === selectedIndex ? "true" : undefined}
                  >
                    {chunk.text}
                  </span>
                ))}
              </div>
            </>
          ) : (
            <p>当前没有固定稿件。使用输入稿，或显式恢复已保存稿。</p>
          )}
        </div>
      </div>
      <div className="file-task-detail" aria-label="部署内提词状态">
        <h3>部署内提词状态</h3>
        <p>
          部署只保存脚本修订、可见位置与手动接管，不保存稿件正文；正文由稿件本身承载，此处不还原文本。
        </p>
        <div className="file-task-actions">
          <button type="button" onClick={() => void readProjects()}>
            读取部署项目
          </button>
        </div>
        <label className="file-task-field">
          项目
          <select
            value={projectId}
            onChange={(event) => {
              setProjectId(event.currentTarget.value);
              setServerMessage("尚未读取该项目状态。");
            }}
          >
            <option value="">请选择</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        <div className="file-task-actions">
          <button
            type="button"
            disabled={projectId === "" || serverState.busy}
            onClick={() => {
              void server.load(projectId).then(() => {
                const loaded = server.getSnapshot();
                if (loaded.state !== null) applyLoadedState(loaded.state);
              });
            }}
          >
            读取部署状态
          </button>
        </div>
        <label className="file-task-field">
          脚本修订
          <input
            inputMode="numeric"
            value={revision}
            onChange={(event) => setRevision(event.currentTarget.value)}
          />
        </label>
        <label className="file-task-field">
          可见位置
          <input
            inputMode="numeric"
            value={position}
            onChange={(event) => setPosition(event.currentTarget.value)}
          />
        </label>
        <label className="file-task-field">
          <input
            type="checkbox"
            checked={hold}
            onChange={(event) => setHold(event.currentTarget.checked)}
          />
          手动接管
        </label>
        <label className="file-task-field">
          请求键（8 位以上，重放沿用同一值）
          <input value={serverKey} onChange={(event) => setServerKey(event.currentTarget.value)} />
        </label>
        <button
          type="button"
          disabled={
            projectId === "" ||
            serverState.busy ||
            !/^[1-9][0-9]*$/.test(revision) ||
            !/^[0-9]+$/.test(position) ||
            Array.from(serverKey.trim()).length < 8
          }
          onClick={() => {
            void server
              .save(
                projectId,
                {
                  script_revision: revision,
                  visible_position: Number(position),
                  manual_hold: hold,
                },
                serverKey.trim(),
              )
              .then(() => {
                const result = server.getSnapshot();
                if (result.accepted) {
                  setServerKey("");
                  applyLoadedState(result.state as Contracts["TeleprompterState"]);
                }
              });
          }}
        >
          保存到当前部署
        </button>
        {serverState.state !== null && (
          <p role="status" aria-label="部署提词快照">
            部署内已保存：脚本修订 {serverState.state.script_revision}，位置{" "}
            {serverState.state.visible_position}，手动接管{" "}
            {serverState.state.manual_hold ? "是" : "否"}（更新于 {serverState.state.updated_at}）。
          </p>
        )}
        {serverState.problem !== null && (
          <p role="alert" aria-label="部署提词错误">
            {SERVER_PROBLEMS[serverState.problem]}
          </p>
        )}
        <p role="status" aria-label="部署提词状态">
          {serverMessage || "读取项目后选择目标。"}
        </p>
      </div>
    </section>
  );
}
