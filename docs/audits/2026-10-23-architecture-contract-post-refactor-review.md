---
title: "集群协调重构后的架构与契约审查"
status: reviewed
version: "0.1.0"
date: 2026-10-23
---

# 集群协调重构后的架构与契约审查

本文复核基线 `68b2452`（`refactor: reduce PostgreSQL coordination coupling`）之后的整体架构、
技术方案、机器契约完整性 / 合理性 / 兼容性，以及 SOLID / DRY 合规性。结论只以本文列出的
命令、样例与文件为限：契约校验和原型测试通过不代表业务路由、真实引擎、容量或质量资格已实现，
也不代表远端 CI 已运行。

## 1. 结论

架构方向成立且未降级。PostgreSQL 仍是唯一正式产品数据库，跨表业务裁决由受控应用事务承担；
活动 DDL 为 15 张普通表、318 行，无业务函数、触发器、扩展、部分唯一索引或 GiST 区间约束；
旧 24 个函数与 45 个触发器的原型只保留在
[`legacy_postgresql.sql`](../../tests/fixtures/coordination/legacy_postgresql.sql) 中继续跑
53 项 PG 回归；不引入 etcd，Redis 只作可选缓存或唤醒；集群支持与单机同构要求保持原样。

本轮发现并修复了六类真实缺陷，其中两类是权限与门禁漏洞：运行角色可以自行放宽 attempt 配额、
改写拓扑身份、扩大 allocatable 容量并读取其他所有者的规范命令正文；本地双库门禁在只配置一个
DSN 时会静默跳过另一个引擎，仍以成功结束。另有两类属于契约完整性缺口（8 个端口缺少结果样例、
`query_outcome` 的返回快照类型未与查询目标绑定），一类是测试原型忽略冻结请求字段与 version
前置条件，一类是 DDL 与授权脚本重复维护却无机器校验。修复全部落在契约、测试 / fixture、工具与
文档范围内，未改业务源码。

仍开放的项见第 6 节：契约类型生成未落地（P0-A 计划项）、OpenAI 标准路径之外的兼容门禁范围有限、
4 个端口尚无行为原型、原型返回便捷事实而非完整 `value` 快照。

## 2. 审查范围

读取范围：`contracts/`（内部协调事务契约、执行 / 运行时控制 OpenAPI、共享域）、
`migrations/0001_cluster_coordination_draft.sql`、`docs/architecture/`（coordination-transactions、
distributed-coordination-schema、technical-blueprint、repository-layout）、
`docs/plans/2026-10-23-cluster-coordination-refactor.md`、`scripts/dev-env/`、
`tests/{contract,integration,support,fixtures}` 与 `Makefile`。

执行范围：契约校验与兼容比较、全量 pytest 与覆盖率门禁、Ruff / mypy / import-linter、
`git diff --check`，以及在两个隔离引擎（本机 PostgreSQL 16.15 与一次性 MySQL 8.4.8 /
InnoDB 容器）上运行完整集成矩阵。未运行：远端 CI、`make verify` 的浏览器 / 服务 smoke、
真实模型与引擎、物理多主机、主备切换与备份恢复。

## 3. 发现与修复

