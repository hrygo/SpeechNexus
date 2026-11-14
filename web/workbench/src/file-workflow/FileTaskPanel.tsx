import { type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { type FileTaskSession, unconfirmedAlertFor } from "./file-task-session";
import type { FileContracts } from "./speech-file-client";

import { canFinishTask } from "./file-task-policy";

type Snapshot = ReturnType<FileTaskSession["getSnapshot"]>;
export type ImportTranscript = (
  result: Extract<NonNullable<FileContracts["Job"]["result"]>, { kind: "asr" }>,
) => void;

export function FileTaskPanel({
  session,
  onNew,
  savedAssetId,
  result,
  onImportTranscript,
}: {
  session: FileTaskSession;
  onNew: () => void;
  savedAssetId: string | null;
  result?: ReactNode;
  onImportTranscript?: ImportTranscript | undefined;
}) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const job = state.job;
  const asrResult = job?.kind === "asr" && job.result?.kind === "asr" ? job.result : null;
  const canImport =
    !!asrResult &&
    job?.status === "succeeded" &&
    !state.busy &&
    !state.unknown &&
    job.artifacts.some(
      (artifact) =>
        artifact.artifact_id === asrResult.transcript_artifact_id &&
        artifact.receipt_id === asrResult.receipt_id &&
        artifact.kind === "transcript",
    );
  const [prepared, setPrepared] = useState<Snapshot["job"]>(undefined);
  const [preparingNew, setPreparingNew] = useState(false);
  const [downloadNotice, setDownloadNotice] = useState("");
  const confirmButton = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);
  const cancellable =
    !!job &&
    !state.busy &&
    !state.unknown &&
    // A settled cancel intent cannot be resent (#723); the page offers explicit finish.
    !state.rejected &&
    !["succeeded", "failed", "cancelled", "expired"].includes(job.status);
  const confirming = prepared !== undefined && prepared === job && cancellable;
  const finishing = preparingNew && prepared === job && canFinishTask(state);
  useEffect(() => {
    if (confirming || finishing) confirmButton.current?.focus();
  }, [confirming, finishing]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  async function download(id: string) {
    setDownloadNotice("");
    const result = await session.download(id);
    if (!result || !alive.current) return;
    if (
      result.artifact.kind !== "transcript" ||
      result.artifact.media_type !== "application/json"
    ) {
      setDownloadNotice("制品格式与本次原始 JSON 请求不一致，请重新读取任务。");
      return;
    }
    const url = URL.createObjectURL(result.blob);
    const anchor = document.createElement("a");
    try {
      anchor.href = url;
      anchor.download = "transcript.json";
      document.body.append(anchor);
      anchor.click();
      setDownloadNotice("已交给浏览器下载原始制品。");
    } finally {
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }
  }
  return (
    <section
      className="file-task-detail"
      aria-labelledby="file-detail-title"
      aria-busy={state.busy}
    >
      <h2 id="file-detail-title">任务与原始结果</h2>
      <p role="status">{state.notice}</p>
      {/* The alert's action list is generated from the same read handle `refresh()` uses, so
          it never asks for a read this state cannot perform (#780). */}
      {state.unknown && (
        <p role="alert">
          {unconfirmedAlertFor({ read: state.readTarget, replay: state.canReplay })}
        </p>
      )}
      {state.rejected && !state.unreplayable && (
        <p role="status">
          原请求已被协议确定性拒绝，原意图作废：不能重放，刷新也不会再变回「结果未知」。
          读取原任务核对后，可用「准备新任务」明确结束；服务端数据不会因此删除。
        </p>
      )}
      {state.rejected && state.unreplayable && (
        <p role="status">
          原请求因请求体超过本部署的接收上限被拒绝，这次请求被拒绝、原意图作废：不能按原键重放，
          刷新后仍保持作废。本页只能明确结束并重新开始；服务端数据不会因此删除。
        </p>
      )}
      <dl>
        <dt>素材</dt>
        <dd>{state.asset?.id ?? state.assetRecoveryId ?? savedAssetId ?? "尚未确认"}</dd>
        <dt>素材状态</dt>
        <dd>{state.asset?.status ?? "尚未确认"}</dd>
        <dt>任务</dt>
        <dd>{job?.id ?? "尚未确认"}</dd>
        <dt>任务状态</dt>
        <dd data-testid="file-job-status">{job?.status ?? "尚未确认"}</dd>
        {job?.kind === "tts" && (
          <>
            <dt>实际模型修订</dt>
            <dd>
              {job.effective_configuration.models.tts
                ? `${job.effective_configuration.models.tts.id}@${job.effective_configuration.models.tts.revision}`
                : "尚未确认"}
            </dd>
            <dt>实际声音修订</dt>
            <dd>
              {job.effective_configuration.voice
                ? `${job.effective_configuration.voice.id}@${job.effective_configuration.voice.revision}`
                : "尚未确认"}
            </dd>
            <dt>实际参数修订</dt>
            <dd>
              {job.effective_configuration.profile
                ? `${job.effective_configuration.profile.id}@${job.effective_configuration.profile.revision}`
                : "尚未确认"}
            </dd>
          </>
        )}
        <dt>资源清理</dt>
        <dd>{job?.cleanup_state ?? "尚无证据"}</dd>
        <dt>原执行</dt>
        <dd>{state.operation?.id ?? state.recovery?.error.nexus?.operation_id ?? "无"}</dd>
        <dt>执行状态</dt>
        <dd>
          {state.operation
            ? `${state.operation.status} / ${state.operation.cleanup_state}`
            : "尚未读取"}
        </dd>
        <dt>事件连接</dt>
        <dd>{state.stream}</dd>
        <dt>已确认事件水位</dt>
        <dd data-testid="file-event-cursor">{state.cursor ?? "无"}</dd>
      </dl>
      {job?.error && <p role="alert">任务失败：{job.error.code}。资源清理仍需单独核对。</p>}
      {state.cancellation && (
        <p>
          取消响应：accepted={String(state.cancellation.accepted)} /{" "}
          {state.cancellation.cleanup_state}
        </p>
      )}
      <div className="file-task-actions">
        {/* Disabled exactly when `refresh()` would send no request: the crash window has no
            readable handle, and the alert may not name a read that cannot happen (#780). */}
        <button
          type="button"
          disabled={state.busy || state.readTarget === null}
          onClick={() => void session.restore()}
        >
          读取原状态
        </button>
        <button
          type="button"
          disabled={state.busy || !state.canReplay}
          onClick={() => void session.replay()}
        >
          按原键重放
        </button>
        {state.asset?.status === "ready" && !job && !state.unknown && !state.rejected && (
          <button
            type="button"
            disabled={state.busy}
            onClick={() => void session.continueAfterValidation()}
          >
            继续创建原任务
          </button>
        )}
        <button
          type="button"
          disabled={state.busy || !job || state.stream === "connecting"}
          onClick={() => void session.reconnect()}
        >
          连接任务事件
        </button>
        <button
          type="button"
          disabled={state.stream !== "connecting"}
          onClick={() => session.stopEvents()}
        >
          停止事件连接
        </button>
        <button
          type="button"
          disabled={!cancellable}
          onClick={() => {
            setPreparingNew(false);
            setPrepared(job);
          }}
        >
          准备取消
        </button>
      </div>
      {confirming && job && (
        <div role="group" aria-label="确认取消原任务">
          <p>取消任务 {job.id}？取消响应不代表执行已停止或资源已释放。</p>
          <button
            type="button"
            ref={confirmButton}
            onClick={() => {
              if (prepared === session.getSnapshot().job) void session.cancel();
              setPrepared(undefined);
            }}
          >
            确认取消 {job.id}
          </button>
          <button type="button" onClick={() => setPrepared(undefined)}>
            返回任务
          </button>
        </div>
      )}
      {result ?? (
        <section aria-label="原始转写制品">
          <h3>原始转写制品</h3>
          {onImportTranscript && asrResult && (
            <button
              type="button"
              disabled={!canImport}
              onClick={() => {
                if (canImport) onImportTranscript(asrResult);
              }}
            >
              导入到项目稿件
            </button>
          )}
          <p>下载本任务已提交的原始 JSON，单个制品上限 16 MiB。</p>
          {!job?.artifacts.length && <p>尚无已提交制品。</p>}
          {job?.artifacts
            .filter((artifact) => artifact.kind === "transcript")
            .map((artifact) => (
              <button
                type="button"
                key={artifact.artifact_id}
                disabled={state.busy}
                onClick={() => void download(artifact.artifact_id)}
              >
                下载原始制品 {artifact.artifact_id}
              </button>
            ))}
          {downloadNotice && <p role="status">{downloadNotice}</p>}
        </section>
      )}
      {canFinishTask(state) && (
        <button
          type="button"
          onClick={() => {
            setPrepared(job);
            setPreparingNew(true);
          }}
        >
          准备新任务
        </button>
      )}
      {finishing && (
        <div role="group" aria-label="确认本页新任务">
          <p>
            {state.rejected && state.unreplayable
              ? "这次请求被拒绝，原意图作废。结束本页记录并重新开始？新任务会使用新的幂等键，不会删除服务端数据。"
              : state.rejected
                ? "原请求已被协议确定性拒绝，这次意图已作废。结束本页记录并重新开始？新任务会使用新的幂等键，不会删除服务端数据。"
                : state.uploadRejected
                  ? "原上传已明确拒绝。结束本页输入并重新选择？不会删除服务端数据。"
                  : "结束本页任务恢复并新建？服务器上的原任务与制品继续保留。"}
          </p>
          <button
            type="button"
            ref={confirmButton}
            onClick={() => {
              if (prepared === session.getSnapshot().job && canFinishTask(session.getSnapshot()))
                onNew();
              setPreparingNew(false);
            }}
          >
            {state.rejected ? "放弃这次意图并新建" : "结束本页任务并新建"}
          </button>
          <button type="button" onClick={() => setPreparingNew(false)}>
            保留本页任务
          </button>
        </div>
      )}
    </section>
  );
}
