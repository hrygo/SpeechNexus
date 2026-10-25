# Agent 工程入口与维护

本页供定位材料或维护 Agent 体系时按需读取。仓库通用边界在 [AGENTS.md](../AGENTS.md)，验证命令在[验证指南](agent-verification.md)；架构事实继续由现有契约和设计文档维护。

## 规则放在哪里

| 载体 | 维护内容 | 读取时机 |
| --- | --- | --- |
| 根 `AGENTS.md` | 跨模块的产品、安全、授权、证据边界和任务入口 | 整个仓库 |
| 目录 `AGENTS.md` | 该目录稳定、独有的编辑约束；继承根规则 | 处理对应目录，包括从根目录启动的任务 |
| `.agents/skills/*/SKILL.md` | 可重复、输入和交付明确的项目工作流 | 用户请求符合描述，或显式调用时 |
| skill 的 `references/` | 仅某种模式需要的格式、状态规则、跟踪刷新步骤 | 到达相应分支时 |
| `contracts/`、有效 ADR、架构文档 | 协议、设计和长期决定的权威来源 | 当前任务涉及相应边界时 |
| `docs/plans/`、`docs/audits/` | 特定阶段的方案与有时间、版本、环境边界的证据 | 当前任务明确需要时 |

目录规则只作用于自己的路径。当前宿主未自动加载目标目录规则时，先手动读取；不要假设从根目录启动会注入全部子目录内容。`.paperclip/` 等工具工作树是独立 checkout，不作为根项目指令或技能的副本维护。

## 按任务定位材料

只读当前任务需要的范围，不把本表作为必读清单。

| 任务 | 入口 |
| --- | --- |
| 当前能力、启动入口 | [README.md](../README.md)、[README.en.md](../README.en.md) |
| 文档与工程交接 | [文档索引](README.md)、[整体技术方案](architecture/technical-blueprint.md) |
| 模块边界与依赖方向 | [工程目录说明](architecture/repository-layout.md) |
| 公共与内部协议、错误语义、生成类型 | [契约索引](../contracts/README.md)、[生成契约包](../packages/contracts/README.md)、[所有权矩阵](architecture/contract-ownership.md)、[HTTP 结构变化检查](architecture/http-contract-change-checks.md) |
| 产品范围、身份与共享部署 | [ADR 0001](decisions/0001-product-boundaries-and-authentication-scope.md)、[产品设计](speechnexus-product-architecture.md) |
| 模型选择、绑定与加载 | [ADR 0002](decisions/0002-speech-models-engines-and-loading.md)、[资源治理](architecture/runtime-governance.md) |
| 工作台 LLM 与内容网络边界 | [ADR 0003](decisions/0003-workbench-llm-network-boundary.md) |
| 集群、单机同构与数据库支持范围 | [ADR 0004](decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)、[ADR 0006](decisions/0006-cluster-coordination-and-storage-portability.md) |
| 执行命令、公开回执与删除 | [ADR 0005](decisions/0005-complete-execution-and-data-lifecycle-contracts.md)、[执行命令](architecture/execution-command.md)、[删除生命周期](architecture/data-deletion.md) |
| 协调事务、锁序、表所有权与恢复 | [应用事务](architecture/coordination-transactions.md)、[协调 Schema](architecture/distributed-coordination-schema.md)、[事务契约](../contracts/internal/coordination-transactions.schema.json) |
| 遗留协调原型退出 | [ADR 0007](decisions/0007-legacy-coordination-retirement.md) |
| Web 视觉、Token 与无障碍 | [Web 目录规则](../web/AGENTS.md)、[设计框架](architecture/ui-ux-design-framework-and-tokens.md) |
| 环境、服务与部署 | [开发环境](development-environment.md)、[部署说明](../deploy/README.md)、[集成测试说明](../tests/integration/README.md) |
| CI 调度、验证范围与门禁 | [CI 规则](architecture/ci-validation.md)、[工作流](../.github/workflows/ci.yml) |
| SpeechRail 规则复用 | [固定来源与边界](architecture/speechrail-reuse.md) |

