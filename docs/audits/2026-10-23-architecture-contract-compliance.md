---
title: "架构与契约完整性、OpenAI 兼容性和 SOLID / DRY 审查"
status: reviewed
version: "0.1.0"
date: 2026-10-23
---

# 架构与契约完整性、OpenAI 兼容性和 SOLID / DRY 审查

本记录复核上一轮《架构与契约审查（实现前）》中提出的问题，并按当前仓库重新检查公共语音、Realtime、工作台、管理面、内部执行协议、协调 DDL、OpenAI SDK 兼容门禁和模块依赖。结论以本文列出的命令和样例为限；机器契约通过不等于业务路由、真实引擎或质量资格已实现。

## 1. 结论

上一轮报告中的 P0 阻断项均成立并已修复。当前没有发现需要改变 OpenAI 标准路径、标准字段含义或标准错误 envelope 的未决问题；SpeechNexus 增强仍限定在 `/v1/nexus`、可选 `nexus` 对象和 `nexus.*` 事件。

本轮只读复核后又确认并修复了三类契约缺口：

1. `JobEvent.data` 和 `ExportEvent.data` 原先允许任意对象，事件类型无法约束 payload；现已改为按事件类型区分的严格 data schema。
2. 工作台 MinutesJob、AssistantTurn、ExportJob 的错误对象原先无结构，终态也缺少错误字段约束；现已统一为结构化 `code / message / retryable / param`，失败必须有错误，非 failed 状态不得携带非空错误。
3. 标准转写响应的 `logprobs` 原先是无约束对象；现按官方 OpenAI SDK 2.54.0 的实际 `Logprob` 形状收紧，并由 SDK 解析测试覆盖。

以下判断不因上述修复而扩大：**业务路由、WebSocket 运行时、worker、调度器、真实 adapter、容量与质量资格仍未实现。**

## 2. 对上一轮问题的复核

### 2.1 P0 阻断项

| 编号 | 复核结论 | 当前证据 |
| --- | --- | --- |
| B-1 generation 历史被唯一键阻断 | 成立，已修复 | 删除 `instance_id` 全局唯一约束；历史 generation 可保留，未停止 generation 由 partial unique index 限制；隔离 PostgreSQL 测试覆盖重建设备与历史保留 |
| B-2 意图唯一键禁止合法重试 | 成立，已修复 | 完整意图历史保留，仅 `pending / reconciling / quarantined` 状态对同一 generation / action 去重；测试覆盖历史与活跃意图并存 |
| B-3 门禁只匹配 DDL 文本 | 成立，已修复 | CI 提供 PostgreSQL 16 service，集成测试创建隔离 schema、实际执行 DDL 并验证正负事务；结构文本断言仅作快速冒烟 |
| B-4 ID / pool / digest 多份副本 | 成立，已修复到当前可维护边界 | 公共和内部契约引用 `contracts/shared/`；数据库用 `resource_pools` 注册表和外键，不再复制 pool 枚举。SQL 仍需本地表达列类型 / 正则，无法直接 `$ref` JSON Schema，后续应由迁移生成器或一致性测试维护 |
| B-5 pool 拓扑自相矛盾 | 成立，已修复 | pool 明确为可跨节点逻辑池；移除 `scheduler_pool_leases.owner_node_id`，ADR 0004 与 schema 文档同步 |
| B-6 quarantine 维度不全且无退出路径 | 成立，已修复 | unified / device / host / CPU / PCM / scratch 全部计入隔离账目；超阈值禁止 `ready`；后台证据与人工核验 release 端点、证据类型和摘要已进入契约与 DDL 检查 |

### 2.2 契约表达力

| 编号 | 复核结论 | 当前处理 |
| --- | --- | --- |
| C-1 公共与内部错误模型不同 | 部分成立，不合并 | 公共入口统一引用 OpenAI 可解析的 `ErrorBody`；内部错误保留 fencing、generation、cleanup 等控制字段。强行合并会泄漏或丢失内部控制语义 |
| C-2 ID 前缀类型安全 | 不成立为 wire 级阻断 | ID 保持 opaque；类型安全应由生成代码的 branded type / `NewType` 实现，不能把前缀变成授权或对象类型判断 |
| C-3 调用方指定 staging 路径 | 成立，已修复 | `SubmitExecutionRequest` 删除自由路径入口，staging namespace 由 gateway 从 `workspace_id` 与 `submit_intent_id` 派生 |
| C-4 标量 watermark 覆盖区间证据 | 成立，已修复 | 新增 append-only `output_sample_intervals`，禁止同一 meter 区间重叠、越硬上限、改写或删除；标量只作为查询投影并保持单调 |
| C-5 envelope 聚合口径不明 | 成立，已修复 | schema 冻结 stage 求和、`engine_active_slots` 取最大值、`max_active_slots` 取最小值的规则，并由语义测试校验样例 |
| C-6 数据库一致性约束缺失 | 成立，已修复 | 增加 service-class / pool / workspace / execution identity 复合外键、fencing 单调触发器、`updated_at` 触发器、submit intent 与输出区间身份约束 |
| C-7 无破坏性变更门禁 | 成立，已加入 | PR CI 对比目标分支的公共 OpenAPI / Realtime schema 及其引用的 `contracts/shared/` 公共域；删除路径、字段、状态、事件类型或枚举，增加必填字段或收紧域会失败 |
| C-8 unified memory 可被重复计费 | 成立，已修复 | host、instance、intent、lease 的 memory-architecture 计数器条件约束和复合外键阻止统一内存同时使用 device / host 预算 |

