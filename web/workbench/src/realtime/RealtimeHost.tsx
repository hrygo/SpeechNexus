import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { parse } from "../../../../packages/contracts/src/realtime";
import { RealtimeClient } from "./realtime-client";
import { RealtimeJournal, type RealtimeStorage } from "./realtime-journal";
import { RealtimeController, type RealtimeSnapshot } from "./realtime-session";
import InputDevicePicker from "./InputDevicePicker";
import "./realtime.css";

const problems: Record<NonNullable<RealtimeSnapshot["problem"]>, string> = {
  storage: "恢复信息未能保存，已暂停采集与提交。请先复制正文，修复存储后重试。",
  unknown: "提交结果尚未确认。已保留原键和原请求，请按原键重放。",
  rejected: "服务端已明确拒绝请求。当前部署可能尚未提供实时服务，请核对部署引用。",
  read_failed: "当前会话状态尚未确认。请刷新读取；旧回执不能证明当前状态。",
  protocol: "实时消息不符合约定，已停止采集。已确认正文保留，清理状态请重新读取。",
  capture: "麦克风采集已中断或不可用。已停止本次输入，已确认正文仍可复制。",
  capture_cleanup:
    "本地采集资源释放未确认，已阻断新会话与采集。请关闭此浏览器标签页；服务端清理状态仍需单独读取。",
};
const microphoneNames: Record<RealtimeSnapshot["microphone"], string> = {
  idle: "已停止",
  starting: "正在请求设备",
  running: "采集中",
  stopping: "正在停止，等待本地资源释放",
  failed: "本地资源释放未确认",
};
const cleanupNames = {
  not_required: "无需清理",
  pending: "清理待确认",
  complete: "清理完成",
  quarantined: "已隔离，资源尚未释放",
};
const statusNames = {
  created: "已创建，等待连接",
  admitting: "正在准入",
  active: "活动中",
  draining: "正在结束",
  closed: "已关闭",
  failed: "会话失败",
  expired: "会话已过期",
};
const phases = {
  awaiting: "等待准入握手",
  active: "握手完成，可显式采集",
  draining: "已请求排空，等待结果",
  disconnected: "连接已中断，需新会话续接",
  closed: "连接已结束",
};
function initialize(storage?: RealtimeStorage) {
  try {
    return new RealtimeController(
      new RealtimeClient(),
      new RealtimeJournal(storage ?? window.sessionStorage),
    );
  } catch {
    return null;
  }
}
function ActiveHost({ controller, active }: { controller: RealtimeController; active: boolean }) {
  const s = useSyncExternalStore(controller.subscribe, controller.getSnapshot),
    r = s.record;
  const [profile, setProfile] = useState(r.request?.profile.id ?? ""),
    [profileRevision, setProfileRevision] = useState(r.request?.profile.revision ?? ""),
    [model, setModel] = useState(r.request?.models.asr?.id ?? ""),
    [modelRevision, setModelRevision] = useState(r.request?.models.asr?.revision ?? "");
  const [invalid, setInvalid] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null),
    lifecycle = useRef({ generation: 0 });
  useEffect(() => {
    const owner = lifecycle.current,
      generation = ++owner.generation;
    const hidden = () => {
      if (document.hidden) controller.suspend();
    };
    const leaving = () => controller.suspend();
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", leaving);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", leaving);
      queueMicrotask(() => {
        if (owner.generation === generation) controller.dispose();
      });
    };
  }, [controller]);
  useEffect(() => {
    controller.setActive(active);
    if (active) heading.current?.focus();
  }, [active, controller]);
  const locked = s.busy || s.blocked || !!r.pending;
  const canCreate =
    !locked &&
    !s.connecting &&
    s.microphone === "idle" &&
    (!r.session ||
      (!!s.current &&
        ["closed", "failed", "expired"].includes(s.current.status) &&
        ["complete", "not_required"].includes(s.current.cleanup_state)));
  const canCapture =
    !locked &&
    !s.connecting &&
    s.stream?.phase === "active" &&
    r.session?.status !== "draining" &&
    s.microphone === "idle";
  const draft = r.captions.map((c) => c.text).join("\n");
  return (
    <section className="realtime-asr" aria-label="实时字幕工作台" aria-busy={s.busy}>
      <header>
        <h2 ref={heading} tabIndex={-1}>
          实时字幕
        </h2>
        <p>使用部署已发布的预设和模型版本。先创建并连接，服务完成准入后，再由你明确开启麦克风。</p>
      </header>
      <p>
        当前部署共享；会话编号不能充当身份。本标签页保留最终正文草稿，不代表已保存为会议或语音制品。
      </p>
      <p>
        离开此功能、隐藏页面或断线会停止采集。续接必须创建新会话并保留缺口；不会自动重放旧音频。
      </p>
      {s.problem && <p role="alert">{problems[s.problem]}</p>}
      {invalid && <p role="alert">请填写部署实际发布的预设、模型编号及正整数版本。</p>}
      {s.busy && <p role="status">正在提交原请求，请稍候…</p>}
      <div className="realtime-actions">
        {s.blocked && (
          <button type="button" disabled={s.busy} onClick={() => controller.retryStorage()}>
            重试保存恢复信息
          </button>
        )}
        {r.pending && (
          <button
            type="button"
            disabled={s.busy || s.blocked}
            onClick={() => {
              void controller.replay();
            }}
          >
            按原键重放
          </button>
        )}
        {s.problem === "rejected" && r.pending?.kind === "create" && (
          <button
            type="button"
            disabled={s.busy || s.blocked}
            onClick={() => controller.dismissRejected()}
          >
            结束已拒绝的创建请求
          </button>
        )}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          try {
            const body = parse("RealtimeSessionCreateRequest", {
              profile: { id: profile, revision: profileRevision },
              models: { asr: { id: model, revision: modelRevision } },
              modalities: ["asr"],
              audio: { input: { encoding: "pcm_s16le", sample_rate_hz: 24000, channels: 1 } },
            });
            setInvalid(false);
            void controller.create(body);
          } catch {
            setInvalid(true);
          }
        }}
      >
        <fieldset disabled={!canCreate}>
          <legend>部署引用</legend>
          <div className="realtime-fields">
            <label>
              会话预设编号
              <input
                required
                maxLength={128}
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
              />
            </label>
            <label>
              预设版本
              <input
                required
                maxLength={19}
                inputMode="numeric"
                value={profileRevision}
                onChange={(e) => setProfileRevision(e.target.value)}
              />
            </label>
            <label>
              实时转写模型编号
              <input
                required
                maxLength={128}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              />
            </label>
            <label>
              模型版本
              <input
                required
                maxLength={19}
                inputMode="numeric"
                value={modelRevision}
                onChange={(e) => setModelRevision(e.target.value)}
              />
            </label>
          </div>
          <button type="submit">创建实时会话</button>
        </fieldset>
      </form>
      {active && (
        <InputDevicePicker
          value={s.inputDeviceId}
          disabled={locked || s.microphone === "stopping" || s.microphone === "failed"}
          onSelect={(id) => {
            void controller.selectInputDevice(id);
            heading.current?.focus();
          }}
        />
      )}
      {r.session && (
        <section aria-label="会话与清理事实">
          <h3>会话与清理事实</h3>
          <dl>
            <dt>会话编号</dt>
            <dd>{r.session.id}</dd>
            <dt>原请求回执</dt>
            <dd>
              {statusNames[r.session.status]} · {cleanupNames[r.session.cleanup_state]}
              （接受时快照）
            </dd>
            <dt>当前读取</dt>
            <dd>
              {s.current
                ? `${statusNames[s.current.status]} · ${cleanupNames[s.current.cleanup_state]}`
                : "尚未读取或已失效，不能据此确认停止"}
            </dd>
            <dt>连接</dt>
            <dd>
              {s.connecting
                ? "正在连接 / 等待准入"
                : s.stream
                  ? phases[s.stream.phase]
                  : "尚未连接"}
            </dd>
            <dt>麦克风</dt>
            <dd data-testid="asr-microphone-state">{microphoneNames[s.microphone]}</dd>
          </dl>
          <div className="realtime-actions">
            <button
              type="button"
              disabled={locked || s.connecting || !!s.stream}
              onClick={() => {
                void controller.connect();
              }}
            >
              连接此会话
            </button>
            <button
              type="button"
              disabled={s.busy}
              onClick={() => {
                void controller.refresh();
              }}
            >
              读取当前会话状态
            </button>
            <button
              type="button"
              disabled={!canCapture}
              onClick={() => {
                void controller.startCapture();
              }}
            >
              开始麦克风采集
            </button>
            <button
              type="button"
              disabled={
                locked || s.committing || s.stream?.phase !== "active" || !s.stream.uncommitted
              }
              onClick={() => {
                void controller.commit();
              }}
            >
              提交当前语音段
            </button>
            <button
              type="button"
              disabled={locked || s.stream?.phase !== "active"}
              onClick={() => {
                void controller.close("drain");
              }}
            >
              停止采集并排空
            </button>
            <button
              type="button"
              disabled={!r.session || s.busy}
              onClick={() => {
                void controller.close("abort");
              }}
            >
              停止采集并中止
            </button>
          </div>
          {s.stream && (
            <p>
              已发送 {s.stream.sent} samples；确认进入会话缓冲 {s.stream.accepted ?? "未知"}{" "}
              samples；当前可用 credit {s.stream.credit ?? "未知"} samples。确认不代表识别或保存。
            </p>
          )}
          {s.current && s.current.terminal_operations.length > 0 && (
            <ul>
              {s.current.terminal_operations.map((op) => (
                <li key={op.operation_id}>
                  {op.operation_id} · {op.status} · {cleanupNames[op.cleanup_state]}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      <section aria-label="当前会话字幕">
        <h3>当前会话字幕</h3>
        {(!s.stream || s.stream.segments.length === 0) && <p>尚无已确认字幕。</p>}
        {s.stream?.segments.map((row) => (
          <article key={JSON.stringify([row.segmentId, row.itemId])} className="realtime-segment">
            <h4>语音段 {row.segmentId ?? row.itemId}</h4>
            {row.final !== null ? (
              <>
                <p>{row.final === "" ? "正文已完成（空文本）" : "正文已完成"}</p>
                {row.final !== "" && <p className="realtime-text">{row.final}</p>}
              </>
            ) : row.failed ? (
              <p>该段正文生成失败。</p>
            ) : (
              <>
                <p>预览，仍可能整体替换</p>
                <p className="realtime-text">{row.preview || row.delta || "等待正文"}</p>
              </>
            )}
            <p>
              {row.operation
                ? `${row.operation.status === "failed" ? "整体执行失败" : row.operation.status === "succeeded" ? "整体执行成功" : "整体执行已取消"} · ${cleanupNames[row.operation.cleanup]}`
                : "整体执行终态尚未确认"}
            </p>
            {row.operation?.receiptId && <p>执行回执编号：{row.operation.receiptId}</p>}
            {row.auxiliary && (
              <p>
                辅助结果正文版本待核验，未附加到当前正文。text_revision {row.auxiliary.textRevision}{" "}
                / metadata_revision {row.auxiliary.metadataRevision}；alignment{" "}
                {row.auxiliary.alignment} / diarization {row.auxiliary.diarization}
              </p>
            )}
          </article>
        ))}
        {s.stream?.gaps.length !== 0 && !!s.stream?.gaps.length && (
          <div role="status">
            <h4>存在输入或连接缺口</h4>
            <ul>
              {s.stream.gaps.map((gap, i) => (
                <li key={i}>
                  {gap.reason}
                  {gap.span ? ` · samples ${gap.span.start}–${gap.span.end}` : ""}
                </li>
              ))}
            </ul>
            <p>新会话不能填补此前缺口，已确认正文继续保留。</p>
          </div>
        )}
      </section>
      <section aria-label="最终正文草稿">
        <h3>最终正文草稿</h3>
        {r.session && r.captions.some((c) => c.sessionId !== r.session?.id) && (
          <p role="status">已开始新的会话边界，之前的输入或连接缺口仍未补齐。</p>
        )}
        {r.captions.length > 0 && (
          <ul aria-label="字幕会话边界">
            {[...new Set(r.captions.map((c) => c.sessionId))].map((id) => (
              <li key={id}>
                会话 {id} · {r.captions.filter((c) => c.sessionId === id).length} 段已确认正文
              </li>
            ))}
          </ul>
        )}
        <p>
          仅含已确认正文；空成功也有记录。多个会话之间存在连接边界，不能当作无缺口录音。保存上限4MiB或10000段，达到上限会停止输入。
        </p>
        <label>
          本标签页最终正文草稿
          <textarea readOnly rows={8} value={draft} />
        </label>
        <p>已确认 {r.captions.length} 段。</p>
        <button
          type="button"
          disabled={r.captions.length === 0}
          onClick={() => {
            const url = URL.createObjectURL(
                new Blob([draft], { type: "text/plain;charset=utf-8" }),
              ),
              a = document.createElement("a");
            a.href = url;
            a.download = "realtime-transcript.txt";
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 0);
          }}
        >
          下载最终正文草稿
        </button>
      </section>
    </section>
  );
}
export default function RealtimeHost({
  controller,
  storage,
  active = true,
}: {
  controller?: RealtimeController;
  storage?: RealtimeStorage;
  active?: boolean;
}) {
  const [session] = useState(() => controller ?? initialize(storage));
  return session ? (
    <ActiveHost controller={session} active={active} />
  ) : (
    <section className="realtime-asr" aria-label="实时字幕工作台">
      <h2>实时字幕</h2>
      <p role="alert">
        恢复信息不可读取，已暂停创建和采集。原存储未被覆盖，请保留并检查当前标签页数据。
      </p>
    </section>
  );
}
