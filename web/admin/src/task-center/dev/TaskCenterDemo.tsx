import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AppShell, CapabilityCard } from "@speechnexus/shared";
import { AdminJobClient } from "../job-client";
import { TaskCenterSession } from "../task-center-session";
import { TaskCenter } from "../TaskCenter";
import { createTaskDemoTransport, type TaskScenario } from "./demo-transport";

const scenarios: { id: TaskScenario; label: string }[] = [
  { id: "normal", label: "列表与分页" },
  { id: "cancel-unknown", label: "取消响应丢失" },
  { id: "empty", label: "空列表" },
  { id: "list-failed", label: "列表读取失败" },
  { id: "job-failed", label: "失败任务 / 隔离资源" },
  { id: "events", label: "事件水位重连" },
  { id: "events-expired", label: "事件历史过期" },
  { id: "events-disconnected", label: "事件连接中断" },
  { id: "events-wait", label: "等待事件与停止" },
];
function DemoSession({ scenario }: { scenario: TaskScenario }) {
  const [{ session, transport }] = useState(() => {
    const transport = createTaskDemoTransport(scenario);
    return {
      transport,
      session: new TaskCenterSession(new AdminJobClient(location.origin, transport.fetch)),
    };
  });
  useSyncExternalStore(session.subscribe, session.getSnapshot);
  const mount = useRef({ generation: 0 });
  useEffect(() => {
    const owner = mount.current;
    const generation = ++owner.generation;
    return () =>
      queueMicrotask(() => {
        if (owner.generation === generation) session.dispose();
      });
  }, [session]);
  const keys = transport.stats.cancelKeys;
  return (
    <>
      <TaskCenter session={session} />
      <p data-testid="synthetic-cancel-count">本地演练取消提交次数：{keys.length}</p>
      {keys.length > 1 && (
        <p data-testid="synthetic-cancel-identity">
          同键同目标重放：
          {keys.every((key) => key === keys[0]) &&
          transport.stats.cancelTargets.every((id) => id === transport.stats.cancelTargets[0])
            ? "一致"
            : "不同"}
        </p>
      )}
    </>
  );
}

export default function TaskCenterDemo() {
  const [scenario, setScenario] = useState<TaskScenario>("normal");
  return (
    <AppShell
      surface="AdminUI"
      tone="admin"
      eyebrow="SpeechNexus / Admin"
      title="任务状态演练"
      description="开发演练 · 合成数据。任务列表、详情与取消仅在本页内存运行，不连接真实服务。"
      boundaryNote="当前部署共享数据，没有登录或角色隔离。切换场景会清除本页合成状态；这不是已启用的生产管理入口。"
      statusTitle="开发演练 / 契约已交付"
      statusDetail="合成任务不代表部署能力或算力资格"
      statusState="contract"
    >
      <div className="task-center-demo">
        <label>
          演练场景
          <select
            value={scenario}
            onChange={(event) => {
              const next = scenarios.find((s) => s.id === event.target.value);
              if (next) setScenario(next.id);
            }}
          >
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <section className="card-grid" aria-label="管理能力接入范围">
          <CapabilityCard
            label="任务"
            title="公开任务协议"
            description="使用同版本生成客户端校验列表、详情和取消响应；真实部署接入仍待联调。"
            state="contract"
          />
          <CapabilityCard
            label="资源"
            title="绑定与预算视图"
            description="等待真实能力、实例和容量快照，不显示合成 GPU 指标。"
            state="planned"
          />
        </section>
        <DemoSession key={scenario} scenario={scenario} />
      </div>
    </AppShell>
  );
}
