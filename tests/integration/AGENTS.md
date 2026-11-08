# 集成测试目录规则

继承[根规则](../../AGENTS.md)。执行前读取 [README.md](README.md) 的隔离配置与证据范围；协调逻辑变更使用 [speechnexus-coordination-change](../../.agents/skills/speechnexus-coordination-change/SKILL.md)。

- 只连接本项目隔离的测试服务，使用合成或最小脱敏数据。测试管理员可以创建和清理独立 namespace、角色、用户、桶；产品运行角色不因此获得管理员或 DDL 权限。
- `make coordination-test` 与 `make integration-test` 必须同时设置 `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL`。后者还要求隔离 MinIO endpoint 与测试密钥；完整变量见[验证指南](../../docs/agent-verification.md)。
- 直接运行共享事务 pytest 时，两个 DSN 任一缺失都会失败并点名变量，不因两个都缺而跳过。单库通过或跳过不能报为双库验收；缺配置时保留门禁失败并报告条件。
- PostgreSQL 是正式产品目标；InnoDB 验证关键协调路径，不能推导为全产品 MySQL 支持。共用行为 oracle 的引擎映射遵循测试说明，不能为通过某库而放宽业务语义。
- 并发证据需要独立连接和可观察的锁等待/交错，不能用 sleep 或顺序调用替代。双库测试中的合成停止事实不能证明真实引擎停止，丢弃响应不能证明真实网络故障或 HA。
- `tests/support/` 原型不能供产品导入。历史 PG 门禁与当前受控事务各有保护边界；按 [ADR 0007](../../docs/decisions/0007-legacy-coordination-retirement.md) 迁移有效不变量、核对退出条件后移除废弃门禁，不永久并养旧原型。

Make 目标可能启动受管服务。先核对实际宿主、服务、端口与当前授权；缺少条件时报告未验证范围，不改向共享实例。具体命令和运行态说明统一见[验证指南](../../docs/agent-verification.md)。

## PostgreSQL DSN 与隔离 schema

### DSN 与失败语义

执行 PostgreSQL 的集成或迁移用例（包括 `*_pg.py` 和包含 Alembic 升级/降级的用例）在当前用例或 fixture 中只读取 `os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")`。变量缺失时调用 `pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")`；不得用 `pytest.skip`、本地默认连接或导入期异常代替，错误信息必须说明调用方需要提供隔离 DSN。

数据库名、Unix socket 目录、用户、密码和连接参数一律由该 DSN 决定。不得写 `postgresql:///sn_profile_it`、`dbname=sn_profile_it`、固定的 socket 路径或补默认凭据。需要 SQLAlchemy URL 时只从取得的 DSN 派生，例如 `url.replace("postgresql://", "postgresql+psycopg://", 1)`。

### 跳过守护

`test_pg_dsn_skip_guard.py` 静态扫描 `tests/integration` 与 `tests/support`，解析 `pytest.skip`、`pytest.xfail`、`pytest.mark.skipif` 及对应的导入别名，并沿同文件嵌套函数、跨文件共享 helper 的调用参数传播 DSN 来源。`tests/support` 中自读数据库 DSN 的模块不依赖调用参数传播，其跳过直接上报，fixture 注入也不例外。缺任一数据库 DSN 的跳过会使守护失败并报告文件、行号与变量；白名单为空。`shutil.which("psql")`、引擎 dialect 分支、MinIO 与 Workbench 专用变量属于非数据库前置，不按数据库缺 DSN 处理。

### 独立 schema 生命周期

需要独立 schema 的用例必须自行生成唯一名称（例如 `"sn_retry_mig_" + uuid.uuid4().hex[:12]`），不得复用固定 schema 或共享表状态。在 `try` 中创建 schema，在 `finally` 中执行 `DROP SCHEMA IF EXISTS "<schema>" CASCADE`，并在同一清理段关闭连接、释放 engine。创建失败、断言失败和并发分片都必须走这条清理路径。

### 双 DSN

同时需要 PostgreSQL 与 MySQL 的用例必须把 `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL` 作为一个整体检查。两者都配置时才执行数据库分支；被选中执行的用例缺少任一侧时 `pytest.fail` 并点名缺失变量，不得只跑已配置的一侧，也不得用 `pytest.skip` 把配置错误降级为未执行。skip 只能表示“未验证”，不能作为单库或双库验收证据。

```python
VARIABLES = ("SPEECHNEXUS_TEST_DATABASE_URL", "SPEECHNEXUS_TEST_MYSQL_URL")
configured = {name: os.environ.get(name) for name in VARIABLES}
missing = [name for name, value in configured.items() if not value]
if missing:
    pytest.fail(
        "dual-database tests require isolated PostgreSQL and MySQL DSNs; missing "
        + ", ".join(missing)
    )
```

### 正例

现存的 [`test_public_job_retry_migration_pg.py`](test_public_job_retry_migration_pg.py) 在用例入口读取调用方 DSN，缺失即失败，并为每次运行创建唯一 schema、在 `finally` 中清理：

```python
url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
if not url:
    pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")

schema = "sn_retry_mig_" + uuid.uuid4().hex[:12]
engine_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(engine_url, poolclass=NullPool)
admin = psycopg.connect(url, autocommit=True)
try:
    admin.execute(f'CREATE SCHEMA "{schema}"')
    # Configure Alembic with this schema and connection, then run the migration.
finally:
    admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    admin.close()
    engine.dispose()
```

### 反例

PR #445 修正前的 `test_public_job_progress_migration_pg.py` 使用了被修掉的写法：

```python
URL = "postgresql:///sn_profile_it"

engine_url = URL.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(engine_url, poolclass=NullPool)
admin = psycopg.connect(URL, autocommit=True)
```

随机 schema 不能抵消这个错误：schema 仍会建在作者本机的 `sn_profile_it`，而不是 CI fixture 指定的数据库；缺少该库时连接在迁移断言前就失败。禁止通过只随机化 schema、保留固定库名或增加本地默认 DSN 来“修复”它。
