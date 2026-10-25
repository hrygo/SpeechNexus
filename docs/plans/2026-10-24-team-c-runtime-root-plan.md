# 工作台真实 PostgreSQL 服务装配计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans 内联实施，整个单元一次 fresh 终审；不提交、推送或删除证据。

**Goal:** 提供独立工作台 ASGI factory，将既有版本/删除/会议业务装配到真实 PG，启动前拒绝未准备的 schema 和危险角色，LLM 默认关闭。

**Architecture:** C 专用环境配置，没有默认 DSN/workspace/cursor key。owner-only 新 schema bootstrap 组合既有五份冻结片段并追加 C schema revision stamp，供 A 协调迁移；运行入口只有只读检查，无 DDL 或隐式升级。lifespan 检查后通过已有生成契约 handlers 提供业务，不导入 speech 内部实现或 fake。

**Tech Stack:** 现有 Pydantic Settings / FastAPI / psycopg、C PostgresVersionUnitOfWork/PostgresDeletionUnitOfWork/PostgresMeetings。

**Spec:** [三队计划](2026-10-23-three-team-delivery-plan.md) C00/C05/C08；[产品设计](../speechnexus-product-architecture.md) 工作台独立边界；[仓库布局](../architecture/repository-layout.md)。完整部署、对象区和 worker 仍待，不能据本入口关闭父包。

## Global Constraints

- 当前基线固定 `4b08c396c0fe8fc43c7e85683e534befab6bb22b`；保留338路径累计包/全部并行改动，不追逐无关B #95。
- C 不写共享 app/pyproject/deploy/锁文件或 A 全局迁移序列；bootstrap 仅供独立 fresh schema、非通用升级器，由 A 排队集成。
- 运行配置只读显式环境，不自动加载 `.env`；DSN/key 为 SecretStr，不落凭据或暴露于日志。
- schema 不得为 speech/public/系统命名空间；运行角色不得 owner/member、superuser/createdb/createrole/replication/bypassrls、持有 schema CREATE、历史全表UPDATE 或物理DELETE/TRUNCATE/TRIGGER。
- 无 speech 导入、启动时 LLM/对象存储/语音外发；未配置服务不使用fake。既有会议基于Transcript，save_audio仍不支持。
- 独立HTTP入口只接受配置Host和显式Origin（无Origin的headless请求允许）；重复/非法/外部authority拒绝，使用既有生成ErrorResponse，不引入新业务协议。
- 数据库操作有连接/事务/锁/语句期限；运行时不DDL、不自动重试不确定写入。cleanup worker与LLM worker尚未从本入口启动。

## Review Focus

1. 错 DSN/schema/role、缺表/权限与损坏stamp不能误启动；错误不能回显秘密。
2. owner bootstrap 原子、失败回滚且不接触其他schema；运行角色无法更改stamp/历史或DDL。
3. 真实lifespan而非测试注入或假readiness；重建应用后数据/原键回执仍由PG恢复。
4. 会议默认LLM关闭仍可保存/封存，状态与未实现speech/audio/worker能力准确。
5. 应用停止/启动失败不遗留连接或改写业务，Host部署/共享网络边界不被本机HTTP结果冒称通过。

### Task 1: owner片段和严格配置

**Files:** 新建 `workbench/runtime_settings.py`、`workbench/infrastructure/runtime_schema.py`；新增对应unit及PG测试。
**Interfaces:** `WorkbenchSettings`，`bootstrap_sql(schema)`、`runtime_grants_sql(schema,role)`，`SCHEMA_REVISION=1`；既有五份SQL片段保持不变。

> 时点说明：`SCHEMA_REVISION` 后续由[服务端提词持久化](2026-10-24-team-c-teleprompter-persistence-handoff.md)升至 2（新增第六份片段），本行保留上一单元时点值。

- [x] RED/GREEN：配置缺项、保密、限值/保留schema；bootstrap fresh原子/重复拒绝，stamp不可伪写，精确角色grant。

### Task 2: 独立ASGI factory与启动检查

**Files:** 新建 `workbench/runtime.py`、`interfaces/runtime_ingress.py`；修改 C `app.py` 和 `interfaces/http_versions.py` 传入标准lifespan；新建 `tests/integration/test_workbench_runtime.py` 和配置/ingress unit测试；为已有PG探针添加显式 `--workbench-runtime`。
**Interfaces:** `create_runtime_app(settings)`、`create_app_from_env()`；真实PG检查，创建既有具体服务，无fake/LLM。

- [x] RED/GREEN：危险角色/缺表/权限拒绝；真实lifespan/HTTP创建项目、稿件、会议和封存；LLM关闭；重建factory重读/原键重放/删除屏障。stamp错误已有实际PG覆盖。
- [x] private socket-only PG312项与既有业务浏览器通过，无跳过；独立停止检查另见本机记录。

### Task 3: 资格、审查与交接

**Files:** 双语README、计划索引及新handoff；本机ledger/累计包。

- [x] Python门禁/相关PG，Web和契约按实际影响验证；3488 Python/310 PG及静态/契约门禁通过，另加2项 Chromium 浏览器回归，Web源码未变复用上轮1604/五门禁；未宣称完整生产迁移/部署/对象区/LLM或Linux资格。
- [x] 一次fresh终审返回2项Important：真实隔离PG复现并阻断DSN options/生效角色导致的启动检查与业务连接身份差异；合成敏感串复现并修复URL配置解析异常回显输入。两项先RED后GREEN，相关完整回归通过；Minor延期。
- [x] 累计补丁干净apply/全字节/index/链接核对；九issue任务/状态及最终hash写后逐字回读；完整Goal active。

## 终审修复（2026-10-24）

| 发现 | RED 证据 | 修复 | GREEN 证据 |
| --- | --- | --- | --- |
| 启动检查与业务连接身份差异 | 真实PG：DSN `options=-c role=<BYPASSRLS角色>` 时预检看到登录角色，业务连接 `current_user` 为提权角色，项目创建 503 `store_unavailable` | `runtime.check_database` 连接后 `SET ROLE NONE` 并显式 `SET search_path=pg_catalog`/`statement_timeout`，身份查询改查 `session_user` 且要求 `session_user=current_user`；`PostgresVersionUnitOfWork.transaction` 连接后同样 `SET ROLE NONE` | 新增真实PG lifespan/HTTP 用例通过；`RESET ROLE` 对启动包 `-c role=` 无效、`SET ROLE NONE` 才恢复登录身份已单独取证 |
| URL 配置解析异常回显输入 | `allowed_origins` 端口非法时 `urlsplit(...).port` 的 ValueError 消息引用原始输入，经 pydantic 回显 | `runtime_settings` 以 `_explicit_host`/`_explicit_origin` 包裹解析与端口访问，异常一律转为固定消息 | 3 项敏感串参数化用例通过，`allowed_hosts`/`allowed_origins` 错误不再包含输入 |

延期 Minor 新增：5 秒启动检查是应用级取消目标，驱动清理可能延长，不构成硬墙钟总期限。
