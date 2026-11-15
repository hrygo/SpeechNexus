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

## 已知 flake 复现条件（`flake_harness`）

`#775` 记录过的“CI 等价条件”曾只存在于一次性会话里：没有入口，下一个人无法重放。现行入口是 [`flake_harness.py`](flake_harness.py) 与其支撑模块 [`tests/support/flake_conditions.py`](../support/flake_conditions.py)，把已确认会踩到的两类慢条件落成**显式触发**的单次运行：

- **提交慢**（slow commit）：PostgreSQL `commit_delay` 与 InnoDB `binlog_group_commit_sync_delay`。
- **往返慢**（slow round trip）：回环 TCP 延迟代理，两个 DSN 都过代理，每个转发 chunk 在每个方向各延迟一次。

### 何时使用

- 改动 `runtime_intent_deliveries` 的租约/退避窗口、outbox 行锁顺序，或任何在提交完成前读取时钟的断言时，用它确认用例在慢提交与慢往返下仍然成立。
- 用某条用例证明“有效静默期 = `delay −` 提交延迟”一类时序性质之前，先确认它在 10 ms/chunk 代理下不变。

条件会改写服务器全局参数并持续制造提交负载，因此**不默认开启**：没有 conftest 钩子、没有 marker 自动应用，直接运行 pytest 与 `make integration-test` 都不受影响；必须显式给出 `--condition`。

### 前置条件

- 两个隔离 DSN 必须同时可用，与集成套件一致：`SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL`。缺任一侧时 harness 以退出码 2 失败并点名变量，不跳过。
- 起库方式：InnoDB 用 `./scripts/dev-env/mysql-service.sh`（受管容器，`127.0.0.1:55434`）；PG 用项目隔离实例或[本地双库验证](../operations/local-dual-database.md#临时-postgresql-实例)里的一次性 `postgres:16.15` 容器。两者都只跑本机回环，不指向共享实例。
- PG 侧 DSN 需要超级用户权限（`ALTER SYSTEM` + `pg_reload_conf`）。`commit_delay` 单项无效，且上限为 100000 µs；harness 同时设置 `commit_siblings=1` 并保持 12 个忙提交连接。
- MySQL 侧需要 `log_bin=ON`（容器/CI 的 8.4 默认值）与 `SET GLOBAL` 权限；`binlog_group_commit_sync_delay` 对单个提交事务即生效，不需要忙提交负载。

### 用法

一条命令，`--` 之后是 pytest 参数（省略时跑本目录为这些条件维护的两条交付用例）：

```bash
SPEECHNEXUS_TEST_DATABASE_URL=... SPEECHNEXUS_TEST_MYSQL_URL=... \
  uv run python -m tests.integration.flake_harness --condition latency-proxy
```

| `--condition` | 机制与生效证据 | 预期 |
| --- | --- | --- |
| `latency-proxy` | 回环代理，`--chunk-delay-ms`（默认 10）按 chunk 双向延迟；启动时打印两个 DSN 的代理端口 | `test_mismatched_receipt_and_retry_never_reopen_submission` 稳定绿（`#795` 的退避窗口已提到 30 s）；`test_ack_rechecks_expiration_after_the_actual_outbox_row_lock` 稳定绿（租约窗口 `LEASE_WINDOW_US` = 2.5 s）；把该窗口回退到产品下限 1 s 时稳定红：`Failed: ACK did not encounter the real outbox row lock` |
| `commit-delay` | PG `commit_delay=100000` + `commit_siblings=1` + 12 个忙提交；InnoDB `binlog_group_commit_sync_delay=200000`；启动时打印基线与条件下的提交中位数 | 提交中位数从 <1 ms 升到约 100 ms（PG）/ 220 ms（InnoDB），statement 往返不变 |
| `both` | 两类条件叠加 | 用于确认租约窗口同时满足“大于建立耗时”与“阻塞等待小于 3 s `innodb_lock_wait_timeout`” |

`--commit-delay-us` 同时覆盖两个引擎的值；PG 超过 100000 会在启动时被拒绝。条件未达到配置延迟的一半中位提交延迟时 harness 直接失败，防止把“没有生效的条件”当成证据。

### 清理

正常退出与被测用例失败时都由 `ExitStack`/`finally` 收尾：`ALTER SYSTEM RESET commit_delay`、`ALTER SYSTEM RESET commit_siblings` + `pg_reload_conf` 并读回参数值确认，`SET GLOBAL binlog_group_commit_sync_delay` 还原为运行前的值，停止忙提交线程、删除临时 schema、关闭代理监听。若进程在启动条件后被强杀，按同一顺序手工复原并在 `SHOW commit_delay`、`SELECT @@GLOBAL.binlog_group_commit_sync_delay` 上核对。

### 它证明什么、不证明什么

- 证明：指定用例在**对端提交慢**或**TCP 往返慢**时仍给出断言要求的结论；未修用例在这些条件下给出报告里的确定性失败。即“只能由当时那个 agent 重现”变成了任何人可重放的一条命令。
- 证明：条件确实生效——启动输出包含基线/条件下的提交中位数、代理端口映射，条件不达标即失败。
- 不证明：真实网络故障、丢包、HA、跨主机时钟偏移、真实慢磁盘或 fsync。代理只延迟字节，`commit_delay` 只延迟提交，两者都不模拟宕机与崩溃恢复。
- 不证明：租约/退避的产品设计本身正确；它只说明既有断言在这些条件下成立。
