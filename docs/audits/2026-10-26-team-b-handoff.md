---
title: "团队 B 收尾与负责人交接"
status: active
version: "1.0.0"
date: 2026-10-26
---

# 团队 B 收尾与负责人交接

2026-10-26（Asia/Shanghai），用户要求收敛工作、清理现场、更新文档与 issue，
提交最后一个收尾 PR；后续工作由新负责人重新安排。本记录交接已交付事实与未完成范围，
不形成新排期，不放行 Gate，也不将 B00–B07 整体目标标为完成。

## 接管基线

- 交接前远端与本机 main 均为 `54daef6ae4792b69a0e5e8645b09240ff715082e`，
  工作区干净，没有开放 PR。收尾 PR 的文档检查与业务基线证据分别记录。
- [#219](https://github.com/hrygo/SpeechNexus/pull/219) 已于 2026-10-26
  16:16:13（Asia/Shanghai）合并；对应
  [PR CI](https://github.com/hrygo/SpeechNexus/actions/runs/40289866804) 与
  [精确合后 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40291095616)
  均为 14 项成功。完整 CI 五分钟目标未达到，原 PR Python job 为 8 分 46 秒。
- 原[团队 B 目标](../plans/2026-10-23-team-b-goal.md)保留完整范围与历史证据；
  原优先级及自动推进安排停止执行，恢复工作前由新负责人重新指定范围、版本和环境。
- [总控 #6](https://github.com/hrygo/SpeechNexus/issues/6)、
  [阶段任务 #142](https://github.com/hrygo/SpeechNexus/issues/142)及
  [跟踪矩阵](../plans/2026-10-23-implementation-tracking.md)承接未完成事项。
  issue 的 `in-progress` 表示已有部分实施，不表示交接后仍有自动执行任务。

## 已交付内容及证据边界

| 内容 | 可接收产物与证据 | 尚不能据此宣称 |
| --- | --- | --- |
| 文件 ASR | [固定制品及注册输入](../architecture/file-asr-binding-handoff.md)、[默认工厂](../architecture/file-asr-runtime.md)、[真实隔离实验](2026-10-25-b03-default-real-asr-probe.md)；真实 1.7B/CPU VAD、语音/静音、私有结果、原身份重放与网关重启 | 正式注册发布、生产准入、公开上传到下载完整链 |
| 文件 CustomVoice | [固定制品及注册输入](../architecture/file-tts-binding-handoff.md)、[默认工厂](../architecture/file-tts-runtime.md)、[真实三格式实验](2026-10-26-b03-default-real-tts-probe.md)；WAV/MP3/PCM、24 kHz、生成区间及固定结果版本 | Base、VoiceDesign、克隆、实时、长文本或全部参数组合资格 |
| 持久执行 | #152/#154/#158/#163/#168/#203 已集成；[执行接线](../plans/2026-10-25-b03-durable-file-runner.md)复用原 intent、回执和 worker，组件覆盖确认丢失与双消费者竞争 | 真实引擎/控制器故障及完整公开链已同版验收 |
| 控制器与停止 | [ASR 停止结算](2026-10-25-b03-real-stop-settlement.md)、[取消安全链](2026-10-25-b03-real-cancel-settlement.md)、[TTS 停止结算](2026-10-26-b03-real-tts-stop-settlement.md)；真实整代次双证据、默认恢复和 A 原事务幂等结算 | 单请求 GPU 释放、持续 ready、真实故障恢复或物理多机资格 |
| CPU VAD 与区间 | [CPU VAD](../architecture/cpu-vad.md)、[有界媒体](../architecture/file-asr-audio.md)、已集成的输出区间组件 | 实时双工、对齐、匿名分人或完整 required 配方资格 |

历史实验分别绑定其报告中的候选、镜像、fixture 和拓扑，不能把多个版本的局部成功
合并成当前主干的完整生产资格。#219 的 187 项制品/命令及 75 项 PG HTTP/双库存储回归，
证明注册输入与消费者兼容；注册、发布和容量资格仍由相应生产链提供证据。

TTS #218 的资格、准入及 runtime stop 意图生产者仍为 fixture；实例 base 为 16 GiB，
该次只结算执行租约，base 未结算。不能把 ASR 的 base 分步归零实验转挂到 TTS。
原生 worker 的 `release_evidence=false`，不能据观察结果直接退预算。

## 未完成范围清单

以下是接管时的缺口，不预定新负责人采用的实施顺序。全部原 AC 保留。

| 工作包 | 交接时标签 | 主要剩余 |
| --- | --- | --- |
| B00 #27 | in-progress | 全候选模型/许可/资源 manifest、权威 binding 与逐能力资格 |
| B01 #28 | in-progress | 生产 submit/observe/abort/恢复、真实故障、单请求停止与生产预算链 |
| B02 #29 | in-progress | 正式 readiness、持续监控、controller 重启、多代竞争、peak→steady/drain |
| B03 #30 / #142 | in-progress | 正式 ASR/TTS 公开链、完整参数矩阵、长文本、Base 与同版联合验收 |
| B04 #31 | in-progress | 真实 PCM/WS、credit/背压、流式 ASR/TTS 和双工完整配方 |
| B05 #32 | planned | VoiceDesign/Base、克隆、换文复用、声音材料恢复和显式发布 |
| B06 #33 | in-progress | 实时 VAD、词级对齐、匿名分人及逐绑定质量资格 |
| B07 #34 | planned | 质量/容量门槛、混合负载、长时、故障及物理多机资格 |

A 的权威配置、音频 Asset ready、完整 Job/准入/runtime 意图/发布/下载，
以及 SDK/C 正式消费者的同版联验仍缺。共享媒体包尚待 A 提供固定提交/wheel/源码映射，
B 尚未完成其实际网关兼容接收。各方存在可独立推进的事项，不能把整个工作包统称为阻塞。
固定模型卡与引擎许可证来源不等于全部依赖再分发许可已经审核通过。

### TTS 指令长度：保留调查，不宣布修复

| 入口 | 当前声明上限 | 权威来源 |
| --- | --- | --- |
| Nexus TTS overrides | 2000 codepoints | [speech-overrides.schema.json](../../contracts/shared/speech-overrides.schema.json) |
| 标准 speech 请求 | 4096 codepoints | [speech-openapi.yaml](../../contracts/speech-openapi.yaml) 的 `SpeechRequest.instructions` |
| 内部执行命令 | 4096 codepoints | [execution-command.schema.json](../../contracts/internal/execution-command.schema.json) 的 TTS 参数 |
| 固定 Omni pipeline | 默认声明 500；CLI 可声明覆盖 | [runtime pin](../../deploy/engines/file-tts-customvoice/runtime-pin.json) 固定的源码 |

既有交接曾将 2000 统称“公共命令上限”，本次按入口纠正。批准模板目标 2000 不代表
标准入口的 2001–4096 已有合法映射或准入策略；相关兼容语义仍待 A/B 明确，
不得静默截断或通过收窄公共 schema 掩盖差异。

调查只确认 serving 计算并缓存该配置；是否传入实际 adapter 请求校验仍未完成验证。
当前没有产品补丁、2000/4096 上界真实合成或超限拒绝证据。
原短指令实验不能补齐此项，接管方需要同时核对字符上限、token 总预算与实际拒绝路径。
本轮遵照收尾要求停止调查，没有加载模型。

## C 方联合审查交接

[B 方完整意见](https://github.com/hrygo/SpeechNexus/issues/142#issuecomment-6411923009)
已发布到共享 issue；C 随后通过会话回执确认收到，并声明已修订本机计划为 v1.1.0。
回执中的原文件 SHA-256 为
`66fccff9c80f0c8c25395a111a19b97cdce3ef13ac620fede3acaaa13b236205`；
B 未独立核验 C 本机文件，也未将其视作已提交或已集成。

C 回执采纳外发前持久身份、原键恢复、Job/有效配置与版本核对、
400 codepoints 单次 CustomVoice 边界，以及单请求/整代停止/实例 base 分列。
请求固定 binding 的公开表达、无键恢复和删除后未知重放等仍待 A/C 冻结；
不自行新增公共字段或查询接口。回执还指出并经 B 读取源契约确认上述 2000/4096 差异。
这些是计划修订与审查反馈，不是 C 实现或联合验收已通过。

## 现场清理与私有证据保留

- 收尾检查没有发现遗留 ASR/TTS 实验容器；三个受保护服务为
  `cube-ccba-qwen36-35b-nvfp4`、`qwenpaw-embedding`、`qwenpaw-reranker`。
  本轮不停止或重启它们，不清理同机其他项目的容器、存储或网络。
- `.runtime/probes/` 的 5308 份文件、512351757 字节已在部署内另外归档，
  gzip 归档经过完整读取检查；原探针目录保留，不把工作树清理当作删除证据的理由。
  归档可能包含私有配置，仅保存在权限受限的本地目录，不上传到 Git 或 CI。
- 旧 `codex/speechnexus-engineering-skills` 工作树有 10 份未提交文件，
  包含 AGENTS 修改及三个旧 skill 草稿，均已逐文件摘要归档；
  原分支历史另存 Git bundle 并验证。完成核对后已删除旧工作树和本地分支。
  这些草稿未集成，不替代 main 中现有项目技能。
- `.venv/` 与 `node_modules/` 属于可重建依赖，不纳入草稿归档。
  模型制品、原语料和项目基础设施均保留。收尾 PR 是本轮唯一交付分支，
  不自动合并，留给新负责人审阅。

归档目录由本机忽略文件 `.runtime/probes/team-b-closeout/archive-root.txt` 指向；
本地 `archive-verification.json` 记录文件数量、长度与摘要。
`skills-worktree-manifest.json` 保存原分支、基线及每份草稿摘要。
新负责人获得本机访问后可按以下方式恢复到独立目录；不要覆盖当前工作树：

```bash
git clone --branch codex/speechnexus-engineering-skills \
  "$SPEECHNEXUS_B_HANDOFF_ROOT/skills-history.bundle" "$SPEECHNEXUS_B_RESTORE_DIR"
tar -xzf "$SPEECHNEXUS_B_HANDOFF_ROOT/skills-worktree-wip.tar.gz" \
  -C "$SPEECHNEXUS_B_RESTORE_DIR"
```

其中 `SPEECHNEXUS_B_HANDOFF_ROOT` 指向已核验归档目录，
`SPEECHNEXUS_B_RESTORE_DIR` 必须是新的恢复目录。
`skills-worktree.patch` 用于审阅，tar 已含修改后文件，不再重复应用 patch。
私有探针恢复也使用独立目录，先核验归档摘要，不导入共享数据库或覆盖运行配置。

## 交接后保持的边界

集群支持要求不降级；单机逻辑竞争不能代替物理多机资格。PostgreSQL 保存权威事实，
程序负责业务判断与编排，不恢复存储过程业务引擎，不采用 etcd，Redis 只作可选加速。
unknown 继续占用，停止及资源释放双证据后由 A 原事务结算，不另建预算权威。
内容、凭据和模型路径不出部署网络；业务与工作台不绕过网关直连引擎。

本次只验证文档、状态同步、归档和收尾 PR 的适用检查，不重新执行旧业务或模型实验。
新负责人依据上述事实重新安排工作；未完成范围不因收尾、暂停旧 Goal 或文档 CI 成功而消失。
