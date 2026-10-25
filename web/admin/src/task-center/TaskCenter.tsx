import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Job } from "./job-client";
import type { TaskCenterSession, TaskCenterSnapshot } from "./task-center-session";
import "./task-center.css";

function TaskDetail({
  session,
  snapshot,
}: {
  session: TaskCenterSession;
  snapshot: TaskCenterSnapshot;
}) {
  const [prepared, setPrepared] = useState<Job | null>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const detail = snapshot.detail;
  const confirming = prepared !== null && prepared === detail && session.canCancel();
  useEffect(() => {
    if (confirming) confirmButton.current?.focus();
  }, [confirming]);
  return (
    <section
      className="task-center-detail"
      aria-labelledby="task-detail-heading"
      aria-busy={snapshot.detailPhase === "loading"}
    >
      <h2 id="task-detail-heading">任务详情</h2>
      {snapshot.detailPhase === "loading" && <p role="status">正在重读任务…</p>}
      {snapshot.detailPhase === "error" && (
        <p role="alert">详情读取失败；以下保留的快照不代表当前状态。</p>
      )}
      {!snapshot.selectedId && <p>选择任务后读取详情。</p>}
      {snapshot.selectedId && !detail && <p>待核对任务：{snapshot.selectedId}</p>}
      <section aria-label="所选任务事件">
        <h3>事件更新</h3>
        <p role="status" data-testid="task-event-state">
          {snapshot.streamNotice}
        </p>
        <p>
          已确认事件水位：
          <span data-testid="task-event-cursor">{snapshot.cursor ?? "无已确认水位"}</span>
        </p>
        {!session.eventsAvailable() && (
          <p>
            {snapshot.recovery === "unavailable"
              ? "恢复信息未确认，事件连接已暂停；可重读任务快照。"
              : "当前入口未接入事件端口，可重读任务快照。"}
          </p>
        )}
        <div className="task-center-actions">
          <button
            type="button"
            disabled={
              !session.eventsAvailable() ||
              !detail ||
              snapshot.detailPhase !== "ready" ||
              snapshot.stream === "connecting" ||
              snapshot.stream === "listening"
            }
            onClick={() => {
              setPrepared(null);
              void session.connectEvents();
            }}
          >
            连接任务事件
          </button>
          <button
            type="button"
            disabled={snapshot.stream !== "connecting" && snapshot.stream !== "listening"}
            onClick={() => session.stopEvents()}
          >
            停止事件连接
          </button>
        </div>
      </section>
      {detail && (
        <>
          <dl>
            <dt>任务标识</dt>
            <dd data-testid="task-detail-id">{detail.id}</dd>
            <dt>任务类型</dt>
            <dd>{detail.kind}</dd>
            <dt>任务状态</dt>
            <dd data-testid="task-detail-status">{detail.status}</dd>
            <dt>资源清理</dt>
            <dd data-testid="task-cleanup">{detail.cleanup_state}</dd>
            <dt>工作空间</dt>
            <dd>{detail.workspace_id}</dd>
            <dt>执行版本</dt>
            <dd>{detail.execution_revision}</dd>
            <dt>快照更新时间</dt>
            <dd>{detail.updated_at}</dd>
          </dl>
          {detail.error && (
            <p role="alert">任务失败：{detail.error.code}。资源清理仍以当前快照为准。</p>
          )}
          <p>任务终态与资源释放分别核对；取消请求不构成停止证据。</p>
        </>
      )}
      <div className="task-center-actions">
        <button
          type="button"
          disabled={!snapshot.selectedId || snapshot.detailPhase === "loading"}
          onClick={() => {
            setPrepared(null);
            void session.refreshSelected();
          }}
        >
          重读所选任务
        </button>
        <button type="button" disabled={!session.canCancel()} onClick={() => setPrepared(detail)}>
          准备取消
        </button>
      </div>
      {confirming && detail && (
        <div className="task-center-confirm" role="group" aria-label="确认取消目标">
          <p>确认取消任务 {detail.id}？</p>
          <p>此操作只发出取消请求；执行与资源释放仍需后续核实。</p>
          <button
            ref={confirmButton}
            type="button"
            onClick={() => {
              if (prepared === session.getSnapshot().detail) void session.cancelSelected();
              setPrepared(null);
            }}
          >
            确认取消本任务
          </button>
          <button type="button" onClick={() => setPrepared(null)}>
            返回详情
          </button>
        </div>
      )}
    </section>
  );
}