当前代码与可复现运行结果说明已实现行为；机器契约定义协议预期；`active` 设计与 `accepted` ADR 表示当前设计基线，旧决定按后续 ADR 的替代范围理解。冲突须指出具体差异，不通过放宽校验掩盖。计划、占位目录、历史 fixture 和测试原型不能单独证明生产能力已交付。

## 维护方法

- 长期日常流程以一个 PR 完整解决并关闭一个小 issue 为默认；四部分正文简写，状态按实际工作直接收敛。现有多团队、Epic、Gate 与编排矩阵仅在实际关系和交付影响需要时使用，不作为未来所有 issue 的固定流程。
- 优先修正现有责任入口。新增 skill 应有独立的触发意图、可重复的多步骤任务和可观察交付；一个目录约束或一次故障不单独建技能。不规定技能数量。
- 描述只写能力与触发边界，正文保留必要判断和完成条件；条件细节放入直接链接的参考。避免复制架构正文、全局技能、工具目录和本机环境快照。
- 项目技能保存在本仓库 `.agents/skills/`，不复制到个人技能目录或嵌套工作树。保持目录名和 frontmatter `name` 一致；普通自动发现维持默认，显式调用不扩大授权。
- 变更命令时同时核对根路由、目录规则和[验证指南](agent-verification.md)，以 `Makefile`、`package.json`、CI 和有效测试说明核验实际行为。文档修改不顺带安装依赖或启动服务。
- 修改技能后检查 YAML、链接、名称唯一性、引用路径、授权衔接和代表性任务。结构校验不能证明触发准确率或任务效果；只有实际运行的行为评估才能作为效果证据。

## 回归情景

下表是维护时的预期行为，用于静态走查或隔离演练；它不是已经执行的评测结果。演练使用合成材料，不写 GitHub 或启动业务服务。

| 请求 | 预期入口与结果 |
| --- | --- |
| 修复 README 的链接 | 文档验证；不触发协调工作流或启动服务 |
| 新增公开路由或改变错误字段 | `speechnexus-contract-change`；先核对生产者、消费者及正负样例 |
| 判断提交未知、重放、旧响应或取消后的恢复动作 | `speechnexus-recovery-design`；按原身份和权威事实决定动作，协议/事务未变时不展开数据库流程 |
| 修改协调事务、锁序、租约释放或预算落地 | `speechnexus-coordination-change`；核对事务原子性、停止证据、锁序和双库范围 |
| 客户端丢回执后，方案需要新增原命令查询接口并修改持久事务 | recovery 主导恢复决定，contract 处理新接口及消费者，coordination 处理事务；共同材料和检查复用，不串行重跑三套完整流程 |
| 调整 Web 间距 | `web/AGENTS.md` 与 Web 检查；不启动数据库 |
| 整理某个 issue 正文 | `speechnexus-issue-maintenance`；四部分结构，不擅改状态和标签 |
| 一个 PR 完成小 issue 全部范围与 AC，并合入主干 | `sync-issues-after-merge`；直接关闭并维护 `status:done`，没有相关层级就只处理这一项 |
| 小 issue 已正确自动关闭，正文、勾选和标签均准确 | 核对后结束；不重开、不重复写入、不刷新无关矩阵 |
| 查看某 PR 是否已满足 AC | 只读核对证据；不因读取技能而写远端 |
| 合并获准 PR 并完成收尾 | `sync-issues-after-merge`；核实主干合并、逐项判断 AC、写后回读 |
| 仅有 PostgreSQL 测试通过 | 报告单库覆盖；不声明双库协调或真实集群验收通过 |
| 在 macOS 请求完整验证 | 先核验脚本宿主条件；缺 Linux/systemd 条件时报告受限项 |

## 方法来源

以下官方资料核验于 **2026-10-25**；本项目的拆分方式是结合现有边界作出的工程选择。

- [AGENTS.md 指令发现](https://learn.chatgpt.com/docs/agent-configuration/agents-md)：区分通用规则与目录规则，注意启动目录和加载范围。
- [Build skills](https://learn.chatgpt.com/docs/build-skills)：项目技能在 `.agents/skills/` 发现，名称与描述负责选择，正文与支持文件按需读取。
- [Rethinking skills and prompts](https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra)：收窄描述、渐进披露、减少没有实际收益的固定流程。