### 2.3 架构取舍

- 单机 MVP 允许 scheduler / job-worker 逻辑角色折叠进 API 进程，但保留独立身份、连接池、租约、claim 与协议边界；node-controller 不与业务 API 合并。
- `host_nodes` 行锁是已知准入热点，文档不再把“增加节点”描述成零成本线性扩容，要求 P5 前用压测给出阈值。
- 首个真实 adapter 接入后设置一次契约复审点，允许依据真实停止证据、watermark 和 usage 语义修订内部协议；OpenAI 标准路径仍不得漂移。
- 延迟预算补充了 ASR 首预览 / TTS 首 PCM 的计时口径和 p95 门槛；具体阶段预算仍须真实 adapter 测量后冻结。
- DDL 已移出 Python package，位于 `migrations/`，明确由独立迁移角色执行，不随 wheel 发布。

## 3. 本轮新增修复

| 问题 | 为什么是真问题 | 修复与门禁 |
| --- | --- | --- |
| SSE event `data` 为任意对象 | 事件类型、状态、错误和产物之间没有机器可验证关系，消费者无法据此生成稳定类型或拒绝错误 payload | `JobEvent` / `ExportEvent` 改为按 `type` 的严格 variant；未知嵌套字段、错误状态组合被负例拒绝 |
| 工作台 Job error 不受约束 | 公共资源可能返回不可解析、不可重试判断的自由对象，且失败与成功状态可同时携带矛盾错误 | 统一 `WorkbenchJobError`，并加 failed / succeeded / cancelled / expired 条件约束 |
| `Transcription.logprobs` 无 schema | 与标准响应声明不一致，SDK 可解析性没有约束 | 使用官方 SDK `Logprob` 字段形状；SDK mock transport 返回并解析 logprobs |
| 事件样例只抽查一个未知字段 | 其他事件 variant 的严格性可能悄悄回退 | contract test 对全部 Realtime / execution-stream 样例注入未知顶层字段并要求拒绝；新增 SSE 嵌套字段负例 |
| `required` / `enum` / `type` 数组重复值无门禁 | 重复项不影响部分生成器，却会让契约意图和生成产物漂移 | 新增语义数组重复值测试；YAML / JSON 重复 key 测试继续保留 |
| 兼容性脚本本身没有行为测试 | 历史基线为新文件时脚本会 skip，无法证明检测逻辑仍有效 | 增加合成 base / current 测试，确认删除属性、增加必填字段和删除事件类型都会失败 |

## 4. SOLID 与 DRY 判断

### 4.1 已收敛

- 公共、管理和内部契约分别建模，公共 DTO 不暴露内部端点、实例、generation、存储 key 或凭据。
- `SpeechTaskKind`、`StageRole`、`EngineKind`、`ServiceClass`、`ReleaseTier`、`MemoryArchitecture`、revisioned reference、language tag、PCM / usage / realtime limits 和 retire request 已集中到 `contracts/shared/`。
- OpenAI 标准响应错误、Realtime 标准事件和 `nexus.*` 扩展保持命名空间分离；增强没有改变标准字段语义。
- OpenAI SDK 兼容性由官方 SDK 2.54.0 的 mock transport 与 Realtime 实际序列化路径验证，而不是靠复制官方文档文本。

### 4.2 未强行抽象的重复

`backend` 与 `execution-gateway` 的 readiness service、Postgres probe 和 RequestId middleware 存在少量同形代码。当前不抽取第三个运行时共享包，原因是：

- 两者是独立部署边界，服务级停机和依赖策略可能不同；
- 共享运行时包会把 API 与网关的生命周期、发布版本和依赖升级重新耦合；
- 重复范围是有界的操作基础设施，不承载公共协议或领域规则；真正必须一致的契约已在 `contracts/` 和集成测试中冻结。

若后续重复扩展到业务层、错误语义或调度规则，应新建独立平台包，而不是让一个应用导入另一个应用的内部模块。

## 5. 明确不采纳

- **不把公共错误和内部错误强压成同一 schema**：公共 envelope 必须兼容 OpenAI SDK；内部错误需要控制面诊断字段。
- **不要求 wire ID 用前缀表达类型**：ID 业务含义和授权不能由字符串前缀推断；生成代码可用 branded type 提供编译期保护。
- **不为“完整 OpenAI 兼容”扩张范围**：只承诺声明的 Audio / transcription-session 子集；`response.*`、conversation history、tool call、semantic VAD、SSE speech 和未声明参数显式拒绝。
- **不把 `JobEvent.data` 当作开放扩展口袋**：扩展事件必须增加可版本化的显式 variant，而不是塞入任意键。

## 6. 实现状态与残余风险

