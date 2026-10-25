import { AppShell, CapabilityCard } from "@speechnexus/shared";
import { TaskCenterHost } from "./task-center/TaskCenterHost";
import { WorkspaceHost } from "./workspaces/WorkspaceHost";
import { DiagnosticsHost } from "./diagnostics/DiagnosticsHost";
import { ProfileHost } from "./profiles/ProfileHost";

export function App() {
  return (
    <AppShell
      surface="AdminUI"
      tone="admin"
      eyebrow="SpeechNexus / Admin"
      title="运行事实管理"
      description="读取共享工作空间、任务详情与持久事件，明确核对快照、取消响应和资源清理。部署诊断已可只读核对能力三态与内网模型绑定；预算、租约与计量仍待接入。"
      statusTitle="任务入口已接入"
      statusDetail="公开 API 消费已接入，真实部署待联调"
      statusState="contract"
      boundaryNote="首期没有登录与角色隔离。AdminUI 与工作台在同一部署内共享数据，入口切换只改变任务视图，管理 API 仍需由部署网络与入口网关限制。"
    >
      <section className="card-grid" aria-label="AdminUI 初始化范围">
        <CapabilityCard
          label="01 / Runtime"
          title="服务进程"
          description="FastAPI 应用、执行网关、严格配置与健康检查已经建立，可由平台探针分别检查存活和依赖状态。"
          state="ready"
        />
        <CapabilityCard
          label="02 / Contract"
          title="语音与管理协议"
          description="公共机器契约已交付；任务查询、取消和事件已消费同版本生成客户端与校验器，真实部署仍待联调。"
          state="contract"
        />
        <CapabilityCard
          label="03 / Operations"
          title="部署诊断"
          description="部署能力三态与内网模型绑定已可只读核对；预算、租约、outbox、计量与 GPU 事实尚未接入，没有可读来源前保持为空。"
          state="contract"
        />
      </section>
      <DiagnosticsHost />
      <WorkspaceHost />
      <ProfileHost />
      <TaskCenterHost />
    </AppShell>
  );
}
