import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { WorkspaceClient } from "./workspace-client";
import { WorkspaceSession } from "./workspace-session";
import "./workspace.css";
/** Production reads use this deployment's origin, never a user-supplied endpoint. */
export function WorkspaceHost({
  session,
  active = true,
}: {
  session?: WorkspaceSession;
  active?: boolean;
}) {
  const [c] = useState(() => session ?? new WorkspaceSession(new WorkspaceClient(location.origin)));
  const s = useSyncExternalStore(c.subscribe, c.snapshot);
  const life = useRef({ generation: 0 });
  const heading = useRef<HTMLHeadingElement>(null);
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
    if (s.detailCurrent) heading.current?.focus();
  }, [s.detailCurrent, s.detail?.id]);
  return (
    <section className="task-center workspace-read" aria-label="当前部署工作空间">
      <h2>共享工作空间</h2>
      <p>
        工作空间是当前部署共享的内容与预算分组，不构成用户或安全隔离。这里只读取元信息；配额、创建和修改尚未接入。
      </p>
      <p>
        请显式读取；列表和详情分别是读取时的快照。真实服务联调待完成，读取失败不会填入虚构工作空间。
      </p>
      <button type="button" disabled={!active || s.busy} onClick={() => void c.list()}>
        读取工作空间
      </button>
      {s.busy && <p role="status">正在读取工作空间…</p>}
      {s.error && <p role="alert">工作空间读取未确认：{s.error}。服务端错误正文不在此展示。</p>}
      {s.error && (
        <button type="button" disabled={!active || s.busy} onClick={() => void c.retry()}>
          重试原读取
        </button>
      )}
      {!s.page && <p>尚未读取工作空间</p>}
      {s.page && (
        <>
          <p>{s.pageCurrent ? "本次成功读取的列表快照" : "历史列表快照；请显式重读后继续分页"}</p>
          {s.page.data.length === 0 && <p>此页没有工作空间。</p>}
          <ul>
            {s.page.data.map((w) => (
              <li key={w.id}>
                <strong>{w.display_name}</strong>
                <span>
                  {w.id} · {w.status}
                </span>
                <button
                  type="button"
                  disabled={!active}
                  aria-label={`读取工作空间 ${w.id}`}
                  onClick={() => void c.get(w.id)}
                >
                  查看详情
                </button>
              </li>
            ))}
          </ul>
          <button
            type="button"
            disabled={!active || s.busy || !s.pageCurrent || !s.page.has_more}
            onClick={() => void c.list(s.page!.next_cursor!)}
          >
            读取下一页工作空间
          </button>
        </>
      )}
      {s.selectedId && <p>已选工作空间：{s.selectedId}</p>}
      {s.detail && (
        <div aria-label="工作空间详情快照">
          <h3 ref={heading} tabIndex={-1}>
            工作空间详情
          </h3>
          <p>{s.detailCurrent ? "本次成功读取的详情快照" : "历史详情快照；需显式重读"}</p>
          <dl>
            <dt>名称</dt>
            <dd>{s.detail.display_name}</dd>
            <dt>编号</dt>
            <dd>{s.detail.id}</dd>
            <dt>状态</dt>
            <dd data-testid="workspace-status">{s.detail.status}</dd>
            <dt>元信息版本</dt>
            <dd data-testid="workspace-metadata-version">{s.detail.metadata_revision}</dd>
            <dt>策略版本</dt>
            <dd>{s.detail.policy_revision}</dd>
            <dt>创建时间</dt>
            <dd>{s.detail.created_at}</dd>
          </dl>
        </div>
      )}
    </section>
  );
}