| 范围 | 状态 |
| --- | --- |
| 公共 speech / realtime / workbench / admin 契约 | 已交付并通过 schema、样例和 SDK 形状门禁 |
| 协调 DDL 原型 | 已交付并通过隔离 PostgreSQL 16 行为测试 |
| HTTP / WebSocket 业务路由 | 未实现 |
| 真实引擎 adapter、停止证据、容量与质量资格 | 未实现、未验证 |
| 生产迁移工具与运行角色权限 | DDL 位置已确定，Alembic / 独立迁移角色仍未落地 |
| OpenAI 历史基线无破坏 | 无法宣称：`speech-openapi.yaml` 与 Realtime schema 相对 `origin/main` 是新文件，兼容脚本按设计跳过 |
| 真实延迟、并发、长时恢复、RPO / RTO | 未验证 |

仍存在的结构性风险：`host_nodes` 单行准入热点需要压测；SQL 中的文本 ID / digest 正则与 JSON Schema 仍靠测试和后续生成器保持同步；quarantine 阈值、证据清理与审计保留期需要真实运维流程验证。

## 7. 验证证据

验证时间：2026-10-23，仓库根目录，基线 `9492b9e`。

| 检查 | 结果 |
| --- | --- |
| `make contracts-check` | 23 个契约通过 |
| `uv run pytest tests/contract -q` | 40 passed |
| `SPEECHNEXUS_TEST_DATABASE_URL=... uv run pytest -q` | 61 passed |
| `SPEECHNEXUS_TEST_DATABASE_URL=... uv run pytest -m integration tests/integration -q` | 12 passed |
| `uv run ruff format --check .` | 通过 |
| `uv run ruff check .` | 通过 |
| `uv run mypy` | 通过 |
| `uv run lint-imports` | 3 个依赖方向契约通过 |
| `CONTRACT_BASE_REF=origin/main make contracts-compat` | 当前公共契约与共享域相对基线均为新文件，按设计 skip；不能据此证明历史无破坏 |
| `pnpm format:check` | 通过 |
| `pnpm --reporter=append-only run lint` | 通过 |
| `pnpm typecheck` | 3 个 workspace 通过 |
| `pnpm test` | 2 个 Vitest 套件通过 |
| `pnpm build` | Admin / Workbench 构建通过 |
| `git diff --check` | 通过 |
| `make verify` | 完整仓库门禁通过（环境、契约、Python、Web、媒体、部署配置与运营健康 smoke） |

`make verify` 的服务 smoke 只覆盖当前已实现的运营健康端点、构建产物和隔离基础设施；语音 / 工作台业务 API、真实模型、质量、容量和长时恢复仍未验证。

## 8. 后续收口（同日增量）

上一节保留基线 `9492b9e` 的时点证据。后续复核继续修复了以下真实问题，并已分别提交、推送：

1. `AssistantTurn=success` 原先只要求 `output_text`，可以不引用实际 TTS Job；现在成功必须同时固定生成文本与 `speech_job_id`，任何非空 Job 引用也必须带生成文本。
2. `host_nodes` / `engine_instances` 的 reserved、incremental、quarantine 计数原先只依赖应用正确累加；现在事务结束时以实例基础预算与 admission lease 状态推导并校验计数，active / quarantine 槽位也分别与 lease 行对账。
3. 隔离实例原先可清空原因后回到 `stopping`，实例基础预算也并非 generation 不可变字段；现在隔离 generation 只能保持隔离或经核验停止，内存向量、槽位容量与实时保留在 generation 内不可缩小。
4. 新 admission lease 原先不与 pool / host / instance 的实时服务状态串行化；现在 active pool、ready node、ready generation 的检查使用行共享锁，与 drain / quarantine 转换竞争时不会放行错误准入。
5. 工作台 assistant voice / profile 与 render-unit reference 重复内联；现在统一引用 `shared/references.schema.json`。
6. 完整 Python 门禁发现 OpenAI 兼容脚本的本地变量类型过宽，已用显式拆分文档变量修复，未改变兼容比较行为。

本轮最终验证：

| 检查 | 结果 |
| --- | --- |
| `make verify` | 通过 |
| `uv run pytest -q`（含隔离 PostgreSQL） | 96 passed |
| `make contracts-check` | 25 个契约通过 |
| `pnpm test:contracts-sdk` | OpenAI JavaScript SDK 契约通过 |
| `CONTRACT_BASE_REF=e6aa84a make contracts-compat` | OpenAI 兼容路径无破坏性变化 |
| `uv run ruff format --check .` / `uv run ruff check .` | 通过 |
| `uv run mypy` / `uv run lint-imports` | 通过 / 3 个依赖方向通过 |
| `pnpm format:check` / ESLint / `pnpm typecheck` / `pnpm test` / `pnpm build` | 通过 |

这些证据仍不证明业务路由、真实引擎调用、端到端 SDK 请求、多节点容量、Alembic 迁移或生产恢复已经可用。对外可宣称的仍是：已声明的 OpenAI 子集与严格 `nexus` 扩展机器契约可由官方 SDK 形状消费；实现状态以仓库入口和后续真实运行验证为准。
