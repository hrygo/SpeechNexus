import { useEffect, useRef, useState } from "react";
import { AdminJobClient, type TaskApi } from "./job-client";
import { TaskCenter } from "./TaskCenter";
import { TaskCenterSession } from "./task-center-session";
import { browserTaskRecovery } from "./task-recovery";

/** The production host uses the deployment's own origin; reads begin with a user action. */
export function TaskCenterHost({ api }: { api?: TaskApi }) {
  const [session] = useState(
    () => new TaskCenterSession(api ?? new AdminJobClient(location.origin), browserTaskRecovery()),
  );
  const mount = useRef({ generation: 0 });
  useEffect(() => {
    const owner = mount.current;
    const generation = ++owner.generation;
    const stop = () => session.stopEvents();
    const visibility = () => {
      if (document.hidden) stop();
    };
    window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("pagehide", stop);
      document.removeEventListener("visibilitychange", visibility);
      // StrictMode replays effects without destroying this stateful session.
      queueMicrotask(() => {
        if (owner.generation === generation) session.dispose();
      });
    };
  }, [session]);
  return (
    <section aria-label="当前部署任务中心">
      <p>
        任务中心使用当前部署的公开
        API；请显式读取。真实服务联调待完成，读取失败时保留可核对的旧快照。
      </p>
      <TaskCenter session={session} />
    </section>
  );
}
