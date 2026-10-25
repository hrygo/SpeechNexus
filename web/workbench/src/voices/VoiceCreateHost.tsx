import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { SpeechFileClient } from "../file-workflow/speech-file-client";
import { VoiceSession } from "./voice-session";
import { voiceTransport } from "./voice-transport";
import "./voice-create.css";

const errors: Record<string, string> = {
  recovery_unavailable: "恢复记录不可读取，已阻止创建。请保留原标签页记录核对，不要重复创建。",
  storage_unavailable: "恢复存储未确认或被其他操作改写，已阻止网络写入。",
  outcome_unknown: "结果未知。只能重放原操作；不会创建替代任务。",
  input_or_reference_invalid: "输入或参考素材不可用，请核对模型、工作空间和材料用途。",
  current_unavailable: "当前任务读取失败；保留的快照不是当前状态。",
  unsupported_parameter:
    "unsupported_parameter：请求未被接受，请纠正参数后重新确认；原文本已保留。",
  operation_recovery:
    "原执行已有恢复身份，请显式读取当前任务，不再重复提交。取消请求仍须单独确认。",
};
export default function VoiceCreateHost({
  active = true,
  session,
}: {
  active?: boolean;
  session?: VoiceSession;
}) {
  const [c] = useState(
    () =>
      session ??
      new VoiceSession(
        new SpeechFileClient(window.location.origin, voiceTransport(window.location.origin)),
        {
          getItem: (k) => window.sessionStorage.getItem(k),
          setItem: (k, v) => window.sessionStorage.setItem(k, v),
          removeItem: (k) => window.sessionStorage.removeItem(k),
        },
      ),
  );
  const s = useSyncExternalStore(c.subscribe, c.snapshot);
  const fieldId = useId();
  const [mode, setMode] = useState<"voice_design" | "voice_clone">(
    () => c.snapshot().record?.command.kind ?? "voice_design",
  );
  const [form, setForm] = useState(() => {
    const fixed = c.snapshot().record?.command;
    return {
      workspace: fixed?.workspace_id ?? "",
      model: fixed?.model ?? "",
      description: fixed?.kind === "voice_design" ? fixed.input.description : "",
      text: fixed?.input.preview_text ?? "",
      asset: fixed?.kind === "voice_clone" ? fixed.input.reference_audio_asset_id : "",
      reference: fixed?.kind === "voice_clone" ? (fixed.input.reference_text ?? "") : "",
      language: fixed?.input.language ?? "",
      name: fixed?.input.display_name ?? "",
    };
  });
  const [confirmed, setConfirmed] = useState(
    () => c.snapshot().record?.command.kind === "voice_clone",
  );
  const [cancelTarget, setCancelTarget] = useState<string | null>(null);
  const life = useRef({ generation: 0 });
  const confirmButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const owner = life.current;
    const token = ++owner.generation;
    return () => {
      c.setActive(false);
      queueMicrotask(() => {
        if (owner.generation === token) c.dispose();
      });
    };
  }, [c]);
  useEffect(() => {
    const update = () => {
      c.setActive(active && !document.hidden);
      setCancelTarget(null);
    };
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, [active, c]);
  useEffect(() => {
    if (cancelTarget) confirmButton.current?.focus();
  }, [cancelTarget]);
  const locked = !!s.record || s.busy || s.blocked || !active;
  const change = (key: keyof typeof form, value: string) => {
    setForm((f) => ({ ...f, [key]: value }));
    setConfirmed(false);
  };
  const field = (name: string, key: keyof typeof form, textarea = false, required = false) => (
    <div>
      <label htmlFor={`${fieldId}-${key}`}>{name}</label>
      {textarea ? (
        <textarea
          id={`${fieldId}-${key}`}
          disabled={locked}
          value={form[key]}
          required={required}
          onChange={(e) => change(key, e.target.value)}
        />
      ) : (
        <input
          id={`${fieldId}-${key}`}
          disabled={locked}
          value={form[key]}
          required={required}
          onChange={(e) => change(key, e.target.value)}
        />
      )}
    </div>
  );
  const currentCancelable =
    s.current &&
    !!s.job &&
    !s.record?.pending &&
    !s.busy &&
    !s.blocked &&
    !["succeeded", "failed", "cancelled", "expired"].includes(s.job.status);
  const draft =
    s.job?.result && (s.job.result.kind === "voice_design" || s.job.result.kind === "voice_clone")
      ? s.job.result
      : null;
  return (
    <section className="voice-create file-task" aria-labelledby="voice-create-heading">
      <h2 id="voice-create-heading">音色创作任务</h2>
      <p>
        声音由当前部署共享。设计和克隆分别验证；创建任务不等于已发布声音，后续 TTS
        只能使用显式发布的固定声音版本。
      </p>
      <p>
        本入口创建持久任务。候选、再生成、试听、复用校验、发布和退役尚待接入；真实创作服务与模型资格待联调。
      </p>
      <div className="file-task-actions">
        <button
          type="button"
          disabled={locked}
          aria-pressed={mode === "voice_design"}
          onClick={() => {
            setMode("voice_design");
            setConfirmed(false);
          }}
        >
          描述设计
        </button>
        <button
          type="button"
          disabled={locked}
          aria-pressed={mode === "voice_clone"}
          onClick={() => {
            setMode("voice_clone");
            setConfirmed(false);
          }}
        >
          样本克隆
        </button>
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setCancelTarget(null);
          void c.create({
            kind: mode,
            workspace_id: form.workspace,
            model: form.model,
            input: {
              ...(form.name ? { display_name: form.name } : {}),
              ...(form.language ? { language: form.language } : {}),
              ...(mode === "voice_design"
                ? { description: form.description, preview_text: form.text }
                : {
                    reference_audio_asset_id: form.asset,
                    source_use_confirmed: confirmed,
                    ...(form.reference ? { reference_text: form.reference } : {}),
                    ...(form.text ? { preview_text: form.text } : {}),
                  }),
            },
          });
        }}
      >
        <div className="voice-fields">
          {field("工作空间编号", "workspace", false, true)}
          {field("创作模型编号", "model", false, true)}
          {field("显示名称（可选）", "name")}
          {field("语言（可选）", "language")}
          {mode === "voice_design" ? (
            field("声音描述", "description", true, true)
          ) : (
            <>
              {field("参考音频素材编号", "asset", false, true)}
              {field("参考文本", "reference", true)}
            </>
          )}
          {field("试听文本", "text", true, mode === "voice_design")}
        </div>
        {mode === "voice_clone" && (
          <>
            <p>
              使用已上传且就绪的 voice_reference
              音频编号；其他工作空间、用途或失效素材不能用于克隆。
            </p>
            <label>
              <input
                type="checkbox"
                required
                disabled={locked}
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              确认已获准将此参考音频及文本用于声音克隆；修改材料后需要重新确认。
            </label>
          </>
        )}
        <p>
          确认后会把原命令文本、素材编号和恢复键保存在本标签页；不保存音频。未提交输入在切换页面后保留，关闭标签页可能丢失。
        </p>
        <button type="submit" disabled={locked}>
          确认并创建任务
        </button>
      </form>
      {s.error && <p role="alert">{errors[s.error] ?? "操作未确认，请保留原意图核对。"}</p>}
      {s.busy && <p role="status">正在核对音色任务…</p>}
      {s.blocked && (
        <button type="button" disabled={s.busy} onClick={() => c.retryStorage()}>
          重试恢复存储
        </button>
      )}
      {s.record && (
        <>
          <h3>固定原意图</h3>
          <pre aria-label="固定音色命令">{JSON.stringify(s.record.command, null, 2)}</pre>
          <p>
            任务编号：{s.record.jobId ?? "创建结果尚未确认"}；原操作：
            {s.record.pending ?? "已收到回执"}。
          </p>
          {s.record.operationId && (
            <p>
              原执行编号：{s.record.operationId}
              。读取执行仅用于定位原任务，执行回执不代表当前资源已释放。
            </p>
          )}
          {s.record.pending && !s.record.operationId && (
            <button type="button" disabled={s.busy || s.blocked} onClick={() => void c.replay()}>
              重放原操作
            </button>
          )}
          <button
            type="button"
            disabled={(!s.record.jobId && !s.record.operationId) || s.busy || s.blocked}
            onClick={() => {
              setCancelTarget(null);
              void c.refresh();
            }}
          >
            读取当前任务
          </button>
          {s.job && (
            <dl>
              <dt>任务快照</dt>
              <dd>{s.current ? "本次读取的当前状态" : "历史接受快照，需显式重读"}</dd>
              <dt>任务状态</dt>
              <dd data-testid="voice-job-status">{s.job.status}</dd>
              <dt>资源清理</dt>
              <dd data-testid="voice-cleanup">{s.job.cleanup_state}</dd>
              <dt>生效模型版本</dt>
              <dd>
                {Object.entries(s.job.effective_configuration.models)
                  .map(([role, ref]) => `${role}: ${ref.id} / ${ref.revision}`)
                  .join("；")}
              </dd>
            </dl>
          )}
          {s.job?.error && (
            <p role="alert">
              任务失败：{s.job.error.code}。资源状态仍需单独核对；服务端错误正文不在此展示。
            </p>
          )}
          {draft && (
            <p>
              任务返回草稿 {draft.voice_draft_id} / {draft.draft_revision}
              ，仍不是可复用的已发布声音。
            </p>
          )}
          {s.cancellation && (
            <p>
              取消回执：{s.cancellation.status} / {s.cancellation.cleanup_state}
              。取消接受不代表计算已停止或资源已释放。
            </p>
          )}
          <button
            type="button"
            disabled={!currentCancelable}
            onClick={() => setCancelTarget(s.record!.jobId)}
          >
            准备取消
          </button>
          {cancelTarget === s.record.jobId && currentCancelable && (
            <div role="group" aria-label="确认音色任务取消">
              <p>确认取消任务 {cancelTarget}？</p>
              <button
                ref={confirmButton}
                type="button"
                onClick={() => {
                  setCancelTarget(null);
                  void c.cancel();
                }}
              >
                确认取消此任务
              </button>
              <button type="button" onClick={() => setCancelTarget(null)}>
                返回
              </button>
            </div>
          )}
          <button
            type="button"
            disabled={!c.canNext()}
            onClick={() => {
              c.next();
              setConfirmed(false);
            }}
          >
            新建下一项任务
          </button>
        </>
      )}
    </section>
  );
}