export function TaskCenter({ session }: { session: TaskCenterSession }) {
  const snapshot = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const { page, cancellation } = snapshot;
  return (
    <div className="task-center">
      <p
        role={snapshot.recovery === "unavailable" ? "alert" : "status"}
        data-testid="task-recovery-state"
      >
        {snapshot.recoveryNotice}
      </p>
      <p role="status">{snapshot.notice}</p>
      {cancellation && (
        <section className="task-center-cancellation" aria-label="取消请求证据">
          <h2>取消请求</h2>
          <p>原任务：{cancellation.jobId}</p>
          {cancellation.phase === "unknown" && (
            <p role="alert">
              取消结果未知；请核对原任务。重新读取任务不会自动重试取消，按原键重放仅针对下列原任务。
            </p>
          )}
          {cancellation.phase === "submitting" && <p role="status">正在提交原任务的取消请求…</p>}
          {cancellation.response && (
            <p>
              accepted={String(cancellation.response.accepted)} / {cancellation.response.status} /{" "}
              {cancellation.response.cleanup_state}
            </p>
          )}
          <div className="task-center-actions">
            <button type="button" onClick={() => void session.select(cancellation.jobId)}>
              重读取消原任务
            </button>
            <button
              type="button"
              disabled={cancellation.phase !== "unknown" || snapshot.recovery === "unavailable"}
              onClick={() => void session.replayCancellation()}
            >
              按原键重放取消
            </button>
          </div>
        </section>
      )}
      <div className="task-center-grid">
        <section
          className="task-center-list"
          aria-labelledby="task-list-heading"
          aria-busy={snapshot.listPhase === "loading"}
        >
          <h2 id="task-list-heading">任务列表</h2>
          <div className="task-center-actions">
            <button
              type="button"
              disabled={snapshot.listPhase === "loading"}
              onClick={() => void session.load()}
            >
              读取任务列表
            </button>
            <button
              type="button"
              disabled={snapshot.listPhase !== "ready" || !page?.has_more}
              onClick={() => {
                if (page?.next_cursor) void session.load(page.next_cursor);
              }}
            >
              下一页
            </button>
          </div>
          {snapshot.listPhase === "loading" && <p role="status">正在读取列表…</p>}
          {snapshot.listPhase === "error" && (
            <p role="alert">列表读取失败；上次快照已过时，请重新读取。</p>
          )}
          {snapshot.listPhase === "idle" && <p>读取部署内共享的任务快照。</p>}
          {page && page.data.length === 0 && <p>当前快照没有任务。可重新读取列表。</p>}
          {page && page.data.length > 0 && (
            <div className="task-center-table-wrap">
              <table>
                <caption>列表快照；选中任务后重读详情</caption>
                <thead>
                  <tr>
                    <th scope="col">任务</th>
                    <th scope="col">状态</th>
                    <th scope="col">清理</th>
                  </tr>
                </thead>
                <tbody>
                  {page.data.map((job) => (
                    <tr key={job.id} aria-selected={snapshot.selectedId === job.id}>
                      <td>
                        <button
                          type="button"
                          aria-label={`查看 ${job.id}`}
                          onClick={() => void session.select(job.id)}
                        >
                          {job.id}
                        </button>
                      </td>
                      <td>{job.status}</td>
                      <td>{job.cleanup_state}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <TaskDetail key={snapshot.selectedId} session={session} snapshot={snapshot} />
      </div>
    </div>
  );
}
