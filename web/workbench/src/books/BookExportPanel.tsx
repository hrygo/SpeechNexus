import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Contracts, WorkbenchClient } from "../manuscripts/workbench-client";
import {
  BookExportSession,
  isTerminal,
  type BookExportProblem,
  type ExportFormat,
} from "./book-export";
import "./books.css";
import { ContentDownload } from "./ContentDownload";

const problems: Record<BookExportProblem, string> = {
  storage: "恢复信息未能保存，已暂停提交。请修复浏览器存储后重试。",
  unavailable: "导出服务当前不可用。作业状态尚未确认，请按原键重试，不要重新创建。",
  conflict: "服务端版本或幂等键冲突。请重新读取书目修订后再发起导出。",
  rejected: "本次导出请求已被拒绝。请核对导出格式与选择范围后重试。",
  missing: "该导出或书目已不可读取。",
  unknown: "提交结果尚未确认。已保留本次请求键，请按原键重放。",
};

const formats: readonly ExportFormat[] = ["zip", "wav", "mp3"];

export function BookExportPanel({
  client,
  book,
  pollMs,
}: {
  client: WorkbenchClient;
  book: Contracts["Book"];
  pollMs?: number;
}) {
  const session = useMemo(
    () => new BookExportSession(client, book, pollMs === undefined ? {} : { pollMs }),
    [client, book, pollMs],
  );
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const [format, setFormat] = useState<ExportFormat>("zip");
  const mounted = useRef(new WeakMap<BookExportSession, number>());

  useEffect(() => {
    const lifetimes = mounted.current;
    const generation = (lifetimes.get(session) ?? 0) + 1;
    lifetimes.set(session, generation);
    return () => {
      // A StrictMode remount re-runs this effect before the microtask, so only a real
      // teardown disposes the session it shares with the remount.
      queueMicrotask(() => {
        if (lifetimes.get(session) === generation) session.dispose();
      });
    };
  }, [session]);

  const job = state.job;
  const artifact = job?.artifact ?? null;
  const failure = job?.error ?? null;
  const settled = job !== null && isTerminal(job.status);
  const inflight = job !== null && !settled;
  const locked = state.busy || inflight || state.pending !== null || state.blocked;

  return (
    <section className="books" aria-label="有声书导出">
      <h2>有声书导出</h2>
      <p>
        导出固定当前书目修订 <code>{book.casting_revision}</code>（{book.title}
        ）。导出的内容由服务端生成并发布到对象区，完成后可校验并下载。
      </p>
      <div className="books-panel">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (locked) return;
            void session.start(format, crypto.randomUUID());
          }}
        >
          <label>
            导出格式
            <select
              disabled={locked}
              value={state.pending?.kind === "create" ? state.pending.body.format : format}
              onChange={(event) => setFormat(event.target.value as ExportFormat)}
            >
              {formats.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={locked}>
            发起导出
          </button>
          {inflight && (
            <button
              type="button"
              disabled={state.busy}
              onClick={() => void session.cancel(crypto.randomUUID())}
            >
              取消导出
            </button>
          )}
        </form>

        {state.pending && (
          <aside aria-label="导出原请求恢复">
            <p>原请求已保存在此标签页，不能切换意图，也不会自动重放。</p>
            {state.pending.kind === "create" && (
              <p>
                固定修订 {state.pending.body.expected_casting_revision} · 格式{" "}
                {state.pending.body.format}
              </p>
            )}
            <button type="button" disabled={state.busy} onClick={() => void session.replay()}>
              按原键重放导出请求
            </button>
            {state.rejected && (
              <button type="button" onClick={() => session.dismissRejected()}>
                结束已拒绝导出请求
              </button>
            )}
          </aside>
        )}
        {state.problem !== null && (
          <p className="books-problem" role="alert">
            {problems[state.problem]}
          </p>
        )}

        {job !== null && (
          <div aria-live="polite">
            <h3>作业状态：{job.status}</h3>
            <progress value={job.progress_completed} max={job.progress_total} />
            <p>
              已完成 {job.progress_completed} / {job.progress_total} 个单元；固定修订{" "}
              <code>{job.manifest_revision}</code>。
            </p>
            {job.status === "succeeded" && artifact !== null && (
              <div className="books-artifact">
                <p>
                  产物已发布：{artifact.media_type}，{artifact.bytes} 字节。
                </p>
                <p>
                  摘要 <code>{artifact.sha256}</code>。
                </p>
                <ContentDownload
                  key={job.id + artifact.sha256}
                  identity={job.id + artifact.sha256}
                  filename={`${job.id}.${job.format}`}
                  label="校验并下载导出"
                  load={(signal) => client.downloadBookExport(job, signal)}
                />
              </div>
            )}
            {job.status === "failed" && failure !== null && (
              <p>
                导出失败：<code>{failure.code}</code>（{failure.message}）
                {failure.retryable ? "；可重试。" : "；不可重试。"}
              </p>
            )}
            {job.status === "cancelled" && <p>导出已取消，产物未发布。</p>}
            {job.status === "expired" && <p>产物已过期并被清理，manifest 修订仍保留。</p>}
            {job.status === "cancelling" && <p>取消已受理，正在等待生产停止。</p>}
          </div>
        )}
      </div>
    </section>
  );
}
