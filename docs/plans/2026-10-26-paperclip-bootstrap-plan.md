---
title: Paperclip 团队与工作流落地计划
status: blocked
version: "1.4.0"
date: 2026-10-27
---

# Paperclip 团队与工作流落地计划

**目标：** 将 [工作流设计](../operations/paperclip.md) 配置到 DEL 的既有 SpeechNexus 项目，先准备所有岗位必需的环境，之后再按用户要求恢复任务闭环验收。

**当前总开关：** 用户要求暂缓真实任务；七个 agent 保持暂停。当前只做环境准备与检查，不建立业务执行队列。实际环境证据与限制见 [环境验收记录](../operations/paperclip/environment-readiness.md)。

**实施方式：** 项目经理按步骤执行，使用既有 Paperclip 连接器与官方 CLI。用户已完整授权本轮工作流与 agent 调整。
设计由独立 reviewer 做 builder check，问题自行收敛；角色正文为版本源，平台执行记录为运行证据。
本计划调整管理配置与文档；平台环境保存缺陷由 [Delegra PR #20](https://github.com/hrygo/Delegra/pull/20) 单独修复。两条历史费用已通过线上 r6 既有审计调整接口更正，不需要新增计费模块；未来自动订阅声明的 [PR #21](https://github.com/hrygo/Delegra/pull/21) 保留草稿，本次不部署。不修改 SpeechNexus 产品协议、数据库、模型服务或未提交产品实现。

## 文件与交付

- `docs/operations/paperclip.md`：当前工作流、职责、三机执行、验收与回退约定。
- `docs/operations/paperclip/roles/*.md`：七个角色的受管 instructions 源。
- `docs/operations/paperclip/environment-readiness.md`：已测环境、检查边界、能力限制与启用门禁。
- 本计划：本轮配置检查点与受检边界。
- Paperclip bootstrap 任务文档：实际 agent/task ID、配置 revision、部署内路径、探针及完整回读结果。
- `docs/plans/README.md`、`docs/README.md`：接续入口。

## 落地步骤

- [x] 核对当前 GitHub main、旧团队会话、Paperclip 项目/任务/agent/预算和三台环境入口。
- [x] 注册并真实调用 `delegra-assistant` MCP，发现 37 个工具；后续写入沿用当前授权公司。
- [x] 完成独立设计审查，收敛重要缺口；独立 builder check 未发现重要待决问题。
- [x] 修正 SpeechNexus 仓库关联和工作区源；初始探针固定 `54daef6ae4792b69a0e5e8645b09240ff715082e`，收尾核对 #220–#222 合入后，将新任务基线更新为 `fe4a825ed025b7ebca3aa3d577bcb54bc9ea8de7`，三机按该版本重新锁定安装并验证新增媒体包。
- [x] 建立项目经理，将六个现有 agent 设为具名专业岗位，保存回退 revision；保留模型与 AI 连接，取得七岗成功 hello。
- [x] 应用角色正文及单 agent 并发 1、按需唤醒、关闭 timer、受限续接；用户要求环境优先后，全部暂停。
- [x] 准备三机精确工具链、锁定 Python / Web 依赖及缓存；两台 Mac 的合成媒体与 Chromium 自检通过。
- [x] 核验 MegaCube 隔离 PostgreSQL / MySQL / MinIO 的合成读写和清理，准备 Paperclip 专属 MinIO，测试连接使用加密凭据引用。
- [x] 根据 SSH 空目录实测将 B / QA 切换到 Codex CLI 的 Legacy runner；两机的固定源码与未提交修改往返通过，远端 commit 模式保留未通过结论。
- [x] 合并 Delegra PR #20，发布不可变 r8，核对实际运行提交与受检源码文件树一致；A/B 三项最终字段均正常保存并通过只读数据库精确回查，模型、连接、秘密引用及七岗暂停不变。
- [x] 按用户确认的“固定订阅已包含、不按本次用量加收费用”，通过 r6 既有接口为两条原事件追加可追溯审计调整；回读未计价数为 0、预算 `ok`、原 incident 已自动解决，原回执/用量不变，额度和未计价阻止保持。
- [ ] 在允许恢复非业务环境探针后，验收真实 agent 任务准入、工具、报告回写和 workspace finalize。DEL-37–40 的历史失败与未验事实保留。
- [ ] 在用户暂缓派工要求解除后，再保存有 owner、reviewer 和前置条件的近期业务队列；当前不派发。
- [x] 回读七岗配置和 instructions hash；DEL-36 保存交接文档，DEL-36–40 有机更新正文、检查项、父子关系和解阻责任，全部保持 blocked，不以直接 SSH 结果冒充 agent 交付。
- [x] 核对 12 份文档、158 个本地链接、frontmatter、七份角色源 hash、最终 diff 与敏感信息；提交可评审的仓库文档交付。

## 验收重点

1. 错仓库、旧基线或空目录必须被发现，不得继续产品写入。
2. 远端 runtime 通过平台工作区传递与最终化交付，不能使用本机路径或绕开 restore。
3. 作者与高风险 reviewer 分离；同一文件只有一个写者。
4. 任务完成、PR 合并和 GitHub 整包 AC 完成分别判断；历史结果固定原版本。
5. 未计价/超预算、工具/鉴权/工作区失败停止派工，既有模型与生产服务保持稳定。

## 实际结果

团队、工具与基础环境已配置，原始配置及实际检查保存在 DEL-36 的部署内交付记录。
七个 agent 保持暂停；三机准备、各 Harness hello 和适配器修改回传分别取得证据。两条历史费用已通过既有接口更正并解除预算硬停；预算保护未放宽。A/B 保存修复的本地类型检查、构建、独立源审及 50 项 CI 通过，另 1 项 CI 跳过；PR #20 已合并，r8 的实际运行提交、恢复就绪与三项字段保存均已核验，原费用/用量/回执不变。本地全量长测仍在运行，不计通过。自动订阅声明功能未部署。完整任务闭环尚未在新配置下执行，用户暂缓业务派工的要求继续有效。
因此本计划保持 blocked，不把管理员自检或旧探针的运行结果升级为真实 agent 完整交付验收。
