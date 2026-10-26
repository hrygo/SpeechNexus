import { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { parse } from "../../../../packages/contracts/src/realtime";
import { RealtimeClient } from "./realtime-client";
import type { RealtimeStorage } from "./realtime-journal";
import { TtsJournal } from "./tts-journal";
import { TtsController, type TtsControllerSnapshot } from "./tts-session";
import { useRealtimeFocus } from "./useRealtimeFocus";
import "./realtime.css";

const problems: Record<NonNullable<TtsControllerSnapshot["problem"]>, string> = {
  storage: "文本或恢复信息未保存，已停止本地播放并阻断提交。请先复制文本，再重试保存。",
  unknown: "请求结果尚未确认。请按原键重放，避免重复创建或关闭。",
  rejected: "服务端明确拒绝请求，请核对部署实际发布的引用。",
  read_failed: "当前会话事实读取失败，请重新查询。",
  protocol: "实时消息或发送不符合约定，已停止本地链路。请查询清理状态。",
  playback: "本地播放不可用或已中断，请查询会话后重新开始。",
  playback_cleanup:
    "本地播放资源释放失败，尚未确认停止，已阻断新的播放。请关闭此页面以释放资源；服务端清理状态仍需另行查询。",
};
function initialize(storage?: RealtimeStorage) {
  try {
    return new TtsController(
      new RealtimeClient(),
      new TtsJournal(storage ?? window.sessionStorage),
    );
  } catch {
    return null;
  }
}
function Content({ controller: c, active }: { controller: TtsController; active: boolean }) {
  const textId = useId();
  const s = useSyncExternalStore(c.subscribe, c.getSnapshot),
    r = s.record,
    u = s.stream?.utterance;
  const [profile, setProfile] = useState(r.request?.profile.id ?? ""),
    [profileRev, setProfileRev] = useState(r.request?.profile.revision ?? ""),
    [model, setModel] = useState(r.request?.models.tts?.id ?? ""),
    [modelRev, setModelRev] = useState(r.request?.models.tts?.revision ?? ""),
    [voice, setVoice] = useState(r.request?.voice?.id ?? ""),
    [voiceRev, setVoiceRev] = useState(r.request?.voice?.revision ?? "");
  const { root, heading } = useRealtimeFocus(active);
  const [invalid, setInvalid] = useState(false),
    life = useRef({ generation: 0 });
  useEffect(() => {
    const owner = life.current,
      gen = ++owner.generation;
    const hidden = () => {
        void c.setActive(active && !document.hidden);
      },
      pagehide = () => c.suspend();
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", pagehide);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", pagehide);
      queueMicrotask(() => {
        if (owner.generation === gen) c.dispose();
      });
    };
  }, [active, c]);
  useEffect(() => {
    void c.setActive(active && !document.hidden);
    if (active && !document.hidden) heading.current?.focus();
  }, [active, c, heading]);
  const locked = s.busy || s.blocked || !!r.pending || !active;
  const released =
    s.current &&
    ["closed", "failed", "expired"].includes(s.current.status) &&
    ["complete", "not_required"].includes(s.current.cleanup_state);
  const canStart =
    !locked &&
    !s.starting &&
    s.stream?.phase === "active" &&
    (!u || (!!u.done && ["complete", "not_required"].includes(u.done.cleanup_state))) &&
    (!s.playback || s.playback.state === "stopped");
  const accepting =
    !locked &&
    u?.phase === "accepting" &&
    (s.stream?.phase === "active" || s.stream?.phase === "draining");
  return (
    <section ref={root} className="realtime-asr" aria-label="实时配音工作台">
      <header>
        <h2 ref={heading} tabIndex={-1}>
          实时配音
        </h2>
        <p>
          按段追加文本，再冻结前缀或结束输入。文本确认、生成结果与本地播放分别显示。当前部署引用由管理员提供。
        </p>
        <p>
          文本仅保存在当前标签页；刷新后不会自动连接或重播音频。此入口尚未完成真实服务联调，不代表模型或声音已经可用。
        </p>
      </header>
      {s.problem && <p role="alert">{problems[s.problem]}</p>}
      {invalid && <p role="alert">引用或版本不符合部署契约，请检查输入。</p>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          try {
            const body = parse("RealtimeSessionCreateRequest", {
              profile: { id: profile, revision: profileRev },
              modalities: ["tts"],
              models: { tts: { id: model, revision: modelRev } },
              ...(voice ? { voice: { id: voice, revision: voiceRev } } : {}),
              audio: { output: { encoding: "pcm_s16le", sample_rate_hz: 24000, channels: 1 } },
            });
            setInvalid(false);
            void c.create(body);
          } catch {
            setInvalid(true);
          }
        }}
      >
        <fieldset
          disabled={
            locked ||
            s.connecting ||
            s.starting ||
            (!!s.playback && s.playback.state !== "stopped") ||
            (!!r.session && !released)
          }
        >
          <legend>部署引用</legend>
          <div className="realtime-fields">
            <label>
              TTS profile ID
              <input required value={profile} onChange={(e) => setProfile(e.target.value)} />
            </label>
            <label>
              TTS profile revision
              <input required value={profileRev} onChange={(e) => setProfileRev(e.target.value)} />
            </label>
            <label>
              TTS model ID
              <input required value={model} onChange={(e) => setModel(e.target.value)} />
            </label>
            <label>
              TTS model revision
              <input required value={modelRev} onChange={(e) => setModelRev(e.target.value)} />
            </label>
            <label>
              Voice ID（可留空由服务端解析）
              <input value={voice} onChange={(e) => setVoice(e.target.value)} />
            </label>
            <label>
              Voice revision
              <input
                required={!!voice}
                value={voiceRev}
                onChange={(e) => setVoiceRev(e.target.value)}
              />
            </label>
          </div>
          <button type="submit">创建实时配音会话</button>
        </fieldset>
      </form>
      <div className="realtime-actions">
        <button disabled={!r.pending || s.busy || s.blocked} onClick={() => void c.replay()}>
          按原键重放
        </button>
        {s.problem === "rejected" && (
          <button onClick={() => c.dismissRejected()}>确认拒绝并解除原请求</button>
        )}
        {s.blocked && <button onClick={() => c.retryStorage()}>重试保存恢复信息</button>}
        <button
          disabled={locked || !r.session || !!s.stream || s.connecting}
          onClick={() => void c.connect()}
        >
          连接配音会话
        </button>
        <button disabled={!canStart} onClick={() => void c.start()}>
          开始一轮配音
        </button>
        <button disabled={locked || !r.session} onClick={() => void c.close("drain")}>
          排空并关闭
        </button>
        <button disabled={locked || !r.session} onClick={() => void c.close("abort")}>
          中止会话
        </button>
        <button disabled={!r.session || s.busy} onClick={() => void c.refresh()}>
          查询当前清理状态
        </button>
      </div>
      <label htmlFor={textId}>待追加文本</label>
      <textarea
        id={textId}
        rows={5}
        value={r.text}
        maxLength={2000}
        onChange={(e) => c.setText(e.target.value)}
      />
      <div className="realtime-actions">
        <button
          disabled={
            !accepting ||
            s.stream?.phase !== "active" ||
            !r.text ||
            u.sent !== u.accepted ||
            new TextEncoder().encode(r.text).length > u.credit
          }
          onClick={() => c.append()}
        >
          追加这段文本
        </button>
        <button disabled={!accepting || u.accepted <= u.committed} onClick={() => c.commit()}>
          冻结已确认前缀
        </button>
        <button
          disabled={!accepting || u.sent === 0 || u.sent !== u.accepted}
          onClick={() => c.finish()}
        >
          结束文本输入
        </button>
        <button
          disabled={
            !active ||
            (s.playback?.state !== "running" &&
              (locked || !u || u.phase === "terminal" || u.phase === "cancelling"))
          }
          onClick={() => c.cancel()}
        >
          {u?.phase === "terminal" ? "停止本轮播放" : "取消本轮并停止播放"}
        </button>
      </div>
      <section aria-label="配音事实">
        <h3>会话与播放事实</h3>
        <p data-testid="tts-phase">
          {s.stream?.phase === "draining"
            ? "排空中，可结束已接受的文本"
            : s.stream?.phase === "active"
              ? "握手完成"
              : s.stream?.gap
                ? "连接有缺口，旧音频不会恢复"
                : s.connecting
                  ? "正在连接"
                  : "尚未连接"}
        </p>
        <dl>
          <dt>会话</dt>
          <dd>{r.session?.id ?? "未创建"}</dd>
          <dt>当前服务端清理</dt>
          <dd>
            {s.current ? `${s.current.status} / ${s.current.cleanup_state}` : "尚未查询或已失效"}
          </dd>
          <dt>实际声音版本</dt>
          <dd>
            {s.stream?.configuration?.voice
              ? `${s.stream.configuration.voice.id} / ${s.stream.configuration.voice.revision}`
              : "等待服务端握手"}
          </dd>
          <dt>确认文本序号 / 冻结前缀 / 剩余credit</dt>
          <dd>{u ? `${u.accepted} / ${u.committed} / ${u.credit}` : "等待本轮开始"}</dd>
          <dt>生成</dt>
          <dd>
            {u?.done
              ? `${u.done.status} / ${u.done.cleanup_state}`
              : u
                ? "执行尚未终结"
                : "尚未开始"}
          </dd>
          <dt>网络收到样本</dt>
          <dd data-testid="tts-received">{u?.received ?? 0}</dd>
          <dt>实际渲染样本</dt>
          <dd data-testid="tts-rendered">{s.playback?.rendered ?? 0}</dd>
          <dt>本地播放</dt>
          <dd data-testid="tts-playback-state">
            {s.playback?.state === "closing"
              ? "正在停止，等待本地资源释放"
              : s.playback?.state === "failed"
                ? "本地播放失败，停止状态待确认"
                : (s.playback?.state ?? "尚未启动")}
          </dd>
        </dl>
        {u && <p className="realtime-text">{u.text || "本轮尚未追加文本"}</p>}
        <p>
          播放水位表示 AudioWorklet
          已渲染，不保证物理扬声器时延。生成成功不等于全部播放；中止请求被接受也不等于算力已经释放。文本credit采用保守字节预算。
        </p>
      </section>
    </section>
  );
}
export default function RealtimeTtsHost({
  active = true,
  storage,
  controller,
}: {
  active?: boolean;
  storage?: RealtimeStorage;
  controller?: TtsController;
}) {
  const [c] = useState(() => controller ?? initialize(storage));
  return c ? (
    <Content controller={c} active={active} />
  ) : (
    <section className="realtime-asr">
      <h2>实时配音</h2>
      <p role="alert">恢复信息不可读取，已阻断提交。原存储未被覆盖。</p>
    </section>
  );
}
