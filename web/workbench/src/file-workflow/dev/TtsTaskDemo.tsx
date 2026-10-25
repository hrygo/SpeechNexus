import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AppShell } from "@speechnexus/shared";
import { FileTaskSession } from "../file-task-session";
import { SpeechFileClient } from "../speech-file-client";
import { createTtsDemoTransport, type TtsDemoScenario } from "./tts-demo-transport";
import "./file-task-demo.css";
import "./tts-task-demo.css";

const selected = {
  model: "tts-quality",
  voiceId: "voice-demo",
  voiceRevision: "1",
  profileId: "profile-demo",
  profileRevision: "2",
};

function DemoSession({ scenario }: { scenario: TtsDemoScenario }) {
  const [{ session, transport }] = useState(() => {
    const transport = createTtsDemoTransport(scenario);
    return {
      transport,
      session: new FileTaskSession(new SpeechFileClient(location.origin, transport.fetch)),
    };
  });
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [text, setText] = useState("合成演练：这是一段测试提示音，不代表真实配音。");
  const [started, setStarted] = useState(false);
  const [audioNotice, setAudioNotice] = useState("");
  const player = useRef<HTMLAudioElement>(null);
  const previewUrl = useRef<string | undefined>(undefined);
  const owner = useRef({ generation: 0, disposed: false });
  useEffect(() => {
    const lifecycle = owner.current;
    const generation = ++lifecycle.generation;
    const audio = player.current;
    return () =>
      queueMicrotask(() => {
        if (lifecycle.generation !== generation) return;
        lifecycle.disposed = true;
        session.dispose();
        audio?.pause();
        audio?.removeAttribute("src");
        audio?.load();
        if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
        previewUrl.current = undefined;
      });
  }, [session]);
  const audioId =
    state.job?.result?.kind === "tts" ? state.job.result.audio_artifact_id : undefined;
  async function loadAudio(download: boolean) {
    if (!audioId) return;
    const result = await session.download(audioId);
    if (!result || owner.current.disposed) return;
    if (result.artifact.kind !== "audio" || result.artifact.media_type !== "audio/wav") {
      setAudioNotice("音频格式与本次 WAV 请求不一致，请重新读取任务。");
      return;
    }
    if (download) {
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "synthetic-speech.wav";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      return;
    }
    const audio = player.current;
    if (!audio) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
    previewUrl.current = URL.createObjectURL(result.blob);
    audio.src = previewUrl.current;
    try {
      await audio.play();
      if (!owner.current.disposed) setAudioNotice("正在试听合成提示音。");
    } catch {
      if (!owner.current.disposed) setAudioNotice("浏览器未开始播放，可使用播放器控件或下载音频。");
    }
  }
  return (
    <div className="file-demo-grid">
      <section className="file-demo-input" aria-labelledby="tts-input-title">
        <h2 id="tts-input-title">文本与固定声音</h2>
        <p>仅演练合成数据。声音和模型均为测试引用，输出是一段提示音。</p>
        <dl>
          <dt>声音版本</dt>
          <dd>voice-demo / 1</dd>
          <dt>参数版本</dt>
          <dd>profile-demo / 2</dd>
          <dt>模型与格式</dt>
          <dd>tts-quality / WAV</dd>
        </dl>
        <label htmlFor="tts-text">配音文本</label>
        <textarea
          id="tts-text"
          value={text}
          maxLength={200000}
          disabled={started || state.busy}
          onChange={(event) => setText(event.target.value)}
        />
        <button
          type="button"
          disabled={started || state.busy || !text.length}
          onClick={async () => {
            setStarted(true);
            await session.startText(text, selected);
          }}
        >
          创建配音任务
        </button>
        <p>任务创建后保留原文本素材和声音版本。修改文本需要另建意图。</p>
      </section>
      <section className="file-demo-task" aria-labelledby="tts-task-title">
        <h2 id="tts-task-title">任务与音频</h2>
        <p role="status" className="file-demo-notice">
          {state.notice}
        </p>
        {state.unknown && <p role="alert">结果未知；保留原请求身份，不创建替代任务。</p>}
        {state.assetRecoveryId && <p>原素材恢复标识：{state.assetRecoveryId}</p>}
        {state.asset?.status === "ready" && !state.job && !state.unknown && (
          <button
            type="button"
            disabled={state.busy}
            onClick={() => session.continueAfterValidation()}
          >
            继续创建原任务
          </button>
        )}
        <dl>
          <dt>状态</dt>
          <dd data-testid="tts-status">{state.job?.status ?? "尚未确认"}</dd>
          <dt>资源清理</dt>
          <dd data-testid="tts-cleanup">{state.job?.cleanup_state ?? "尚无证据"}</dd>
          <dt>取消响应</dt>
          <dd data-testid="tts-cancellation">
            {state.cancellation ? `accepted=${String(state.cancellation.accepted)}` : "尚未收到"}
          </dd>
          <dt>合成演练执行数</dt>
          <dd data-testid="tts-executions">{transport.stats.executions}</dd>
          <dt>创建请求次数</dt>
          <dd data-testid="tts-attempts">{transport.stats.createKeys.length}</dd>
        </dl>
        <div className="file-demo-actions">
          <button
            type="button"
            disabled={state.busy || (!state.job && !state.asset && !state.assetRecoveryId)}
            onClick={() => session.refresh()}
          >
            {state.job ? "重新读取任务" : "重新读取素材"}
          </button>
          <button
            type="button"
            disabled={state.busy || !state.canReplay}
            onClick={() => session.replay()}
          >
            按原键重放
          </button>
          <button
            type="button"
            disabled={state.busy || !state.job}
            onClick={() => session.reconnect()}
          >
            读取任务事件
          </button>
          <button
            type="button"
            disabled={
              state.busy ||
              state.unknown ||
              !state.job ||
              ["succeeded", "failed", "cancelled", "expired"].includes(state.job.status)
            }
            onClick={() => session.cancel()}
          >
            请求取消
          </button>
        </div>
        <div className="file-demo-results">
          <h3>合成音频</h3>
          <p>试听与下载只读取本任务已提交的原始音频，不代表真实声音资格。</p>
          <audio
            ref={player}
            controls
            preload="none"
            aria-label="合成音频播放器"
            onEnded={() => setAudioNotice("试听已结束。")}
          />
          <div className="file-demo-actions">
            <button
              type="button"
              disabled={state.busy || !audioId}
              onClick={() => loadAudio(false)}
            >
              试听合成音频
            </button>
            <button type="button" disabled={state.busy || !audioId} onClick={() => loadAudio(true)}>
              下载合成音频
            </button>
          </div>
          {audioNotice && <p role="status">{audioNotice}</p>}
        </div>
      </section>
    </div>
  );
}

export default function TtsTaskDemo() {
  const [scenario, setScenario] = useState<TtsDemoScenario>("success");
  return (
    <div className="file-demo tts-demo">
      <AppShell
        surface="个人语音工作台"
        tone="workbench"
        eyebrow="SpeechNexus / Workbench"
        title="文本配音演练"
        description="开发演练 · 合成数据。验证固定声音版本、任务恢复及原始音频试听下载。"
        boundaryNote="部署内数据共享。本页仅在浏览器内存运行合成场景，切换场景会丢弃演练状态。"
        statusTitle="开发演练 / 契约已交付"
        statusDetail="提示音不代表真实 TTS 服务可用"
        statusState="contract"
      >
        <label className="file-demo-scenario">
          配音演练场景
          <select
            value={scenario}
            onChange={(event) => setScenario(event.target.value as TtsDemoScenario)}
          >
            <option value="success">正常完成</option>
            <option value="create-unknown">创建响应丢失</option>
            <option value="cancel-unknown">取消响应丢失</option>
            <option value="artifact-expired">音频过期</option>
          </select>
        </label>
        <DemoSession key={scenario} scenario={scenario} />
      </AppShell>
    </div>
  );
}