| 编号 | 问题 | 为什么是真实缺陷 | 修复与门禁 |
| --- | --- | --- | --- |
| F-01 | speech 运行角色获 `workspace_quotas.max_attempts`、`policy_revision`、`host_nodes.topology_digest`、`quarantine_threshold_ppm` 与全部 `allocatable_*` 的 UPDATE | 运行角色可自行提高并发 attempt 上限、改变节点身份、扩大可分配容量或降低隔离阈值，与"预算/策略/拓扑配置属于控制面"和最小权限冲突 | 从授权映射移除这些列并停止动态授予 `allocatable_*`；新增负例（`max_attempts`、`policy_revision`、`topology_digest`、`quarantine_threshold_ppm`、`allocatable_device_memory_bytes`）在 PG/InnoDB 上均被拒 |
| F-02 | 所有表对两个角色授表级 `SELECT` | speech 角色因此可读 `submit_intents.request_document`（私有规范命令正文），网关角色可读 `runtime_instance_intents.request_document` | 非所有者角色改为列级 `SELECT`（身份 / 状态列，不含正文）；原型相应改为显式列查询；新增负例 `SELECT request_document` 在两库均被拒（PG 42501 / MySQL 1143） |
| F-03 | `make integration-test` 与 `verify.sh` 只要求 PostgreSQL DSN | 共享事务 oracle 的 MySQL 一半会 `skip`，命令仍以 0 退出，单库结果可被当成双库证据 | Make 目标要求两个 DSN；共享事务用例在"只配置一个引擎"时按失败而非跳过处理；`verify.sh` 同时提供并导出两个隔离默认值；文档同步 |
| F-04 | 18 个端口只有 10 个有成功结果样例，且没有覆盖断言 | 结果语义（快照类型、recovery、`error_code` 约束）对 8 个端口没有可执行证据 | 补齐 8 个端口的结果样例，并断言 `valid_results` 的操作集合等于契约的 18 个端口 |
| F-05 | `query_outcome` 的 `value` 未与查询目标绑定 | 消费者无法静态判断会收到哪种快照；lease 查询在契约上可合法返回 `RuntimeSnapshot` | 在 `query_outcome` 的每个 target 分支声明 `x-nexus-result-kind`（pool→OwnerSnapshot、attempt→AdmissionSnapshot、job→ClaimSnapshot、submit→ExecutionSnapshot、lease→LeaseSnapshot、interval→FactSnapshot、runtime_intent→RuntimeSnapshot），方案文档同步，测试校验声明与 fixture 结果一致 |
| F-06 | 测试原型忽略冻结请求字段（`mark_execution_unknown`/`quarantine_lease` 的 `reason_code`、`observed_at_us`，`release_lease`/`activate_owner` 的 `expected_version`，`seal_barrier` 的 barrier 文档），`create_job` 无同键同摘要重放 | 契约冻结的前置条件与重放规则没有行为证据，原型不能作为端口验收证据 | 原型改为消费冻结请求对象：版本前置条件、首次观察原因不改写、终态不可降为 unknown、Job 同键同摘要重放 / 异摘要或异 job 键冲突；新增 3 项负向用例（两库各 3，共 6） |
| F-07 | DDL、授权脚本与所有权映射各自维护，无一致性校验 | 新增或改名一个列就会让授权脚本静默漂移，权限门禁不再覆盖真实列 | 新增静态测试：授权表集合等于 DDL 表集合、所有授权列都存在于 DDL、控制面专属列不出现在任何角色的可写列、`request_document` 只出现在所属角色的列级 UPDATE |

## 4. SOLID 与 DRY 判断

依赖方向仍然成立：`uv run lint-imports` 保持 5 个契约通过、0 破坏；`domain` 不依赖框架、
生成 DTO、数据库或 vendor；`tests/support/` 的事务原型不被产品代码导入（见依赖契约与文件头说明）。
公共协议、内部协议与领域对象分开建模，公开 DTO 不暴露内部端点、实例身份或凭据。

契约单一来源在本轮加强而非削弱：共享域继续用 `$ref` 复用；契约测试仍拒绝重复内联 schema 与
孤儿组件（`tests/contract/test_p0_public_contracts.py`）；新增的授权一致性测试把"DDL 是唯一列事实来源"
变成机器检查，减少了 F-07 那类复制式维护。`query_outcome` 的映射从隐式知识变成契约内的显式声明，
生成器与消费者可据此产出类型，不再需要散文约定。

