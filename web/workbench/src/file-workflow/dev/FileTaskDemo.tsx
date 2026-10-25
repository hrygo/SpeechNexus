import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AppShell } from "@speechnexus/shared";
import { FileTaskSession } from "../file-task-session";
import { SpeechFileClient } from "../speech-file-client";
import { createDemoTransport, type DemoScenario } from "./demo-transport";
import "./file-task-demo.css";

const scenarios: { id: DemoScenario; label: string }[] = [
  { id: "success", label: "正常完成" },
  { id: "persistent-events", label: "持久事件连接 / 取消" },
  { id: "operation-recovery", label: "原执行身份 / 查询恢复" },
  { id: "create-unknown", label: "创建响应丢失 / 原键重放" },
  { id: "cancel-unknown", label: "取消响应丢失 / 清理待确认" },
  { id: "conflict", label: "幂等冲突" },
  { id: "history-expired", label: "事件历史过期 / 重读" },
  { id: "artifact-expired", label: "制品过期" },
  { id: "failed", label: "任务失败 / 资源隔离" },
];

function syntheticFile() {
  const bytes = new ArrayBuffer(44 + 256 * 2);
  const view = new DataView(bytes);
  const ascii = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index++)
      view.setUint8(offset + index, value.charCodeAt(index));
  };
  ascii(0, "RIFF");
  view.setUint32(4, bytes.byteLength - 8, true);
  ascii(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 24000, true);
  view.setUint32(28, 48000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, 512, true);
  return new File([bytes], "synthetic-silence.wav", { type: "audio/wav" });
}

