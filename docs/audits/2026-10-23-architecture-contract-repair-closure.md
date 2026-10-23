# 架构与契约审查修复闭环

日期：2026-10-23。原审查基线：`811ff05b1d8f646b49505b6d3744ec4a6edb052b`。

验收状态：已完成本轮架构与契约层修复及验证。交付范围为架构、机器契约、契约工具、测试与协调
DDL 草案；用户另行授权两份现有运营 DTO 的声明约束对齐。业务服务保持未实现。

## 逐项修复证据

| 项目 | 修复结果 | 可复核证据 |
| --- | --- | --- |
| F01 · 完整执行命令 | 严格任务/阶段判别命令，实际输入、全量有效参数、固定资源、canonical digest；TTS 文本握手与提交/结束/重放 | [command schema](../../contracts/internal/execution-command.schema.json)、[内部控制](../../contracts/internal/execution-control.openapi.yaml)、[stream schema](../../contracts/internal/execution-stream.schema.json)、[协议语义](../architecture/execution-command.md)、`test_canonical_command_*`、`test_all_canonical_command_variants_*`、`test_internal_tts_text_*`、`test_internal_text_negotiation_*` |
| F02 · 执行证据与 fencing 历史 | 禁止删除/TRUNCATE；水位作用域不可改名；运行角色不能删除历史、执行 DDL 或访问验证队列 | [DDL](../../migrations/0001_cluster_coordination_draft.sql)、`test_fencing_is_monotonic_*`、`test_history_cannot_be_truncated`、`test_runtime_roles_cannot_erase_evidence_*`；实际 PostgreSQL 执行而非 SQL 字符串断言 |
| F03 · 预算低报 | 六维 reservation 至少为 slots × 固定 per-slot 预算；CPU/PCM/scratch 要显式声明；多 stage 共用实例保守累计 | 同一 DDL、`test_reservation_cannot_underreport_slot_budget`、`test_ordered_transaction_recipes_under_actual_lock_contention[last_slot]` |
| F04 · 同步恢复 | 保留 OpenAI error envelope，在 nexus 扩展提供原 Operation，内容不可重放时提供原成功 receipt | [errors](../../contracts/shared/errors.schema.json)、`test_sync_recovery_requires_*`、`test_lost_sync_response_recovers_same_operation_through_completion`、[JavaScript SDK 回归](../../tests/contract/test_openai_js_sdk.mjs) |
| F05 · Receipt 投影 | requested/effective/observed 均为允许字段明确的公开事实；复用 ASR/TTS 参数域；正文仅允许 presence 布尔事实；missing/unknown 是允许的稳定路径 | [receipt](../../contracts/shared/receipts.schema.json)、`test_receipt_rejects_nested_private_data`、`test_receipt_rejects_private_reference_*`；REST 与 Realtime 共同验证 |
| F06 · 兼容门禁 | 区分请求域与响应保证，处理新增必填参数/body、缺失边界、required/enum/type、引用交集及响应头；复杂约束保守拒绝 | [比较器](../../scripts/dev-env/check-openai-contract-compat.py)、[33 项回归](../../tests/contract/test_contract_compat.py)；对比 HEAD 与 e6aa84a |
| F07 · 意图不可变 | runtime intent 的六维请求预算与 deadline 一并冻结，观察字段仍可协调 | DDL 的 `enforce_runtime_intent_identity_and_terminality`；`test_unresolved_intents_are_unique_while_history_is_retained` 逐字段数据库负例 |
| F08 · 运营投影一致性 | 递归解析组件与 nullable 表达，比较类型/枚举/必填/长度/数量；两份既有 DTO 补齐声明约束 | [运营契约回归](../../tests/contract/test_operational_contract.py)；包含组件变异和实际 ASGI HTTP 响应验证 |
| F09 · 锁序 | 准入、submit、加载、释放及人工协调使用同一顺序；包括 FK 与触发器的隐式锁，同表按主键排序 | [事务模板](../architecture/distributed-coordination-schema.md)、8 个受控双连接交错；通过 pg_blocking_pids 确认实际阻塞后放行 |
| F10 · 删除闭环 | 每模块自有的 plan/create/query，依赖版本/摘要 pin、精确确认、异步清理、独立副本、停止证据与恢复账本 | [deletion schema](../../contracts/shared/deletion.schema.json)、[删除规范](../architecture/data-deletion.md)、Speech/Workbench 新契约入口、`test_deletion_*` 与 `test_accepted_deletion_replays_*` |
| R1 · 依赖门禁 | interfaces/infrastructure 为独立同层；speech/workbench 与 backend/gateway 独立；公开 SDK 客户端不被误禁 | [配置](../../pyproject.toml)、[合成 import graph 违规测试](../../tests/contract/test_import_boundaries.py)，实际运行 5 条 import-linter 契约 |
| R2 · 历史增长与重复聚合 | 活跃谓词及 partial indexes；私有事务验证队列按 host/generation 去重，flush 后新修改重新验证 | DDL、`test_capacity_verification_is_deduplicated_and_rearms_after_flush`、角色权限及临时表冒充负例 |

