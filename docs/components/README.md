# 组件与实现说明

本目录记录具体组件的接口、事务、装配与实现边界。设计目标与实际交付需结合各文状态、[架构设计](../architecture/README.md)和[交付跟踪矩阵](../plans/delivery-tracking.md)判断；历史验证仅覆盖其记录的版本与环境。部署、凭据和日常操作入口见[运维文档](../operations/README.md)。

## 执行网关与节点

- [执行网关：提交、屏障与未知执行恢复](execution-gateway.md)
- [执行网关客户端（传输层，不是准入层）](gateway-execution-client.md)
- [受限节点控制器：接口、规则与当前实现](node-controller.md)
- [节点停止检查点与进程崩溃恢复](node-stop-recovery.md)
- [Runtime 控制器 HTTPS 客户端](runtime-controller-client.md)
- [节点停止事实到网关的可信消费](trusted-node-stop.md)
- [执行结果回执的持久登记与查询](execution-staging-results.md)
- [原执行命令与核验文件输入](frozen-execution-inputs.md)
- [受控文件 ASR 的原生鉴权与代次关联](controlled-asr-generation.md)
- [零外发契约核心候选（DEL-417）](zero-dispatch-contract-core.md)（候选；未启用）

## 准入、协调与持久化

- [完整配方准入的独立事务](production-admission.md)
- [完整准入的私有命令存储](production-admission-storage.md)
- [生产 Job/outbox 与 worker 认领](production-jobs.md)
- [生产租约隔离与释放事务](production-leases.md)
- [生产 owner 任期事务](production-owners.md)
- [Runtime 意图存储与未观察快照](production-runtime-intents.md)
- [生产资产发布单元](production-assets.md)

## 文件语音与媒体

- [音频资产检查边界与历史候选](../archive/components/audio-asset-inspection.md)
- [CPU VAD：有界切段与会话状态](cpu-vad.md)
- [文件 ASR 有界媒体输入](file-asr-audio.md)
- [文件 ASR 原生识别接口](file-asr-recognition.md)
- [文件 ASR 私有结果交接](file-asr-results.md)
- [文件 TTS 原生合成接口](file-tts.md)
- [同步语音合成：持久命令与恢复](synchronous-speech.md)

## 资源声明与配置

- [Binding 默认快照生产者 HTTP 接口](binding-defaults-producer.md)
- [Binding 参数默认值快照](binding-parameter-defaults.md)
- [不可变模型绑定草稿](model-binding-drafts.md)
- [不可变配置预设草稿](profile-drafts.md)
- [配置预设发布与激活契约前检](profile-lifecycle-contract.md)
- [配置预设生命周期私有意图账本](profile-lifecycle-intents.md)
- [不可变发音草稿](pronunciation-drafts.md)
- [固定发音版本读取契约](pronunciation-read-contract.md)
- [Public TTS Job 的固定 binding 前置链](public-job-model-binding.md)

## 工作台与工程布局

- [ASR 原稿导入与固定修订文本下载](transcript-import-and-text-export.md)
- [工作台已提交内容下载](workbench-content-downloads.md)
- [工作台导出发布前完整性检查](workbench-export-integrity.md)
- [工作台公开音色目录读取](workbench-voice-catalogue.md)
- [SpeechNexus 工程目录与依赖边界](repository-layout.md)

[文档索引](../README.md)
