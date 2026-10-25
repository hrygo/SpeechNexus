# 团队 C：独立工作台真实 PostgreSQL 服务入口

核验日期：2026-10-24。C00 #35 / C05 #40 / C08 #43 的增量交付；完整 C00–C08 与 G0–G5 继续未完成。

## 入口和实际行为

`speechnexus_backend.workbench.runtime:create_app_from_env` 从显式环境配置装配 `PostgresVersionUnitOfWork`、`PostgresDeletionUnitOfWork(meetings=True)` 和 `PostgresMeetings`。不使用测试 fake、不导入 speech 内部模块、不连接引擎或在启动时调用 LLM/对象存储。现有生成契约与 18 项 C HTTP 操作保持一致，LLM 关闭时纪要新请求明确返回 `llm_disabled`，基础会议保存/封存仍可用。

配置由部署环境/外部秘密管理提供，不自动读取 `.env`，不把凭据放进命令行。准备好 owner 迁移与以下配置后运行：

```bash
uv run uvicorn speechnexus_backend.workbench.runtime:create_app_from_env \
  --factory --lifespan on --host 127.0.0.1 --port 8081
```

必须保留 `--lifespan on`；关闭 ASGI lifespan 会跳过启动检查，不属于受支持入口。此命令只是部署入口说明，本轮没有启动持久服务。

| 环境变量前缀 `SPEECHNEXUS_WORKBENCH_` 后缀 | 要求 |
| --- | --- |
| `DATABASE_URL` | 必填；独立非 owner 应用角色 DSN，秘密由部署环境提供 |
| `DATABASE_SCHEMA` | 必填；C 独立 schema，禁止 speech/public/系统命名空间 |
| `WORKSPACE_ID` | 必填；明确配置的共享内容分组，不是用户或安全隔离 |
| `DEPLOYMENT_ID` | 必填；稳定部署标识，用于 scope 游标 |
| `CURSOR_KEY` | 必填；32–4096 UTF-8 字节，保持部署内稳定，由秘密管理提供 |
| `ALLOWED_HOSTS` | JSON 数组；默认仅 `127.0.0.1` / `localhost`，无通配符或端口 |
| `ALLOWED_ORIGINS` | JSON 数组；默认空，带 Origin 的浏览器请求须显式批准；不开放 CORS |
| `MAX_CONNECTIONS` | 默认 8，范围 1–64，每进程数据库连接上限 |
| `MAX_JSON_BYTES` | 默认 1048576，范围 1–4194304 字节 |
| `BODY_TIMEOUT_SECONDS` | 默认 3，范围 (0,120] 秒 |

Host 必须唯一且匹配配置；重复、非法或未批准 Origin 拒绝，使用既有生成 `ErrorResponse` / `invalid_request`。不信任客户端端点或转发头。无 Origin 的 headless 调用允许。反向代理、TLS、实际允许域名与部署网络的完整资格由目标部署另验。

## schema 与权限

`workbench.infrastructure.runtime_schema.bootstrap_sql(schema)` 是供 **owner 单个事务** 执行的 fresh schema 组合，不是运行时迁移器或现存 schema 升级器。它顺序组合五份既有 C 片段：版本、删除、封存、纪要任务、会议，最后写入 C `runtime_schema_revision=1`。既有片段不变；重复 namespace 失败，异常由 caller 事务整体回滚。`runtime_grants_sql(schema, role)` 给现存非 owner 应用角色授予已有最小权限及 stamp SELECT，不能修复权限或创建角色。

> 时点说明：以上为上一单元事实。后续[服务端提词持久化](2026-10-24-team-c-teleprompter-persistence-handoff.md)新增第六份片段（提词状态 + 回执）并把 `SCHEMA_REVISION` 升至 **2**；`stamp=1` 的旧基线现被启动检查 fail-closed 拒绝，须重新 fresh bootstrap。

全局版本号、部署角色和迁移顺序仍由 A 集成；不能把本地 stamp 冒充 A 的正式迁移历史。已有 C schema 需要经 A 评审的迁移适配，不可重跑 fresh bootstrap。物理 cleanup 是另一个角色/worker，本入口只持久接受删除并立即施加读取/写入屏障，不宣称删除队列已物理清空。

ASGI lifespan 执行只读数据库检查：连接后先 `SET ROLE NONE` 并显式 `SET search_path = pg_catalog`/`statement_timeout = 3000`，再 `BEGIN READ ONLY`；身份查询取 `session_user` 作为登录身份并要求 `session_user = current_user`，业务连接在每次短事务前同样 `SET ROLE NONE`，因此 DSN、`PGOPTIONS` 或 service file 里的 `-c role=` 不能使业务身份偏离启动检查身份。检查 namespace、非 owner/member 角色和危险全局权限、20 张普通表、读/追加权限、精确 22 项允许 UPDATE 列与唯一 stamp；拒绝 owner/schema CREATE、危险历史/物理 DELETE/TRUNCATE/TRIGGER、扩大或缺少列授权、缺表/错误 stamp。结束直接关闭只读连接；业务沿用既有短事务、锁/语句期限和未知提交恢复。检查失败只给固定错误，不暴露 DSN、SQL 或内部对象名。这是启动快照检查，不代表完整权限审计或抵御部署 owner 的后续修改。5 秒是应用级取消目标，驱动句柄与 socket 清理可能延长实际墙钟，不构成硬总期限（延期 Minor）。

## 终审两项 Important 修复（2026-10-24）

终审返回 2 项 Important，均已按 RED→修复→GREEN 顺序完成，相关完整回归重跑。

