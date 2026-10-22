---
title: "SpeechRail 复用边界"
status: active
version: "0.3.0"
date: 2026-10-22
---

# SpeechRail 复用边界

SpeechNexus 以独立领域与运行边界演进。本文记录可复用对象、提取条件与证据来源，不构成对 SpeechRail 代码、配置或运行态的修改要求。

## 1. 复用矩阵

> 🧠 **From Hindsight memory (SpeechRail 组件职责与权威边界)** — SpeechRail 的历史设计将语音能力与调用方编排分开，MCP 是 REST 上的无状态代理；这些边界可以继承。单 Mac 的资源 owner 与运行事实不等于企业集群的容量保证。

> 🧠 **From Hindsight memory (四场景共享 ASR：策略与契约落地)** — 共享内核、场景预设、可修订预览、完整段复核、空成功与真实停止后释放资源，是可继承的设计经验。该页的 2026-10-05/06 进度只描述当时，本文以源码核对当前可提取对象，不沿用其“尚未实施”状态。

| SpeechRail 来源 | 复用类别 | SpeechNexus 处理 | 必须重新验证 |
| --- | --- | --- | --- |
| `src/speechrail/domain/asr_policy.py` / ASRPolicy | 可提取领域代码 | 保留严格类型、范围、预览与 finalization；将能力交集扩展为绑定规则 | 与 vLLM 流式 / 完整复核的实际映射；不把 rollback 参数假装成所有引擎能力 |
| `src/speechrail/domain/render_recipe.py` / RenderRecipe | 可提取模型与验证规则 | 转为 ExecutionReceipt 的内容 / 声音 / 模型 / 参数事实与 missing / unknown 语义 | 移除本机路径假设，增加部署 / 工作空间 / attempt / 绑定 revision |
| `src/speechrail/application/services.py` / AppOverrides 与组合入口 | 复用依赖注入方式 | 新组合根注入服务中立端口，不直接导入旧 build_app_services | 不能带入本机 worker 构造、selection 与单例假设 |
| `src/speechrail/application/capability_snapshot.py` 与有效能力文档 | 复用一致快照语义 | 加入 policy revision、后端绑定与容量资格，按部署 / 工作空间呈现 | 缓存作用域、查询无副作用、提交重验状态 |
| `src/speechrail/domain/idempotency.py` / DurableIdempotencyJournal | 复用状态与摘要规则，重写存储 | 原子唯一约束与意图协调移到 PostgreSQL | 原有 JSON 文件、进程内 RLock 与原子替换不能充当跨进程保证 |
| `src/speechrail/runtime/jobs.py`、`job_runner.py` | 复用用例与故障案例，重写基础设施 | 持久任务改为 JobStep / attempt / outbox / staging 发布 | 本机文件 spool、单 runner 与 owner 规则不直接复用 |
| `src/speechrail/runtime/resource_governor.py`、`admission.py` | 复用准入不变量与测试思想，重写调度 | 增加工作空间 / 任务公平、资源向量、全局租约与 fencing | 旧进程内锁、物理内存预算与单 Mac lane 不适用集群 |
| `docs/architecture/speechrail-mcp-proxy.md` | 复用代理边界 | 新无状态 MCP 只代理公开语音 / 音色 REST，沿用部署入口策略 | 工具发现 / 调用、可选共享凭据、不持有 WS / 模型 |
| 现有契约 / 测试中的边界案例 | 提取 fixture 与断言意图 | 为新规范生成新 REST / WS / MCP parity 测试 | SpeechRail 扩展不能简单改名前缀后宣称新协议成立 |
| macOS 会话 / 有声书 / 提词文档 | 复用产品规则与体验经验 | 重建 Web 业务工作流和浏览器生命周期 | 浏览器采集、播放、后台状态、键盘和无障碍 |
| MLX worker、CoreML Sortformer、私有 IPC、XPC / LaunchAgent、SwiftUI 视图 | 平台专属，重新实现 | Linux adapter、独立分人 / 对齐服务与 WebUI | 不复制 macOS 兼容分支或模型二进制 |
| `LICENSE` | 保留来源说明 | 提取代码保留原版权与许可证，分别检查模型和第三方资产许可 | 不将应用 MIT 标识外推为所有模型 / 声音素材许可 |

提取流程为：锁定来源 commit → 列出纯领域模块及依赖 → 在新仓库建立端口 → 移植小范围代码与针对性断言 → 对新接口验收。首版不引入两个仓库共同发布的共享基础包；等两端确实出现稳定且重复的接口，再单独评估包化成本。避免为追求复用把 macOS 生命周期重新带进企业应用。

## 2. 固定来源与证据范围

源码与文档核验日期：2026-10-22，Asia/Shanghai。本节 SpeechRail 来源固定为 commit [`3f57647a3ee7`](https://github.com/hrygo/SpeechRail/tree/3f57647a3ee79c04318d8bf097489b2b5185da4f)，便于复核。设计时对依赖的代码路径进行结构查询与覆盖核对，易变配置与缺口直接读源文件；此项核验不构成全仓审计或 Linux 运行验证。

- [总体架构](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/docs/architecture/architecture.md)：单机语音服务边界与 MLX / CoreML 路线。
- [ASRPolicy](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/domain/asr_policy.py)：严格参数域、预览、定稿和有效段预算。
- [RenderRecipe](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/domain/render_recipe.py)：实际执行事实、revision 与 missing / unknown。
- [组合根](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/application/services.py)：注入入口及平台专属 worker 组装。
- [能力快照](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/application/capability_snapshot.py)、[有效能力文档](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/docs/users/effective-capabilities.md)：一致快照、能力与质量的区分。
- [幂等意图](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/domain/idempotency.py)、[本机任务仓库](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/runtime/jobs.py)、[任务执行器](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/runtime/job_runner.py)：可以提取的状态经验与需要重写的存储 / 执行基础设施。
- [资源治理](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/runtime/resource_governor.py)、[准入队列](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/src/speechrail/runtime/admission.py)：资源释放与单机局限。
- [MCP Proxy](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/docs/architecture/speechrail-mcp-proxy.md)：无状态 REST 工具边界。
- [会议 / 字幕策略设计](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/docs/superpowers/specs/2026-10-16-meeting-caption-asr-design.md)：预览、正文与辅助结果版本的分离；其中历史实测不当作本方案的 Linux 证据。
- [项目运行约束](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/pyproject.toml)、[许可证](https://github.com/hrygo/SpeechRail/blob/3f57647a3ee79c04318d8bf097489b2b5185da4f/LICENSE)：当前 macOS / Python 约束与代码来源。


## 3. 提取与许可证检查

每次提取记录上游 commit、来源文件、保留的版权 / 许可证、变更范围和新契约验证。原应用代码许可、模型权重许可、第三方依赖许可与声音素材的使用确认分别核对。领域规则可继承，Linux adapter、多进程调度与浏览器生命周期必须在 SpeechNexus 重新验证。

[项目主设计](../speechnexus-product-architecture.md) · [语音接口](speech-api.md) · [资源治理](runtime-governance.md)
