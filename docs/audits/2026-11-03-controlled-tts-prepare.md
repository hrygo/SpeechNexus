---
title: 受控 TTS prepare 失败与资源收尾
status: incomplete
date: 2026-11-03
---

# 受控 TTS prepare 失败与资源收尾

本轮未取得可用声音资格，也未完成公开同步 TTS 验收。固定候选为
`90f53c4ead3236ccba61bc353259dc7f05e3a179`；全部 2002 个 Git blob 与远端隔离源码一致，
固定模型制品的离线核验通过。这些检查不能替代真实合成。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 模型 | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`，Vivian；revision `0c0e3051f131929182e2c023b9537f8b1c68adfe` |
| 原始 artifact pin SHA-256 | `e6c898aab968ddadae2ce330437032265b5d8192c23ec4f10c4d1b225296e779` |
| 镜像 | `sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52` |
| 新增实例 | 单实例、generation 1，已核对内存硬上限 12 GiB |
| prepare 开始 | 2026-11-03 00:18:45.501760 UTC |
| prepare 终态 | 00:21:58.144257 UTC：`failed` / `instance_prepare_failed` / `stopped` |
| 原停止意图 | 00:22:16.582013 UTC：`succeeded`，容器退出和资源释放双事实均为 true，退出码 1 |
| 控制器内存观察字段 | `observed_peak_memory.unified_memory_bytes=3226693632`；不是最终 OOM 原因或真实峰值的充分证据 |
| 实际测量 | 尚未进入 native profile 测量；无 WAV、无标准 SDK 成功响应 |
| 正式账本 | 来源批准 1、binding 草稿 1；声音批准、声音资格、publication、runtime head、admission attempt、lease 均为 0 |

原始请求/响应的 JSON 内容、各原文件摘要及清理结果保存在
[结构化证据](evidence/2026-11-03-tts-prepare.json)。这是失败与物理停止证据，
不能计作 READY、声音资格、正式预算结算或公开业务链通过。

## 诊断边界与待验证配置

失败模板使用 `--max-model-len 4096`，未显式限制 `max-num-seqs` 或启用 eager。
只读检查的现有同镜像、同 12 GiB 服务使用 `--max-model-len 1536`、
`--max-num-seqs 1`、`--enforce-eager`。这个差异支持一次保守配置对照，
不能据此认定失败由图编译、并发、上下文长度或内存不足导致。

失败容器在原停止流程中被移除前，没有保全有界引擎 stdout/stderr，因而根因尚未证实。
后续驱动必须在等待期间及停止前采集有界私有日志；不能用退出码或健康检查猜测原因。

保守配置只完成了模板解析和任务专用 CPU 控制器加载；未启动 generation 2，
也未启动 generation 3。原模板和新模板的实际摘要记录在结构化证据中。
后续实验仍须固定同一源码、模型、镜像与模板原件，先取得清晰的运行授权，再验证启动和完整 WAV。

前置 engine/controller profile 的 revision 1 保持 incomplete、测量数为 0。
GPU 运行前已明确的 revision 2 仍要求鉴权/双预热、完整英文 WAV、连接中断和同代物理停止四项实测；
本轮四项均未完成。它不代替最终标准 SDK、正式准入、租约/基线预算结算与独立验收。

## 授权与收尾

会话中的泛化“批准”曾被理解为同时覆盖来源和此前提出的运行窗口，并据此启动了上述 generation 1。
随后用户对原来源问题明确答复：仅批准固定来源与隔离验收，该问题明示不包含 GPU 窗口。
此后未启动下一代次；不能将来源确认作为后续 GPU 运行授权。来源批准的保守截止仍为
2026-11-03 23:33:12 UTC，没有因后续回复擅自延长。

已核验完成：新增受控实例全状态无残留；两道守护及任务控制器停止；专用 PostgreSQL、
版本化 MinIO、内部网络、远端临时源码/依赖/配置及临时凭据删除；控制器监听关闭。
13 个既有共享容器的 ID、镜像和 StartedAt 全部保持一致。本轮无新增 worktree。

专用数据库为本次隔离验收的临时运行数据，随收尾删除；上述批准和草稿计数是删除前的实测快照，
不表示当前仍有这些部署记录。最终同步 TTS Goal 及对应验收缺口保持未完成。