function DemoSession({ scenario }: { scenario: DemoScenario }) {
  const [{ session, transport }] = useState(() => {
    const transport = createDemoTransport(scenario);
    return {
      transport,
      session: new FileTaskSession(new SpeechFileClient(location.origin, transport.fetch)),
    };
  });
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [started, setStarted] = useState(false);
  const [downloaded, setDownloaded] = useState(false);
  const mount = useRef({ generation: 0 });
  useEffect(() => {
    const owner = mount.current;
    const generation = ++owner.generation;
    return () =>
      queueMicrotask(() => {
        // StrictMode reattaches effects synchronously; only a real unmount disposes the session.
        if (owner.generation === generation) session.dispose();
      });
  }, [session]);
  const job = state.job;
  const locked = state.busy;

  async function download(artifactId: string) {
    const result = await session.download(artifactId);
    if (!result) return;
    const url = URL.createObjectURL(result.blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "synthetic-transcript.json";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    setDownloaded(true);
  }

  return (
    <div className="file-demo-grid">
      <section className="file-demo-input" aria-labelledby="file-input-title">
        <h2 id="file-input-title">文件转写</h2>
        <p>使用 24kHz 合成静音文件演练上传。不会读取本机录音，也不会连接语音服务。</p>
        <dl>
          <dt>演练模型</dt>
          <dd>asr-quality（合成配置，非部署能力）</dd>
          <dt>输入</dt>
          <dd>synthetic-silence.wav / 单声道</dd>
          <dt>素材</dt>
          <dd>{state.asset?.status ?? "尚未上传"}</dd>
        </dl>
        <button
          type="button"
          disabled={locked || started}
          onClick={async () => {
            setStarted(true);
            await session.start(syntheticFile(), "asr-quality");
          }}
        >
          上传合成音频
        </button>
        {state.asset?.status === "ready" && !job && !state.unknown && (
          <button type="button" disabled={locked} onClick={() => session.continueAfterValidation()}>
            继续创建原任务
          </button>
        )}
        <p className="file-demo-support">
          校对保存与字幕导出等待工作台接口接入。下方下载保留原始合成制品。
        </p>
      </section>

      <section className="file-demo-task" aria-labelledby="file-task-title">
        <h2 id="file-task-title">任务详情与恢复</h2>
        <p role="status" className="file-demo-notice">
          {state.notice}
        </p>
        {state.unknown && (
          <p role="alert">
            {state.assetRecoveryId
              ? "原素材尚未确认；请只读查询原素材，不要创建替代上传。"
              : state.canReplay
                ? "结果未知；不要创建替代任务。请按原幂等键重放原意图。"
                : "请读取原执行状态，不要创建替代执行。"}
          </p>
        )}
        {state.assetRecoveryId && <p>原素材恢复标识：{state.assetRecoveryId}</p>}
        <dl className="file-demo-facts">
          <dt>任务</dt>
          <dd>{job?.id ?? "尚未确认"}</dd>
          <dt>状态</dt>
          <dd data-testid="job-status">{job?.status ?? "—"}</dd>
          <dt>资源清理</dt>
          <dd data-testid="cleanup-state">{job?.cleanup_state ?? "尚无证据"}</dd>
          <dt>取消响应</dt>
          <dd>
            {state.cancellation
              ? `accepted=${String(state.cancellation.accepted)} / ${state.cancellation.cleanup_state}`
              : "尚未收到"}
          </dd>
          <dt>恢复执行</dt>
          <dd>{state.operation?.id ?? state.recovery?.error.nexus?.operation_id ?? "无"}</dd>
          <dt>原执行状态</dt>
          <dd>
            {state.operation
              ? `${state.operation.status} / ${state.operation.cleanup_state}`
              : "尚未读取"}
          </dd>
          <dt>事件连接</dt>
          <dd>{state.stream}</dd>
          <dt>已确认事件水位</dt>
          <dd>{state.cursor ?? "无"}</dd>
          <dt>演练执行数</dt>
          <dd>{transport.stats.executions}（仅本地演练计数）</dd>
        </dl>
        {job?.error && <p role="alert">任务失败：{job.error.code}。资源清理仍以当前状态为准。</p>}
        <div className="file-demo-actions">
          <button
            type="button"
            disabled={locked || (!job && !state.asset && !state.recovery && !state.assetRecoveryId)}
            onClick={() => session.refresh()}
          >
            重新读取状态
          </button>
          <button
            type="button"
            disabled={locked || !state.canReplay}
            onClick={() => session.replay()}
          >
            按原幂等键重放
          </button>
          <button type="button" disabled={locked || !job} onClick={() => session.reconnect()}>
            连接或重连事件
          </button>
          <button
            type="button"
            disabled={
              locked ||
              state.unknown ||
              !job ||
              ["succeeded", "failed", "cancelled", "expired"].includes(job.status)
            }
            onClick={() => session.cancel()}
          >
            请求取消
          </button>
        </div>
        <div className="file-demo-results" aria-label="任务制品">
          <h3>原始结果</h3>
          {!job?.artifacts.length && <p>尚无已提交制品。</p>}
          {job?.artifacts.map((artifact) => (
            <button
              type="button"
              key={artifact.artifact_id}
              disabled={locked}
              onClick={() => download(artifact.artifact_id)}
            >
              下载原始制品 {artifact.artifact_id}
            </button>
          ))}
          {downloaded && <p role="status">已下载合成原始制品。</p>}
        </div>
      </section>
    </div>
  );
}

export default function FileTaskDemo() {
  const [scenario, setScenario] = useState<DemoScenario>("success");
  return (
    <div className="file-demo">
      <AppShell
        surface="个人语音工作台"
        tone="workbench"
        eyebrow="SpeechNexus / Workbench"
        title="文件转写演练"
        description="开发演练 · 合成数据。使用同版本契约客户端验证任务恢复与原始结果下载，生产业务能力尚待联调。"
        boundaryNote="当前部署共享数据，不代表服务端按用户私有保存。本页只运行浏览器内存中的合成场景，切换场景会丢弃演练状态。"
        statusTitle="开发演练 / 契约已交付"
        statusDetail="合成流程不代表真实语音服务可用"
        statusState="contract"
      >
        <label className="file-demo-scenario">
          故障场景
          <select
            value={scenario}
            onChange={(event) => {
              const selected = scenarios.find((item) => item.id === event.target.value);
              if (selected) setScenario(selected.id);
            }}
          >
            {scenarios.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <DemoSession key={scenario} scenario={scenario} />
      </AppShell>
    </div>
  );
}
