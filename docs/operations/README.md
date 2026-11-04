# 运维与部署文档

本目录收录部署配置、操作、恢复和发布说明。具体组件的协议与实现见[组件说明](../components/README.md)；操作前核对各文的前置条件与实际部署状态。

## 文件语音与存储

- [文件 ASR 默认执行装配](file-asr-runtime.md)及[固定制品与参数交接](file-asr-binding-handoff.md)。
- [文件 TTS 默认执行装配](file-tts-runtime.md)及[固定制品与参数交接](file-tts-binding-handoff.md)。
- [同步文件转写的生产装配](synchronous-transcription.md)。
- [生产协调存储与权限基线](production-coordination-storage.md)。
- [runtime 意图持久投递 worker](production-runtime-delivery.md)。

## 部署、恢复与发布

- [最小 Compose 部署](compose-minimal.md) · [本地端到端历史记录](local-end-to-end.md) · [注册引擎](register-engine.md)。
- [容量清单](capacity-manifest.md) · [资格报告](qualification-report.md) · [内建声音资格](builtin-voice-qualification.md)。
- [对账与恢复](reconciliation-recovery.md) · [上线前检查](release-preflight.md) · [发布就绪历史快照](../archive/operations/release-readiness.md)。
- [绑定发布](publish-binding-revision.md) · [绑定生命周期](binding-lifecycle.md) · [停用 worker](binding-disable-worker.md)。
- [绑定发布证据回填](backfill-binding-publication-evidence.md) · [激活幂等与同版验证记录](binding-activation-verification.md)。
- [Agent 交付 SOP](agent-delivery-sop.md) · [Paperclip 工作流](paperclip.md)。

[文档索引](../README.md)
