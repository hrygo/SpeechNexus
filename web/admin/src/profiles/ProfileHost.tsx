import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  ProfileClient,
  profileDefinition,
  profileRequest,
  type ProfileRevision,
} from "./profile-client";
import { browserProfileJournal } from "./profile-journal";
import { ProfileSession } from "./profile-session";
import "./profiles.css";

function definition(value: Record<string, unknown>): string {
  return JSON.stringify(profileDefinition(value), null, 2);
}

export function ProfileHost({
  session,
  active = true,
}: {
  session?: ProfileSession;
  active?: boolean;
}) {
  const [c] = useState(
    () =>
      session ?? new ProfileSession(new ProfileClient(location.origin), browserProfileJournal()),
  );
  const s = useSyncExternalStore(c.subscribe, c.snapshot);
  const [id, setId] = useState(s.record?.body.profile_id ?? "");
  const [fresh, setFresh] = useState(s.record?.body.expected_latest_revision == null);
  const [expected, setExpected] = useState(s.record?.body.expected_latest_revision ?? "");
  const [json, setJson] = useState(s.record ? definition(s.record.body) : "");
  const [readId, setReadId] = useState("");
  const [readRevision, setReadRevision] = useState("");
  const [inputError, setInputError] = useState(false);
  const confirmation = useRef<HTMLButtonElement>(null);
  const life = useRef({ generation: 0 });
  useEffect(() => {
    const owner = life.current,
      token = ++owner.generation;
    return () => {
      c.setActive(false);
      queueMicrotask(() => {
        if (owner.generation === token) c.dispose();
      });
    };
  }, [c]);
  useEffect(() => {
    const update = () => c.setActive(active && !document.hidden),
      stop = () => c.setActive(false);
    update();
    document.addEventListener("visibilitychange", update);
    window.addEventListener("pagehide", stop);
    window.addEventListener("pageshow", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("pagehide", stop);
      window.removeEventListener("pageshow", update);
    };
  }, [active, c]);
  useEffect(() => {
    if (s.record?.phase === "prepared") confirmation.current?.focus();
  }, [s.record?.phase]);
  const disabled = !s.active || s.blocked || !!s.busy;
  const locked = disabled || !!s.record;
  const copyRevision = (revision: ProfileRevision) => {
    setId(revision.profile_id);
    setFresh(false);
    setExpected(revision.revision);
    setJson(definition(revision));
    setInputError(false);
  };
  const problem =
    s.problem === "configuration_changed"
      ? "分配修订已改变，请返回编辑并核对已知修订后重新决定。"
      : s.problem === "idempotency_conflict"
        ? "原键与服务端记录冲突，请核对原请求。"
        : s.problem === "profile_storage_unavailable"
          ? "本地请求记录不可用，已停止提交；请保留当前页面与原请求。"
          : s.problem === "profile_outcome_unknown"
            ? "提交结果未知。只能按原请求、原键重放；固定读取不能确认此次写入。"
            : s.problem
              ? `操作未确认：${s.problem}`
              : null;
  return (
    <section className="task-center profile-drafts" aria-label="场景预设草稿管理">
      <h2>场景预设草稿</h2>
      <p>
        草稿不会发布或切换当前预设。发布、激活与退役尚未接入；此入口与工作台共享部署数据，没有身份隔离。
      </p>
      <p>请填写完整配置，并核对提交所基于的修订；此页面不会自动补入预设默认值。</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setInputError(false);
          try {
            if (!c.prepare(profileRequest(id, fresh ? null : expected, json))) setInputError(true);
          } catch {
            setInputError(true);
          }
        }}
      >
        <fieldset disabled={locked}>
          <legend>编辑草稿</legend>
          <label>
            预设系列编号
            <input value={id} onChange={(e) => setId(e.target.value)} />
          </label>
          <label>
            <input type="checkbox" checked={fresh} onChange={(e) => setFresh(e.target.checked)} />
            创建尚无修订的新系列
          </label>
          {!fresh && (
            <label>
              已知的最新分配修订
              <input
                value={expected}
                onChange={(e) => setExpected(e.target.value)}
                inputMode="numeric"
              />
            </label>
          )}
          <label>
            完整预设配置（JSON）
            <textarea
              rows={12}
              value={json}
              onChange={(e) => setJson(e.target.value)}
              spellCheck={false}
            />
          </label>
          <button type="submit">核对草稿</button>
        </fieldset>
      </form>
      {inputError && <p role="alert">配置未通过校验，请检查完整配置和修订条件；原输入已保留。</p>}
      {problem && <p role="alert">{problem}</p>}
      {s.busy && <p role="status">正在处理{s.busy === "write" ? "草稿提交" : "固定读取"}…</p>}
      {s.record && (
        <div>
          <h3>核对原始提交</h3>
          <p>幂等键：{s.record.key}</p>
          <pre aria-label="待确认的完整草稿请求">{JSON.stringify(s.record.body, null, 2)}</pre>
          {s.record.phase === "unknown" && (
            <p>原提交结果仍未知；读取任何固定修订都不能解除这笔提交。</p>
          )}
          {(s.record.phase === "prepared" || s.record.phase === "unknown") && (
            <button
              ref={confirmation}
              type="button"
              disabled={disabled}
              onClick={() => void c.confirm()}
            >
              {s.record.phase === "prepared" ? "确认提交草稿" : "按原键重放草稿"}
            </button>
          )}
          {s.record.phase === "accepted" && (
            <div aria-label="草稿回执">
              <p>
                {s.receiptCurrent
                  ? "本次服务端接收的草稿回执"
                  : "历史缓存草稿回执；并非本次服务端读取"}
              </p>
              <p>
                {s.record.result.profile_id} · {s.record.result.revision} ·{" "}
                {s.record.result.publication.status}
              </p>
              <p>草稿尚未发布，此回执不代表运行资格或质量验收。</p>
            </div>
          )}
          <button
            type="button"
            disabled={disabled || s.record.phase === "unknown"}
            onClick={() => {
              const record = s.record;
              if (c.finish() && record?.phase === "accepted") copyRevision(record.result);
            }}
          >
            返回编辑
          </button>
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void c.read(readId, readRevision);
        }}
      >
        <fieldset disabled={disabled}>
          <legend>读取固定修订</legend>
          <label>
            读取预设系列编号
            <input value={readId} onChange={(e) => setReadId(e.target.value)} />
          </label>
          <label>
            读取固定修订
            <input
              value={readRevision}
              onChange={(e) => setReadRevision(e.target.value)}
              inputMode="numeric"
            />
          </label>
          <button type="submit">读取固定修订</button>
        </fieldset>
      </form>
      {s.revision && (
        <div>
          <p>{s.readCurrent ? "本次固定读取的修订快照" : "历史修订快照；请显式重读"}</p>
          <pre aria-label="固定修订快照">{JSON.stringify(s.revision, null, 2)}</pre>
          <button
            type="button"
            disabled={locked || !s.readCurrent}
            onClick={() => copyRevision(s.revision!)}
          >
            以此修订为基础编辑
          </button>
        </div>
      )}
    </section>
  );
}
