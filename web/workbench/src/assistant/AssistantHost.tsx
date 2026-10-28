import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { WorkbenchClient, type Contracts } from "../manuscripts/workbench-client";
import { AssistantSession } from "./assistant-session";
import { assistantProblems as problems, availabilityCopy, designState } from "./copy";
import { catalogueCopy, catalogueRows } from "../voices/catalogue-state";
import "./assistant.css";

export function AssistantHost({
  client,
  active = true,
}: {
  client?: WorkbenchClient;
  active?: boolean;
}) {
  const [workbench] = useState(() => client ?? new WorkbenchClient());
  const session = useMemo(() => new AssistantSession(workbench), [workbench]);
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [projects, setProjects] = useState<readonly Contracts["Project"][]>([]);
  const initial = session.getSnapshot().pending;
  const [projectId, setProjectId] = useState(initial?.kind === "create" ? initial.projectId : "");
  const [voiceKey, setVoiceKey] = useState(
    initial?.kind === "create" ? `${initial.body.voice.id}@${initial.body.voice.revision}` : "",
  );
  const [inputText, setInputText] = useState(
    initial?.kind === "create" ? initial.body.input_text : "",
  );
  const [key, setKey] = useState(initial?.key ?? "");
  const heading = useRef<HTMLHeadingElement>(null);
  const owner = useRef({ generation: 0 });
  useEffect(() => {
    const currentOwner = owner.current;
    const generation = ++currentOwner.generation;
    return () => {
      queueMicrotask(() => {
        if (currentOwner.generation === generation) session.dispose();
      });
    };
  }, [session]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void session.load(controller.signal);
    void workbench
      .projects(undefined, controller.signal)
      .then((page) => setProjects(page.data))
      .catch(() => setProjects([]));
    return () => controller.abort();
  }, [active, session, workbench]);

  const locked = state.busy || state.watching || state.pending !== null || state.blocked;
  const capability = designState(state.availability);
  const turn = state.turn;
  const requestKey = key.trim().length >= 8 ? key.trim() : "";
  const voices = catalogueRows(state.catalogue);
  const selected = voices.find((voice) => `${voice.voice_id}@${voice.revision}` === voiceKey);
  const submitDisabled =
    locked ||
    state.availability !== "available" ||
    projectId === "" ||
    selected === undefined ||
    inputText.trim().length === 0 ||
    requestKey === "";

  return (
    <section className="assistant" aria-label="语音助手工作台" aria-busy={state.busy}>
      <h2 ref={heading} tabIndex={-1}>
        语音助手
      </h2>
      <p className="assistant-capability">
        部署能力：
        <span data-state={capability}>{state.availability}</span>{" "}
        {availabilityCopy[state.availability]}
      </p>
      <p>
        工作台负责业务回合与历史，语音底座负责合成；文本由服务端配置的内网模型生成，浏览器不能选择端点或模型。
      </p>
      {state.problem !== null && <p role="alert">{problems[state.problem]}</p>}

      {state.pending && (
        <aside aria-label="助手原请求恢复">
          <p>原请求已保存在此标签页。结果未确认前不能另建请求，也不会自动重放。</p>
          {state.pending.kind === "create" && (
            <p>
              项目 {state.pending.projectId} · 请求键 {state.pending.key}
            </p>
          )}
          <button type="button" disabled={state.busy} onClick={() => void session.replay()}>
            按原键重放助手请求
          </button>
          {state.rejected && (
            <button type="button" onClick={() => session.dismissRejected()}>
              结束已拒绝助手请求
            </button>
          )}
        </aside>
      )}
      {state.availability === "available" && (
        <form
          aria-label="创建助手回合"
          onSubmit={(event) => {
            event.preventDefault();
            if (submitDisabled || selected === undefined) return;
            void session
              .start(
                projectId,
                {
                  input_text: inputText,
                  voice: { id: selected.voice_id, revision: selected.revision },
                },
                requestKey,
              )
              .then(() => {
                if (
                  session.getSnapshot().pending === null &&
                  !session.getSnapshot().blocked &&
                  session.getSnapshot().problem === null
                ) {
                  setKey("");
                  setInputText("");
                }
              });
          }}
        >
          <label>
            项目
            <select
              disabled={locked}
              value={projectId}
              onChange={(event) => setProjectId(event.currentTarget.value)}
            >
              <option value="">请选择</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            已发布音色
            <select
              disabled={locked}
              value={voiceKey}
              onChange={(event) => setVoiceKey(event.currentTarget.value)}
            >
              <option value="">请选择</option>
              {voices.map((voice) => (
                <option
                  key={`${voice.voice_id}@${voice.revision}`}
                  value={`${voice.voice_id}@${voice.revision}`}
                >
                  {voice.display_name} · v{voice.revision}
                </option>
              ))}
            </select>
          </label>
          {state.catalogue.kind === "loading" && <p aria-busy="true">{catalogueCopy.loading}</p>}
          {state.catalogue.kind === "ready" && voices.length === 0 && <p>{catalogueCopy.empty}</p>}
          {state.catalogue.kind === "problem" && (
            <aside aria-label="音色目录不可用">
              <p role="alert">{catalogueCopy[state.catalogue.problem]}</p>
              <button
                type="button"
                disabled={state.busy}
                onClick={() => void session.readCatalogue()}
              >
                {catalogueCopy.retry}
              </button>
            </aside>
          )}
          <label>
            问题
            <textarea
              disabled={locked}
              value={inputText}
              rows={4}
              maxLength={20000}
              onChange={(event) => setInputText(event.currentTarget.value)}
            />
          </label>
          <label>
            请求键（8 位以上，重放沿用同一值）
            <input
              disabled={locked}
              value={key}
              onChange={(event) => setKey(event.currentTarget.value)}
            />
          </label>
          <button type="submit" disabled={submitDisabled}>
            提交回合
          </button>
        </form>
      )}

      {turn !== null && (
        <section aria-label="当前回合">
          <h3>当前回合 {turn.id}</h3>
          <p role="status">
            状态：{turn.status}
            {state.watching ? "（等待中）" : ""}
          </p>
          {turn.output_text !== null && <p aria-live="polite">{turn.output_text}</p>}
          {turn.status === "failed" && turn.error != null && (
            <p role="alert">
              生成失败：{turn.error.code}
              {turn.error.retryable ? "（可重试）" : "（不可重试）"}
            </p>
          )}
          {!locked && !["succeeded", "failed", "cancelled"].includes(turn.status) && (
            <button
              type="button"
              onClick={() => {
                const cancelKey = `${turn.id}-cancel-${turn.playback_generation}`;
                void session.cancel(cancelKey).then(() => heading.current?.focus());
              }}
            >
              取消回合
            </button>
          )}
        </section>
      )}
      {state.problem === "unknown" && state.turn !== null && state.turn.error !== null && (
        <p>服务端已给出错误码，请按该错误处理；本界面不会自动重试。</p>
      )}
    </section>
  );
}

export default AssistantHost;
