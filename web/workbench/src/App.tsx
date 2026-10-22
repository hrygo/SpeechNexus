import { AppShell, CapabilityCard } from "@speechnexus/shared";

export function App() {
  return (
    <AppShell
      surface="个人语音工作台"
      tone="workbench"
      eyebrow="SpeechNexus / Workbench"
      title="语音任务集中完成"
      description="这里将组织转写、配音、会议、助手、提词与有声书任务。当前版本先建立共享界面基线与可部署入口，任务数据模型和业务流程将在契约冻结后接入。"
      boundaryNote="“个人”描述任务体验，不代表服务端按用户私有保存。当前部署中的项目、音色与结果由所有工作台使用者共享，浏览器本地标识不能充当身份。"
    >
      <section className="card-grid" aria-label="工作台初始化范围">
        <CapabilityCard
          label="01 / Shell"
          title="两个产品入口"
          description="AdminUI 与工作台分别构建和部署，共用语义 Token、无障碍基线与状态表达，后续页面无需复制视觉常量。"
          state="ready"
        />
        <CapabilityCard
          label="02 / Speech"
          title="转写与配音"
          description="文件上传、任务恢复、音频采集与播放等待语音 API、Job 和实时事件契约完成后再实现。"
          state="contract"
        />
        <CapabilityCard
          label="03 / Projects"
          title="项目与续作"
          description="会议、提词、有声书和导出工作流位于可选工作台应用层，先验证底座路径，再逐步接入。"
          state="planned"
        />
      </section>
    </AppShell>
  );
}