仍未落地的 DRY 承诺：Python / TypeScript 边界类型应从契约生成或严格校验
（[technical-blueprint.md §3](../architecture/technical-blueprint.md#3-契约与类型生成)、P0-A 工作包），
当前只有运营健康 DTO 做了手写模型与契约一致性检查，生成器与生成产物均不存在。

## 5. 兼容性判断

`CONTRACT_BASE_REF=origin/main make contracts-compat` 通过：OpenAI 标准路径、标准 Realtime 事件
及其可达共享域相对基线无非兼容变化。本轮没有触碰 `contracts/speech-openapi.yaml`、
`contracts/realtime-events.schema.json` 或 `contracts/shared/`。

内部协调事务契约仍是 `1.0.0`。本轮对它的改动是补全性的：新增 `x-nexus-result-kind` 声明
（非校验关键字，既有读者与生成器不受影响）与结果样例。映射声明放在请求侧的 target 分支，而不是
`TransactionResult`：结果文档不携带 `target`，写在那里会是一条永不触发的条件。既有样例的合法性
未改变，`release_lease`、`admit_recipe`、`create_runtime_intent` 等等价约束保持原样。

原审查时点，PR 只对 OpenAI 标准面做机器 diff；管理、工作台和内部契约依赖源校验、样例与
ADR 0005 的变更记录。当前按原 A00 / 三队计划§7，补入管理、工作台、runtime-control 和
execution-control 四份HTTP源的已知结构变更检查。其余JSON/WS、授权和业务语义仍需样例与
消费者验证，不因工具通过扩大兼容承诺，见第8节。

## 6. 未验证与残余风险

| 范围 | 状态 |
| --- | --- |
| 当前受控事务原型 | 14 个端口有双库行为证据；`create_runtime_intent`、`observe_runtime`、`query_outcome`、`record_execution_observation` 只有契约与样例，无行为原型 |
| 原型返回形状 | 提交返回结果字符串与便捷事实（token、claim version），未按契约返回完整 `value` 快照；快照形状由契约样例与直接状态断言覆盖，生产适配器须自行产出 |
| 契约类型生成 | 未落地（P0-A 计划项） |
| 生产业务接入 | 未实现：无 Scheduler、worker、gateway、adapter、路由或迁移工具 |
| 集群资格 | 未验证物理多主机、节点丢失、网络分区、主备切换与备份恢复；当前只有单机逻辑节点 |
| 引擎与质量 | 未验证真实模型、停止证据来源、延迟与吞吐 |
| MySQL 部署关系 | MySQL 仅用于关键协调路径的可移植性验证；授权输出的 MySQL 主机模式仍为 `'%'`，安装前须按部署收紧 |
| 远端 CI | 未运行；本地证据不替代远端结果 |

## 7. 验证证据

验证时间 2026-10-23，仓库根目录，基线 `68b2452`，本机隔离 PostgreSQL 16.15（127.0.0.1:55432）
与一次性 MySQL 8.4.8 / InnoDB（127.0.0.1:55434，镜像摘要
`sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf`，与集成说明一致）。

| 检查 | 结果 |
| --- | --- |
| `make contracts-check` | 28 份 OpenAPI / JSON Schema 与本地 `$ref` 通过 |
| `CONTRACT_BASE_REF=origin/main make contracts-compat` | 通过 |
| `uv run pytest tests/contract -q` | 220 passed |
| `uv run pytest tests/unit -q` | 9 passed |
| `uv run pytest --cov --cov-report=term-missing` | 229 passed、131 skipped（未设 DSN），覆盖率门禁通过 |
| `SPEECHNEXUS_TEST_DATABASE_URL=... SPEECHNEXUS_TEST_MYSQL_URL=... uv run pytest -m integration tests/integration -q` | 131 passed、0 skipped（旧 PG 回归 53 项；共享事务 78 项，PG / InnoDB 各 39） |
| 只配置 PostgreSQL DSN 运行共享事务用例 | 退出码 1，报错"needs both engines configured"（不再静默跳过） |
| 只配置 PostgreSQL DSN 运行 `make integration-test` | 退出码 2，提示两个隔离 URL 均为必需 |
| `uv run ruff format --check .` / `uv run ruff check .` | 通过 |
| `uv run mypy` | 60 个源文件通过 |
| `uv run lint-imports` | 5 kept、0 broken |
| `git diff --check` | 通过 |
| 两库测试残留 | `sn_coord_*` namespace / database 0，`sn_speech_*`、`sn_gateway_*` 角色 / 用户 0 |

## 8. 已决策事项与明确不采纳

以下按当前实施校对；第2项仍服从原计划验收，不以默认延期代替其可比较部分：

1. `query_outcome` 的目标→快照映射（F-05）**维持按职责归属冻结**：查池返回池单据、查尝试返回尝试
   单据，契约与 fixture 保持当前形状；若要改为对 job / interval 返回更弱的 `FactSnapshot`，须走
   显式契约变更并同时改测试。
2. 内部、工作台与管理面按**原 A00 / 三队计划§7**补可自动比较部分；此前推迟表述不构成已授权
   的计划变更。当前新增[四份HTTP源结构检查](../architecture/http-contract-change-checks.md)，保留
   版本/变更说明、样例和消费者交接要求。纯JSON、WS顺序/授权及业务语义仍未据此完成，不扩大兼容承诺。
3. 契约类型生成**已开始接入**（实例 drain 的请求/响应为生成产物），生成器版本随锁文件固定；
   其余面按后续单元逐个接入，不一次性铺开。

明确不采纳：

1. 不把跨表业务规则搬回数据库函数 / 触发器：这与 ADR 0006 减少数据库绑定的目标冲突，且已删除的
   24 个函数仍以 fixture 回归保护其历史证据。
2. 不给运行角色全表 UPDATE，也不为省事把私有正文重新授给非所有者角色；需要更多列时扩展显式列表。
3. 不用"两个引擎都配了就算过"这一假设替代证据：缺 DSN、跳过或基础设施故障都不算通过。
4. 不为遗留原型补写新的数据库特有机制（`SKIP LOCKED`、GiST、延迟触发器）以求单库兜底。

[架构文档](../architecture/README.md) · [审查记录](README.md) · [重构计划](../plans/2026-10-23-cluster-coordination-refactor.md)
