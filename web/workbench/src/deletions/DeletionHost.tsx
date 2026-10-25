import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { WorkbenchClient, type Contracts } from "../manuscripts/workbench-client";
import { DeletionJournal, type DeletionStorage } from "./deletion-journal";
import { confirmation, targetKey, type DeletionTarget } from "./deletion-plan";
import { DeletionSession, type DeletionProblem } from "./deletion-session";
import "./deletions.css";

const problems: Record<DeletionProblem, string> = {
  unknown: "提交结果尚未确认。请按原键重放，不能另建替代请求。",
  rejected: "服务端已明确拒绝此请求。请结束该请求后，重新读取并生成预检。",
  read_failed: "读取未完成。原请求与接受回执仍保留，请检查连接后刷新。",
  storage: "恢复信息未能保存，已暂停提交。请修复存储后重试保存。",
  unavailable: "当前部署尚未启用工作台。已有恢复信息仍保留。",
  invalid: "确认范围不完整或当前计划存在阻断，尚未提交删除。",
  expired: "预检已过期。请结束本次预检，重新读取目标并生成新预检。",
};
const statusNames = {
  queued: "已排队",
  running: "正在清理",
  blocked: "清理受阻",
  succeeded: "清理完成",
  failed: "清理失败",
};
const cleanupNames = {
  pending: "等待清理",
  quarantined: "已隔离，清理尚未完成",
  complete: "已完成物理清理",
};
const dispositionNames = { blocks: "阻断删除", cascade: "一并删除", retain: "保留，不随本次删除" };
const relationNames = {
  persistent_reference: "持久引用",
  active_execution: "活动执行",
  owned_child: "所属内容",
  independent_copy: "独立副本",
};
function Target({ value }: { value: DeletionTarget }) {
  return (
    <span>
      {value.owner} / {value.kind} / {value.resource.id} · revision {value.resource.revision}
    </span>
  );
}
function ConfirmPlan({
  plan,
  locked,
  onConfirm,
  active,
}: {
  plan: Contracts["DeletionPlan"];
  locked: boolean;
  onConfirm: (targets: DeletionTarget[]) => void;
  active: boolean;
}) {
  const [checked, setChecked] = useState<string[]>([]),
    [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  let targets: DeletionTarget[] = [];
  try {
    targets = confirmation(plan).confirm_targets;
  } catch {
    /* Blocked plans remain visible without confirm controls. */
  }
  const expired = Date.parse(plan.expires_at) <= now;
  return (
    <section aria-label="删除预检结果">
      <h3>预检范围</h3>
      <p>
        目标：
        <Target value={plan.target} />
      </p>
      <p>
        方式：{plan.mode === "cascade" ? "级联删除" : "普通删除"} ·{" "}
        {plan.eligible ? "无阻断依赖" : "存在阻断，不能确认"}
      </p>
      <dl>
        <dt>计划编号 / 版本</dt>
        <dd>
          {plan.id} / {plan.plan_revision}
        </dd>
        <dt>摘要</dt>
        <dd>{plan.plan_digest}</dd>
        <dt>有效期至</dt>
        <dd>
          {plan.expires_at}
          {expired && "（已过期）"}
        </dd>
      </dl>
      <h4>完整依赖</h4>
      {plan.dependencies.length === 0 ? (
        <p>当前预检没有依赖。</p>
      ) : (
        <ul>
          {plan.dependencies.map((d, i) => (
            <li key={i}>
              <Target value={d.target} /> — {relationNames[d.relation]} ·{" "}
              {dispositionNames[d.disposition]}
            </li>
          ))}
        </ul>
      )}
      <p>独立副本与外部源不会因本次级联自动删除。清理完成后不能从此页面撤销。</p>
      {targets.length > 0 && (
        <fieldset disabled={locked || expired}>
          <legend>逐项确认待删除内容</legend>
          {targets.map((t) => (
            <label key={targetKey(t)}>
              <input
                type="checkbox"
                checked={checked.includes(targetKey(t))}
                onChange={(e) =>
                  setChecked((v) =>
                    e.target.checked ? [...v, targetKey(t)] : v.filter((k) => k !== targetKey(t)),
                  )
                }
              />
              <Target value={t} />
            </label>
          ))}
          <button
            type="button"
            disabled={checked.length !== targets.length}
            onClick={() => onConfirm(targets)}
          >
            确认所列范围并提交删除
          </button>
        </fieldset>
      )}
    </section>
  );
}
function ActiveDeletion({ session, active }: { session: DeletionSession; active: boolean }) {
  const s = useSyncExternalStore(session.subscribe, session.getSnapshot),
    r = s.record;
  const [mode, setMode] = useState<Contracts["DeletionPlanRequest"]["mode"]>("ordinary");
  const heading = useRef<HTMLHeadingElement>(null),
    owner = useRef({ generation: 0 }),
    wasActive = useRef(active);
  const locked = s.busy || s.blocked || r.pending !== null,
    selecting = locked || r.request !== null;
  useEffect(() => {
    const current = owner.current,
      generation = ++current.generation;
    void session.start();
    return () =>
      queueMicrotask(() => {
        if (current.generation === generation) session.dispose();
      });
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
  }, [r.plan?.id, r.accepted?.id]);
  return (
    <section className="deletions" aria-label="删除与恢复工作台" aria-busy={s.busy}>
      <header>
        <h2 ref={heading} tabIndex={-1}>
          删除与恢复
        </h2>
        <p>
          当前部署的内容由所有工作台使用者共享。先预检，再逐项确认完整范围；恢复用于查询未知结果，不表示撤销删除。
        </p>
      </header>
      {s.busy && <p role="status">正在读取或提交…</p>}
      {s.problem && <p role="alert">{problems[s.problem]}</p>}
      {s.blocked && (
        <button type="button" disabled={s.busy} onClick={() => session.retryStorage()}>
          重试保存恢复信息
        </button>
      )}
      {r.pending && (
        <aside>
          <p>当前标签页已保留原请求。不会自动重放，也不能在结果未知时切换意图。</p>
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
        </aside>
      )}
      {!r.request && (
        <div className="deletion-directory">
          <section aria-label="删除目标目录">
            <h3>选择项目</h3>
            <button
              type="button"
              disabled={locked}
              onClick={() => {
                void session.discover();
              }}
            >
              刷新项目与配置
            </button>
            {s.projects?.data.length === 0 && <p>还没有可读取的项目。</p>}
            <ul>
              {s.projects?.data.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    disabled={selecting}
                    aria-pressed={s.target?.kind === "project" && s.target.resource.id === p.id}
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
                disabled={selecting}
                onClick={() => {
                  void session.moreProjects();
                }}
              >
                更多项目
              </button>
            )}
            {s.project && (
              <>
                <h3>或选择该项目内的稿件</h3>
                <p>当前项目：{s.project.name}</p>
                <ul>
                  {s.transcripts?.data.map((t) => (
                    <li key={t.id}>
                      <button
                        type="button"
                        disabled={selecting}
                        aria-pressed={
                          s.target?.kind === "transcript" && s.target.resource.id === t.id
                        }
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
                    disabled={selecting}
                    onClick={() => {
                      void session.moreTranscripts();
                    }}
                  >
                    更多稿件
                  </button>
                )}
              </>
            )}
          </section>
          <section>
            <h3>生成预检</h3>
            {s.target ? (
              <p>
                已选：
                <Target value={s.target} />
              </p>
            ) : (
              <p>请选择项目或稿件。此入口尚不支持其他资源。</p>
            )}
            <label>
              删除方式
              <select
                value={mode}
                disabled={selecting}
                onChange={(e) => setMode(e.target.value as typeof mode)}
              >
                <option value="ordinary">普通删除：引用或活动执行会阻断</option>
                <option value="cascade">级联删除：逐项确认所属内容</option>
              </select>
            </label>
            <button
              type="button"
              disabled={selecting || !s.target || !s.status?.workbench_enabled}
              onClick={() => {
                void session.preview(mode);
              }}
            >
              生成删除预检
            </button>
          </section>
        </div>
      )}
      {r.plan && (
        <ConfirmPlan
          key={r.plan.id}
          plan={r.plan}
          active={active}
          locked={locked || !!r.accepted || !s.status?.workbench_enabled}
          onConfirm={(t) => {
            void session.confirm(t);
          }}
        />
      )}
      {r.accepted && (
        <section>
          <h3>原始接受回执</h3>
          <p>删除编号：{r.accepted.id}</p>
          <p>
            接受时状态：{statusNames[r.accepted.status]} / {cleanupNames[r.accepted.cleanup_state]}
            。接受回执不代表当前清理完成。
          </p>
          <button
            type="button"
            disabled={locked}
            onClick={() => {
              void session.refresh();
            }}
          >
            读取当前清理状态
          </button>
          <h3>当前清理状态</h3>
          {s.current ? (
            <>
              <p role="status">
                {statusNames[s.current.status]} · {cleanupNames[s.current.cleanup_state]}
              </p>
              <p>核验时间：{s.current.updated_at}</p>
              {s.current.error && <p>服务端错误码：{s.current.error.code}</p>}
              {s.current.result && (
                <>
                  <p>清理账本：{s.current.result.ledger_id}</p>
                  <h4>已删除</h4>
                  <ul>
                    {s.current.result.deleted.map((t) => (
                      <li key={targetKey(t)}>
                        <Target value={t} />
                      </li>
                    ))}
                  </ul>
                  <h4>保留的独立副本</h4>
                  <ul>
                    {s.current.result.retained_independent_copies.map((t) => (
                      <li key={targetKey(t)}>
                        <Target value={t} />
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          ) : (
            <p>当前状态尚未确认，请重新读取。</p>
          )}
        </section>
      )}
      {r.request && !r.pending && (
        <button type="button" disabled={locked} onClick={() => session.reset()}>
          {r.accepted ? "开始另一项预检" : "结束本次预检并重新选择"}
        </button>
      )}
    </section>
  );
}
function initialize(client?: WorkbenchClient, storage?: DeletionStorage) {
  try {
    return new DeletionSession(
      client ?? new WorkbenchClient(),
      new DeletionJournal(storage ?? window.sessionStorage),
    );
  } catch {
    return null;
  }
}
export default function DeletionHost({
  client,
  storage,
  active = true,
}: {
  client?: WorkbenchClient;
  storage?: DeletionStorage;
  active?: boolean;
}) {
  const [session, setSession] = useState(() => initialize(client, storage));
  return session ? (
    <ActiveDeletion session={session} active={active} />
  ) : (
    <section className="deletions" aria-label="删除与恢复工作台">
      <h2>删除与恢复</h2>
      <p role="alert">恢复信息不可读取，已暂停所有提交。原信息保持，请检查当前标签页存储。</p>
      <button type="button" onClick={() => setSession(initialize(client, storage))}>
        重新读取恢复信息
      </button>
    </section>
  );
}
