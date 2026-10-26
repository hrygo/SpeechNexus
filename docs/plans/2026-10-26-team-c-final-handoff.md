---
title: "团队 C 收尾与新负责人交接"
status: active
version: "1.0.0"
date: 2026-10-26
---

# 团队 C 收尾与新负责人交接

本文件是 C 队当前接管入口。按用户 2026-10-26 的收尾要求，旧计划停止自动接力，后续 owner、优先级、集成顺序及环境窗口由新负责人重新安排。原 C00–C08 范围、41 条原 AC、278 个子条件及 Gate 前置保留；交接完成不表示业务验收完成。

本次只交付文档与 issue 整理，未实施新的产品修复或模型实验。已实现内容、未决审查、缺少制品及证据缺失分别如下，不能由 PR 或 CI 成功互相替代。

## 接管版本与阅读顺序

核验基线为 main `54daef6ae4792b69a0e5e8645b09240ff715082e`（#219）。A/B 收尾 PR 在本次读取时均未合并，接管时应重新查询。

| 入口 | 内容与使用边界 |
| --- | --- |
| [总控 #6](https://github.com/hrygo/SpeechNexus/issues/6) | 当前远端进度；25 个工作包完整验收 0/25，6 个 Gate 放行 0/6 |
| [B 收尾 #220](https://github.com/hrygo/SpeechNexus/pull/220) | 提供[项目统一交接](https://github.com/hrygo/SpeechNexus/blob/45bc90ed0fcc36264022b0c2f684e3d9006685ec/docs/audits/2026-10-26-project-handoff.md)及 B 的完整证据、归档与剩余范围；该文件目前属于待审候选 |
| [A 收尾 #221](https://github.com/hrygo/SpeechNexus/pull/221) | head `b7e65f4a119999e720d594200c53dc683bec5526`；[A 交接](https://github.com/hrygo/SpeechNexus/blob/b7e65f4a119999e720d594200c53dc683bec5526/docs/plans/2026-10-26-team-a-final-handoff.md)与共享媒体/私有检查回执，尚待集成和 B 实际接收 |
| [本轮全量 issue 核对](../audits/2026-10-26-team-c-closeout-issue-audit.md) | 全部 40 项正文、状态与元数据核对，保留目标/AC；状态标签描述实施程度，不表示 agent 仍在自动执行 |
| [C 实现/证据矩阵](../audits/2026-10-26-team-c-render-loop-status.md)、[缺口表](../audits/2026-10-26-team-c-remaining-gaps.md) | C07 已接线范围、未决代码审查和依赖，不把 C 自有风险都归为外部阻塞 |
| [C 协作方案](2026-10-26-team-c-next-phase-execution-and-collaboration.md) | A/B 联审后的 v1.2.0 历史方案，已转为 superseded；C-N01–07 供重新规划，不是开工指令 |
| [阶段 Goal](2026-10-25-team-c-next-phase-goal-and-acceptance.md)、[原始 Goal](2026-10-24-team-c-goal.md) | 原 T01–T11、AC-01–12、C00–C08 范围及历史证据，均不再自动执行 |

#220、#221 与本 PR 分别交付，合并顺序和组合验收由接管人决定。不能把 #221 的私有音频检查当成公开音频 ready；默认 AssetRuntime 仍使用 TextAssetInspector。主干中的 B 真实执行及注册输入，也尚未构成权威注册、生产准入或公开发布。

## C00–C08 逐包交接

九个工作包均为 open / `status:in-progress`，表示已有部分实施且仍缺实现或验收。完整 AC 继续以各 issue 原文为准。

| 工作包 | 已集成范围 | 剩余范围与协作输入 |
| --- | --- | --- |
| [C00 #35](https://github.com/hrygo/SpeechNexus/issues/35) | 项目/稿件版本、CAS、同源客户端/fake/能力三态、操作映射、两条公开二进制下载 | 导出 SSE、公开实时消费者、其他 surface 的行为审计、A07 生产迁移/独立存储/恢复水位；重建丢失证据 |
| [C01 #36](https://github.com/hrygo/SpeechNexus/issues/36) | Workspace 只读、任务/事件恢复、部署诊断、固定预设草稿与原键恢复 | 实际模型/实例/预算/策略、管理写入、发布/回滚/激活/退役及分页；接 A04/A06/B02 |
| [C02 #37](https://github.com/hrygo/SpeechNexus/issues/37) | 文件页面、固定模型/声音/profile、ASR 引用导入、原文封存/独立校对、固定 TXT/JSON | 同版真实上传→ready→Job/准入→执行→发布→SDK/页面试听下载；字幕/Range/格式、取消迟到与长时恢复，接 A02/A03/A04/B01/B03 |
| [C03 #38](https://github.com/hrygo/SpeechNexus/issues/38) | 工作室页面、固定声音目录/精确修订、能力与退役/禁用原因 | 真实材料、设计/克隆各自资格、preview_only、换文复用、显式发布/TTS 及生命周期竞争；接 A06/B05 |
| [C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) | AudioWorklet、合成采集/播放、credit/epoch、本地释放、设备选择和焦点修复 | 真实双工/背压/输出上界、批准 HTTPS/WSS、物理权限/设备/网络/跨浏览器/长时与完整无障碍；接 A05/B04/B06 |
| [C05 #40](https://github.com/hrygo/SpeechNexus/issues/40) | 会议持久化/封存/修订、纪要派发、提词状态/CAS/手动接管及删除账本 | speech session/gap、匹配正文的分人/对齐、要点/固定导出、真实跟随与音频保存范围；普通会议/提词不增加 LLM 依赖 |
| [C06 #41](https://github.com/hrygo/SpeechNexus/issues/41) | 默认关闭的内网 LLM、持久 turn/原意图、能力三态及诚实恢复 | [#149](https://github.com/hrygo/SpeechNexus/issues/149) 的可查询真实 Job/已发布声音、插话/可信停止、独立保存/导出、质量/出站/父预算；3 项真实 LLM 资格未执行 |
| [C07 #42](https://github.com/hrygo/SpeechNexus/issues/42) | 书籍/章节/单元、pin/指纹/stale、派发/终态结算、副本/下载、最近 100 条历史和有界参数转发 | 下表风险、真实 producer→副本→完整导出、固定清单/检查点/续作、预算/质量/试听/匿名确认；独立于 C06 验收 |
| [C08 #43](https://github.com/hrygo/SpeechNexus/issues/43) | 版本化删除预检/确认、账本/擦除/sweeper/启动重放、独立副本保留、内容日志修复及部分浏览器检查 | A07 迁移/恢复水位、B07 Linux/镜像/物理拓扑、设备/网络/全状态读屏、长时及运维资格；入口页 52 项 a11y 不覆盖全体验 |

## 必须接住的未决风险

这些是已经登记的审查问题和待验证行为，本次收尾没有修复。#215 只登记缺口；#216/#217 的参数转发测试不证明数据库或扫描已经有界。

| 风险 | 接管后需核实的行为与证据 |
| --- | --- |
| 外发与持久意图顺序 | 当前生成路径先外发再登记；需用受理后落库失败/崩溃/丢回执反例验证唯一执行。完整原身份、原键、有效意图须在外发前持久保存，HTTP 位于数据库事务外 |
| 幂等及快照 | 同键异体、重放读取可变稿件/配置、删除或撤销后未知提交仍待确认。无键提交按现行契约不承诺恢复；没有可虚构的“按键查询”资源 |
| CAS 与来源一致性 | 锁前读取、source/期望修订和写入顺序需并发验证；旧 unit/Job/generation/pin/receipt 不得结算新结果 |
| 受理与查询身份 | 完整解析受理 Job，持久使用服务端 ID，后续核对 GET 目标、workspace/kind、有效配置和结果关联。client_reference、幂等键与 Job ID 不可假定相等 |
| 请求键和读回失败 | 部分保存键仅在 useRef，刷新即失；确认提交后的读取失败可能误报“提交未知”。需区分写入回执和读取错误并覆盖存储失败/刷新 |
| 副本分类和公平性 | SpeechSourceError 与捕获分类不一致；最老 LIMIT 批次可能饥饿，无持久退避/次数上限。进程内拒绝集合与参数 limit 不能证明重启恢复、公平或资源上界 |
| 历史和页面隔离 | 数据库全读再切片、请求乱序/旧上下文响应、加载/错误/焦点状态审查未结。最近 100 条和 ARIA 有实现，稳定游标翻页及真实全状态辅助技术仍缺 |
| 真实结果和完整导出 | 已有隔离 PG/MinIO/合成结果检查；真实 Job/Artifact/receipt→独立副本→完整导出尚未联验。清单、检查点、续作、质量、预算须独立完成 |

源码入口：`backend/src/speechnexus_backend/workbench/` 下的 `application/render_units.py`、`render_reconcile.py`、`render_copies.py` 及 `infrastructure/public_speech_render_jobs.py`、`postgres_render_units.py`；页面入口 `web/workbench/src/books/RenderUnitPanel.tsx`。此处是定位信息，具体源码判断按受检版本与反例复核。

## 与 A/B 的接收和复审方式

原团队字母继续表示模块边界，实际人员与排期由新负责人指定。沿用“提供者交固定制品，消费者回读验收，集成维护者串行接共享文件”的方式，不由 C 代签 A/B 资格。

| 协作项 | 提供者交付 | 接收者核验与完成条件 |
| --- | --- | --- |
| 音频检查 | A：#221 固定源码/wheel、同源私有回执、decoder/异常类型、窄消费者与迁移角色 | B 实际兼容接收；A 另接公开音频 ready/错误/生产装配；C 只消费公开结果，不绕过底座 |
| 文件公开链 | B：#162/#219 的固定 ASR/CustomVoice 输入、命令/限制、真实结果及停止事实；A：权威 binding/默认/适用声音/profile、Job/准入/发布及公开 SDK | C 同版页面和 SDK 验真实 ASR、TTS 两链及故障恢复；ASR profile 可选，builtin CustomVoice 不等完整设计/克隆包 |
| binding/指令边界 | A/B 冻结公开表达及有效配置；公开创建未声明客户端 binding_revision_id。CustomVoice 的 pronunciation_revision=null | C 不自加字段、不静默丢 pin。一次 intent 一次合成≤400 codepoints；Nexus 2000、标准/内部 4096、固定 pipeline 默认 500 分列，模板目标 2000 未证明实际校验生效 |
| 取消与结算 | B 提供同身份停止/资源释放事实；A 由原事务幂等结算 | C 分列请求取消、本地释放、公开 Job/cleanup、执行租约与实例 base；公开 Job 无 unknown，cancelled 需 cleanup=complete/not_required。#218 仅整代停止/原执行 lease，16 GiB base 为 fixture 且未结算 |
| 迁移、删除与恢复 | C 提供自身 schema/对象区/迁移片段；A07 统一受控迁移、清理角色及恢复水位，B07 提供目标部署资格 | 先重放删除账本再开放内容，独立副本按所有权保留；fresh schema、单机或逻辑多连接不替代已有部署升级与物理多机 |

既有协作会话：[作为团队 A 持续推进工作](thread://01a176e7-3a9d-76ee-a194-eb9b87d7c0af?hostId=durable)、[作为团队 B 持续推进工作](thread://01a1679f-8dd7-78f3-9f00-9bd9b5101a85?hostId=remote-ssh-discovered%3Amegacube)。引用前读取最新内容；本次已读取两队收尾。A/B 对旧计划的联审处理保留在[协作方案 §6](2026-10-26-team-c-next-phase-execution-and-collaboration.md#6-ab-联合审查意见处理v110)；B 的[原评论](https://github.com/hrygo/SpeechNexus/issues/142#issuecomment-6411923009)是审查证据，C 采纳规划不等于修复。

## 证据、现场与清理边界

| 对象 | 当前事实与移交办法 |
| --- | --- |
| 原验收映射 | [41 条原 AC 矩阵](../audits/2026-10-25-team-c-original-ac-matrix.md)、[278 子条件机器映射](../audits/2026-10-25-team-c-acceptance-map.json)均保留；全部仍 not_run，旧实现分类不是当前完成率 |
| 证据事故 | [#189 清理丢失 82 份原件](../audits/2026-10-26-team-c-evidence-retention.md)，影响 214 个条件引用。不可由摘要补勾；稳定根重建属于新证据，不能称原件找回 |
| 当前操作映射 | [JSON](../audits/2026-10-26-team-c-operation-coverage.json)与[报告](../audits/2026-10-26-team-c-operation-coverage.md)：52 项、42 bound、10 consumer_pending；仅 workbench 接线审计，SSE/兼容退役/其他 surface/语义仍待 |
| 本轮稳定归档 | `/Users/hrygo/.codex/team-c-evidence/20261026T101715Z-final-handoff/`；保存旧工作区 3 份文档原件、原目标 diff、worktree/status、40 issue 前后原文/元数据、检查结果与 manifest。只含本任务材料，不纳入 Git |
| 旧工作区 | `/Users/hrygo/Documents/SpeechNexus`，原 C 分支 HEAD `24ea145bd7ff8f77ad7c6f96c655e4597be416dc`。既存目标 diff、贡献指南/模板和未知 `.paperclip/` 予以保留；不将旧 checkout 的未跟踪文件直接当作主干新增 |
| 最终 PR 工作区 | `/Users/hrygo/.codex/worktrees/team-c-final-handoff/SpeechNexus`，`codex/team-c-final-handoff`，独立基于 54daef6；保留供审阅，归档原件位于 worktree 之外 |

归档的 `manifest.json` 记录大小和 SHA-256；移机先校验再复制，在独立目录恢复，不覆盖现有源码、数据库、对象区或配置。原件含任务材料，也按部署内证据管理；不从工作区清理顺带删除其他 worktree、模型、运行数据或未知配置。本轮未启动或停止数据库、模型、容器和已有服务。

仓库外归档校验可用原生 Python（不连接服务）：

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib, json
root = Path('/Users/hrygo/.codex/team-c-evidence/20261026T101715Z-final-handoff')
for entry in json.loads((root / 'manifest.json').read_text())['files']:
    data = (root / entry['path']).read_bytes()
    assert len(data) == entry['bytes']
    assert hashlib.sha256(data).hexdigest() == entry['sha256']
print('archive verified')
PY
```

## 新负责人需要作出的决定

1. 核实 main 与三份收尾 PR，确定审阅/合并顺序及组合版本；#220 的全局交接入口与本文件分工互补，不能把待审候选当已集成。
2. 指定接管人、证据访问与共享文件维护者，确认哪些旧方案继续采用；C-N01–07 与历史“下一卡”均不自动启动。
3. 选择先交付的业务切片及真实环境窗口，同时给 C 自有持久化/幂等/公平性风险明确处置，不以依赖未齐暂停所有独立修复。
4. 为未决 binding/指令/未知重放语义指定 A/B/C 联审；需要机器协议、持久化或运行边界变更时再形成可评审设计。
5. 确认设备、读屏、内网 LLM、物理多机、备份恢复和长时质量资源。依原 AC 分别取证，三队确认与用户 Gate 放行仍保留。

## 本轮验证与完成边界

本 PR 按[文档验证规则](../agent-verification.md)检查本地链接、frontmatter、命令、事实与 diff；issue 全量核对结果见[核对记录](../audits/2026-10-26-team-c-closeout-issue-audit.md)。精确提交的 CI 以 PR 检查为准，文档路由跳过的业务项不记为通过。

未重跑业务、数据库、真实模型、设备、LLM 或目标拓扑资格。历史 #201 合后取消/清理未结、旧 ENOSPC、C 焦点/探针失败及丢失证据继续保留；绿色文档 CI 不解除这些问题。最终交付是可接管的记录和一个待审 PR，项目重新排期由新负责人完成。
