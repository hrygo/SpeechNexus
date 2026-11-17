# 运维与部署文档

本目录收录部署配置、操作、恢复和发布说明。具体组件的协议与实现见[组件说明](../components/README.md)；操作前核对各文的前置条件与实际部署状态。

## 文件语音与存储

- [文件 ASR 默认执行装配](file-asr-runtime.md)及[固定制品与参数交接](file-asr-binding-handoff.md)。
- [文件 TTS 默认执行装配](file-tts-runtime.md)及[固定制品与参数交接](file-tts-binding-handoff.md)。
- [同步文件转写的生产装配](synchronous-transcription.md)。
- [生产协调存储与权限基线](production-coordination-storage.md)。
- [runtime 意图持久投递 worker](production-runtime-delivery.md)。

## 部署、恢复与发布

- [最小 Compose 部署](compose-minimal.md) · [本地端到端历史记录](../archive/operations/local-end-to-end.md) · [注册引擎](register-engine.md)。
- [配置前提扫描](configuration-prerequisites.md)：配置了却不生效的键、当前表现与元守护边界。
- [服务前缀环境变量的启动校验](environment-key-prefixes.md)：拼错或配错入口的前缀变量在启动时点名拒绝/告警。
- [启动闸门的失败输出形态](startup-gate-output.md)：每个单元的闸门阶段、实测是一行 `FAIL:` 还是 traceback、退出码几、文档承诺与处置。
- [参考部署异步 Job 的六项前置与自检](async-job-runbook.md) · 只读自检 `scripts/dev-env/check-async-job-prereqs.py`。
- [从空库按文档起参考部署的执行记录](compose-minimal-from-zero.md)：2026-11-16 实测到引擎注册前，含五类缺前置表现与缺口清单。
- [参考部署一条命令验收](reference-acceptance.md) · `scripts/dev-env/reference-acceptance.py`（工具/迁移/角色/桶/栈/启动闸门/健康）。
- [容量清单](capacity-manifest.md) · [资格报告](qualification-report.md) · [内建声音资格](builtin-voice-qualification.md)。
- [对账与恢复](reconciliation-recovery.md) · [上线前检查](release-preflight.md) · [发布就绪历史快照](../archive/operations/release-readiness.md)。
- [前后端同版核对](same-version-check.md)：运行期契约摘要告警、`GET /v1/nexus/version`，以及两侧都早于该校验时的人工核对清单。
- [绑定发布](publish-binding-revision.md) · [绑定生命周期](binding-lifecycle.md) · [停用 worker](binding-disable-worker.md)。
- [绑定发布证据回填](backfill-binding-publication-evidence.md) · [激活幂等与同版验证记录](binding-activation-verification.md)。
- [取消/中止 oracle 的本地双库验证](local-dual-database.md)。
- [多 Agent 交付 SOP](agent-delivery-sop.md) · [多 Agent 协作与角色](agent-collaboration.md) · [共用边界简报](roles/common.md)。

[文档索引](../README.md)