数据库测试统一位于 [协调 DDL 集成测试](../../tests/integration/test_cluster_coordination_schema.py)。
新增协议、私有字段负例和消费者时序位于 [架构修复契约测试](../../tests/contract/test_architecture_repairs.py)。

## 兼容与架构裁决

[ADR 0005](../decisions/0005-complete-execution-and-data-lifecycle-contracts.md) 记录有意的
内部 control 0.2.0.dev0 / stream 1.1.0 升级与 Nexus receipt 2.0.0 收紧；旧内部
生产者和消费者须一起升级，不能以放宽校验兼容旧私有 payload。当前没有已实现的
语音业务运行态，未执行持久数据迁移。OpenAI 标准请求与字段意义保持原契约；
409 恢复信息使用 nexus 扩展。删除是新增协议，不能视作已有运行时能力。

当前骨架的 SRP/DIP、模块所有权、组合根与共享契约方向得到保留；通过依赖门禁和
投影校验加强 DRY。未实现业务的 OCP/LSP/ISP、引擎取消和停止替换性质仍需后续真实
实现及共享行为测试，不能从这些契约用例推断已经获得运行资格。

## 最终验证

| 实际执行 | 结果 |
| --- | --- |
| `uv sync --locked --all-packages --all-groups` | 锁定安装检查通过；新增 dev 依赖 rfc8785 0.1.4 |
| 配置项目隔离 DSN 后 `uv run pytest --cov --cov-report=term-missing -q` | **204 passed**，无跳过；包含 **49** 项 PostgreSQL 测试；覆盖率显示 84%，达到 80% 门槛 |
| PostgreSQL | 项目端口 55432 的隔离测试库，16.15；测试 schema 和临时角色已回滚/移除 |
| `make contracts-check` | **27** 份契约通过 OpenAPI/JSON Schema 与本地引用检查 |
| 比较器 `--base HEAD` 与 `--base e6aa84a` | 均通过；已由负例确认新增必填/边界/响应保证等原漏洞会报错 |
| `pnpm test:contracts-sdk` | **3 passed**，覆盖标准调用与两种 409 恢复投影 |
| `uv run lint-imports` | **5 kept, 0 broken**；另有合成违规及合法 SDK 导入回归 |
| Ruff format/check、mypy | 均通过 |
| 修改文件本地 Markdown 链接、JS 测试 Prettier、`git diff --check` | 均通过 |

最终验证树包含本报告所述全部代码、契约和 DDL 修改。后续仅补齐本报告和索引，
未再更改已验证的协议或实现。

未运行 `make verify`、本轮 Web 全量构建/浏览器/服务 smoke、真实模型/引擎、负载或
恢复演练；这些不属于本轮实际通过证据。删除恢复和文本流测试是契约消费者 oracle，
不是已部署服务的端到端验证。活跃集合聚合成本仍取决于实际活跃量，未宣称吞吐改善
幅度。角色授权函数与协调 DDL 仅应用于项目隔离测试 schema，未部署到生产运行态。

本轮未 commit、push、发布、下载模型或处理私有音频。历史审查记录不改写。
