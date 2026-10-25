import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { WorkbenchClient } from "../manuscripts/workbench-client";
import { MeetingJournal, type MeetingStorage } from "./meeting-journal";
import { MeetingSession, type MeetingProblem } from "./meeting-session";
import "./meetings.css";

const problems: Record<MeetingProblem, string> = {
  unavailable: "当前部署尚未启用工作台，已有草稿仍保留。",
  read_failed: "读取未完成。请检查连接后刷新，已有草稿仍保留。",
  unsaved: "请先封存当前校对稿，或明确放弃本地修改后再切换。",
  unknown: "提交结果尚未确认。已保留原请求，请按原键重放，不要重新创建。",
  invalid: "输入不符合要求。请检查会议名称、正文或编号。",
  conflict: "服务端已有不同的来源版本。请比较后明确采用最新基线，再封存校对稿。",
  missing: "会议、项目或稿件已不可读取。请先复制需要保留的本地内容。",
  storage: "本地恢复信息未能保存。已暂停提交，草稿仍可复制；请修复存储后重试。",
  rejected: "本次请求已被明确拒绝。请先结束该请求，再刷新并核对来源。",
  profile: "当前部署尚未配置可用的纪要模板；会议记录与校对封存仍可使用。",
};
const statuses = {
  capturing: "尚未封存",
  sealed: "已封存",
  minutes_pending: "已封存 · 纪要状态请见下方",
  minutes_ready: "已封存 · 纪要已生成",
  failed: "记录状态异常",
};
const jobStatuses = {
  queued: "已排队，等待执行",
  running: "正在生成",
  succeeded: "生成完成",
  failed: "生成失败",
  expired: "来源已变化，结果已过期",
  cancelled: "已取消",
};
function initialize(client?: WorkbenchClient, storage?: MeetingStorage) {
  try {
    return new MeetingSession(
      client ?? new WorkbenchClient(),
      new MeetingJournal(storage ?? window.sessionStorage),
    );
  } catch {
    return null;
  }
}
function ActiveMeeting({ session, active }: { session: MeetingSession; active: boolean }) {
  const s = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const r = s.record;
  const locked = s.busy || r.pending !== null;
  const writeDisabled = locked || s.blocked || !s.status?.workbench_enabled || !s.verified;
  const heading = useRef<HTMLHeadingElement>(null);
  const mounted = useRef({ generation: 0 });
  const [discard, setDiscard] = useState(false);
  const cancelDiscard = useRef<HTMLButtonElement>(null);
  const discardLauncher = useRef<HTMLButtonElement>(null);
  const wasDiscard = useRef(false);
  const wasActive = useRef(active);
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
    if (active && !wasActive.current) {
      void session.discover().then(() => session.refresh());
      heading.current?.focus();
    }
    wasActive.current = active;
  }, [active, session]);
  useEffect(() => {
    heading.current?.focus();
  }, [r.view?.transcript.id, r.meeting?.id]);
  useEffect(() => {
    if (discard) cancelDiscard.current?.focus();
    else if (wasDiscard.current) discardLauncher.current?.focus();
    wasDiscard.current = discard;
  }, [discard]);
  const profile = s.status?.llm_enabled ? s.status.minutes_profile : null;
  const minutesDisabled =
    writeDisabled ||
    !profile ||
    !r.meeting?.sealed_revision ||
    r.meeting.sealed_revision !== r.view?.revision.revision ||
    session.hasUnsaved() ||
    !!s.conflict ||
    !!(s.minutes && ["queued", "running"].includes(s.minutes.status));
  return (
    <section className="meetings" aria-label="会议记录工作台" aria-busy={s.busy}>
      <header className="meetings-toolbar">
        <div>
          <h2>会议记录</h2>
          <p>
            选择已保存的稿件，校对并封存会议正文，再按批准的模板生成纪要。实时采集与会议音频保存尚未接入。
          </p>
        </div>
        <button
          type="button"
          disabled={locked}
          onClick={() => {
            void session.discover();
          }}
        >
          刷新项目与配置
        </button>
      </header>
      {s.busy && <p role="status">正在读取或提交，请稍候…</p>}
      {s.problem && <p role="alert">{problems[s.problem]}</p>}
      {s.saved && <p role="status">请求已确认，已重新读取当前会议记录。</p>}
      {r.pending && (
        <div className="meetings-notice">
          <p>原请求已保存在当前标签页。恢复沿用相同内容与原键，不会自动提交。</p>
          <button
            type="button"
            disabled={s.busy || s.blocked}
            onClick={() => {
              void session.replay();
            }}
          >
            按原键重放
          </button>
          {s.problem === "rejected" && (
            <button
              type="button"
              disabled={s.busy || s.blocked}
              onClick={() => session.dismissRejected()}
            >
              结束已拒绝请求
            </button>
          )}
        </div>
      )}
      {s.blocked && (
        <button type="button" disabled={s.busy} onClick={() => session.retryStorage()}>
          重试保存恢复信息
        </button>
      )}
      <div className="meetings-grid">
        <aside className="meetings-directory" aria-label="会议来源">
          <h3>选择项目</h3>
          {s.projects?.data.length === 0 && <p>还没有项目。请先在“项目与稿件”保存项目和会议稿。</p>}
          <ul>
            {s.projects?.data.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  disabled={locked || s.blocked}
                  aria-pressed={r.project?.id === p.id}
                  onClick={() => {
                    void session.selectProject(p.id);
                  }}
                >
                  {p.name}
                </button>
              </li>
            ))}
          </ul>
          {s.projects?.has_more && (
            <button
              type="button"
              disabled={locked || s.blocked}
              onClick={() => {
                void session.moreProjects();
              }}
            >
              更多项目
            </button>
          )}
          {r.project && (
            <section>
              <h3>选择会议稿</h3>
              {s.transcripts?.data.length === 0 && <p>这个项目还没有稿件。</p>}
              <ul>
                {s.transcripts?.data.map((t) => (
                  <li key={t.id}>
                    <button
                      type="button"
                      disabled={locked || s.blocked}
                      aria-pressed={r.view?.transcript.id === t.id}
                      onClick={() => {
                        void session.selectTranscript(t.id);
                      }}
                    >
                      {t.display_name}
                    </button>
                  </li>
                ))}
              </ul>
              {s.transcripts?.has_more && (
                <button
                  type="button"
                  disabled={locked || s.blocked}
                  onClick={() => {
                    void session.moreTranscripts();
                  }}
                >
                  更多稿件
                </button>
              )}
            </section>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void session.open();
            }}
          >
            <h3>打开已有会议</h3>
            <label>
              打开会议编号
              <input
                value={r.forms.openId}
                disabled={locked}
                autoCapitalize="off"
                spellCheck={false}
                onChange={(e) => session.setForm("openId", e.target.value)}
              />
            </label>
            <button
              type="submit"
              disabled={locked || s.blocked || !s.status?.workbench_enabled || !r.forms.openId}
            >
              打开会议
            </button>
          </form>
          <p>
            仅恢复当前标签页的选择和草稿。关闭标签页可能丢失本地恢复信息，请保留已创建的会议编号。
          </p>
        </aside>
        <div className="meetings-document">
          {!r.view ? (
            <p>从左侧选择稿件，或输入已有会议编号。</p>
          ) : (
            <>
              <h3 ref={heading} tabIndex={-1}>
                {r.meeting?.display_name ?? r.view.transcript.display_name}
              </h3>
              <p>
                项目：{r.project?.name} · 稿件：{r.view.transcript.display_name} · 校对基线 v
                {r.draft?.baseRevision}
              </p>
              {r.meeting ? (
                <>
                  <label>
                    当前会议编号
                    <input readOnly value={r.meeting.id} />
                  </label>
                  <p>
                    记录状态：
                    {s.verified && !r.pending ? statuses[r.meeting.status] : "当前状态尚未确认"}
                    {r.meeting.sealed_revision && ` · 封存 v${r.meeting.sealed_revision}`}
                  </p>
                </>
              ) : (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void session.create();
                  }}
                >
                  <label>
                    会议名称
                    <input
                      value={r.forms.name}
                      disabled={locked}
                      onChange={(e) => session.setForm("name", e.target.value)}
                    />
                  </label>
                  <button type="submit" disabled={writeDisabled || !r.forms.name.trim()}>
                    创建会议记录
                  </button>
                </form>
              )}
              <label>
                会议校对正文
                <textarea
                  rows={14}
                  value={r.draft?.text ?? ""}
                  disabled={locked}
                  onChange={(e) => session.edit(e.target.value)}
                />
              </label>
              <p>校对保留精确文本。封存会追加不可变版本，已有版本与纪要记录保留。</p>
              <div className="meetings-actions">
                <button
                  type="button"
                  disabled={writeDisabled || !r.meeting || !!s.conflict}
                  onClick={() => {
                    void session.seal();
                  }}
                >
                  封存校对稿
                </button>
                <button
                  type="button"
                  disabled={locked || s.blocked}
                  onClick={() => {
                    void session.refresh();
                  }}
                >
                  刷新当前记录
                </button>
                <button
                  ref={discardLauncher}
                  type="button"
                  disabled={locked || !session.hasUnsaved()}
                  onClick={() => setDiscard(true)}
                >
                  放弃本地修改
                </button>
              </div>
              {discard && (
                <div role="group" aria-label="确认放弃本地修改">
                  <p>仅放弃当前标签页未封存的校对内容。</p>
                  <button ref={cancelDiscard} type="button" onClick={() => setDiscard(false)}>
                    保留修改
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      session.discardDraft();
                      setDiscard(false);
                    }}
                  >
                    确认放弃本地修改
                  </button>
                </div>
              )}
              {s.conflict && (
                <section className="meetings-notice" aria-label="比较来源版本">
                  <h4>最新稿件 v{s.conflict.revision.revision}</h4>
                  <pre>{s.conflict.revision.text}</pre>
                  <p>采用最新基线会保留你的校对文本，请比较后再追加封存。</p>
                  <button
                    type="button"
                    disabled={locked || s.blocked}
                    onClick={() => session.useLatest()}
                  >
                    保留校对并采用最新基线
                  </button>
                </section>
              )}
              <section className="meetings-minutes" aria-label="会议纪要">
                <h3>会议纪要</h3>
                {profile ? (
                  <p>
                    批准模板 v{profile.template_revision} · 模型 {profile.llm_model}
                    。生成依据已封存版本，尚未封存的校对不会用于纪要。
                  </p>
                ) : (
                  <p>当前部署尚未配置可用的纪要模板；会议记录与校对封存仍可使用。</p>
                )}
                <button
                  type="button"
                  disabled={minutesDisabled}
                  onClick={() => {
                    void session.createMinutes();
                  }}
                >
                  生成纪要
                </button>
                {s.verified && !r.pending && s.minutes && (
                  <>
                    <p role="status">
                      纪要：{jobStatuses[s.minutes.status]} · 来源 v{s.minutes.source_revision} ·
                      模板 v{s.minutes.template_revision}
                    </p>
                    {s.minutes.status === "succeeded" && (
                      <pre aria-label="纪要正文">{s.minutes.minutes_text}</pre>
                    )}
                    {s.minutes.error && (
                      <p>纪要未能完成，请核对配置或来源后再处理。错误码：{s.minutes.error.code}</p>
                    )}
                  </>
                )}
                <p>
                  请用“刷新当前记录”读取执行结果。排队不代表已经生成，封存事实不随纪要失败而撤销。
                </p>
              </section>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
export function MeetingHost({
  client,
  storage,
  active = true,
}: {
  client?: WorkbenchClient;
  storage?: MeetingStorage;
  active?: boolean;
}) {
  const [session] = useState(() => initialize(client, storage));
  if (!session)
    return (
      <section className="meetings" aria-label="会议恢复">
        <p role="alert">恢复信息不可用。已暂停提交，原记录未清除；请修复浏览器存储后重新载入。</p>
      </section>
    );
  return <ActiveMeeting session={session} active={active} />;
}
export default MeetingHost;