1. **启动检查与业务连接身份差异**。真实 PG 复现：同一 DSN 加 `options=-c role=<BYPASSRLS 角色>` 时，修复前预检因覆盖 `options=` 看到登录角色（安全），业务连接 `current_user` 却是提权角色，项目创建返回 503 `store_unavailable`（`.runtime/team-c/review-fixes-4b08/reproduction.log`）。修复：`check_database` 与 `PostgresVersionUnitOfWork.transaction` 连接后均执行 `SET ROLE NONE`，预检显式设置 `search_path`/`statement_timeout` 且要求 `session_user = current_user`。独立取证确认 `RESET ROLE` 对启动包 `-c role=` 无效，只有 `SET ROLE NONE` 恢复登录身份，故实现采用后者。新增真实 PG lifespan/HTTP 用例 `test_dsn_role_switch_cannot_change_checked_or_business_identity` 先 RED（503）后 GREEN。
2. **URL 配置解析异常回显输入**。`allowed_origins` 端口非法时 `urlsplit(...).port` 抛出的 `ValueError` 消息引用原始输入，经 pydantic 进入校验错误；`allowed_hosts` 同类路径也做了加固。修复：`runtime_settings` 用 `_explicit_host`/`_explicit_origin` 包裹解析与端口访问，任何解析异常都转为固定消息。3 项合成敏感串参数化用例先 RED（消息含标记）后 GREEN。

## 实测与未验证

- 配置/Ingress/namespace 相关 37 项通过，包含缺项/保密/独立环境前缀、不读 dotenv、Host/Origin 拒绝、敏感串不回显和 owner helper 不得目标 speech。
- PostgreSQL 18.6 的一次性 Unix socket-only 集群：310 passed / 23.75s，无跳过。新 runtime 单元 16 项在原有 fresh bootstrap 回滚/重复拒绝、13 类危险或缺失配置、真实 factory/lifespan/TCP HTTP、项目/稿件/会议创建封存、LLM关闭、重建读取和原键重放、级联删除 404 之外，新增 DSN 角色切换身份一致用例。
- 同一 PG 轮次另跑既有 C 会议/删除生产 bundle Chromium（fake LLM）2 项通过；它们各自范围保持。本轮新 runtime 的直接证据是实际 lifespan/TCP HTTP，没有冒称所有浏览器页面都已接独立入口。
- Python unit/contract 3488 passed / 63.02s，format 601、ruff、mypy 444、7 依赖边界和 28 源契约/生成漂移通过，7 条门禁命令均 0。相对修复前 3485 的增量是 3 项敏感串参数化用例。Web源码未变，复用上一单元五门禁和1604项结果，本轮未重跑Web测试/构建。
- 证据日志、复现探针和门禁结果位于 `.runtime/team-c/review-fixes-4b08/`；未启用 coverage。独立停止检查另记。

本轮为 Mac 合成内容/隔离 PG，未连接共享数据库、启动 MinIO、使用私人音频或执行模型。工作台对象区、speech Artifact 导入、cleanup/纪要/export worker、LLM 集成启用配置、A 全局迁移/部署与 Linux/真实设备/模型/完整网络和 a11y 资格仍待。工作台关闭不影响语音底座的架构边界保留，尚未在实际生产拓扑做独立故障演练。

## 交接版本与裁决

base=head `4b08c396c0fe8fc43c7e85683e534befab6bb22b`，未消费无关 B #95。生成器、源契约、依赖和 A 共享 root/迁移/deploy/锁文件不变，源 0.1.0.dev0、bindings/package 0.1.0。旧 338 路径/`41515f7fb48962be615f0d196200aef584ab52e0d6f2d988ce76ec4261e2c5ec` 包保存在 `.runtime/team-c/team-c-before-runtime-root.*`；修复后最终累计包为 347 路径（8 个旧文件变化、9 个新增文件），干净 apply、全字节一致、真实 index 未变，精确 SHA-256 与逐文件摘要见 `.runtime/team-c/team-c-current.manifest.json` 和对应 `.patch`；该 hash 同时写回九个 issue 正文。精确证据和 ledger 位于 `.runtime/team-c/runtime-root-4b08/` 与 `.runtime/team-c/review-fixes-4b08/`。没有 C commit/push 或新跨 chat 消息。

本轮裁决，依决策顺序：

1. 固定旧基线、内联 C 入口并保留证据：不改 A 公共写入点；代价是后续 A 集成仍需补验。
2. fresh owner baseline + stamp：运行时无自动 DDL/升级；代价是现存 schema 升级和正式迁移资格仍待。
3. LLM 关闭使用既有 `llm_disabled`：纠正测试 `llm_unavailable` 预期，不改变协议；错误分类若混淆会掩盖关闭与故障区别。
4. 新入口校验 Host/Origin、精确列权限：代价是部署域名/Origin 或角色配置错误会拒绝请求，完整代理/网络/权限资格仍待。
5. 终审两项 Important 以「显式重置会话身份 + 解析异常固定消息」修复，而非依赖 DSN 校验或静默覆盖：代价是 DSN 中的角色切换被忽略而非报错，部署者不会得到提示；`SET ROLE NONE` 是唯一对启动包 `-c role=` 有效的重置方式，`RESET ROLE` 无效。

十项既有 Minor 继续：cursor 空/过大措辞、外部 owner 预检确认措辞、两会议共享 Transcript 并发回归、Meeting 非法成功响应、失败 GET 后旧 conflict/useLatest、Meeting 卸载后 GET、ASR send 失败前计数、TTS done 后未播完缓冲的停止按钮、Tab 目标焦点断言、旧 Workspace 计划出站拦截措辞。本单元审查另新增延期 Minor：启动检查 5 秒为应用级取消目标而非硬墙钟期限。这些都不因入口可启动而关闭完整 Goal。
