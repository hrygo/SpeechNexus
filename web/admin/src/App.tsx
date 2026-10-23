import { AppShell, CapabilityCard } from "@speechnexus/shared";

export function App() {
  return (
    <AppShell
      surface="AdminUI"
      tone="admin"
      eyebrow="SpeechNexus / Admin"
      title="运行事实管理"
      description="这里将承载模型绑定、资源预算、任务维护与诊断。机器契约已交付，当前版本提供可构建、可部署的入口外壳，业务页面会随 P1 服务闭环逐步接入。"
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
          description="业务路由尚未实现。OpenAI 兼容子集、音色、Job、预设及管理资源的机器契约已冻结并通过门禁。"
          state="contract"
        />
        <CapabilityCard
          label="03 / Operations"
          title="资源与任务视图"
          description="队列、租约、outbox、计量和诊断页面依赖 P1 的 PostgreSQL 权威路径，当前不展示虚构运行数据。"
          state="planned"
        />
      </section>
    </AppShell>
  );
}
