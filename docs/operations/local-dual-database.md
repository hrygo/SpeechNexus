---
title: "取消/中止 oracle 的本地双库验证"
status: active
date: 2026-11-06
---

# 取消/中止 oracle 的本地双库验证

本页给出在本机运行取消/中止 oracle 的 MySQL/InnoDB 参数的固定路径，让只有 MySQL 8 才暴露的
问题在推送前出现，而不是等到 CI 集成分片。入口是 `make abort-ledger-mysql`，底层由
[mysql-oracle.sh](../../scripts/dev-env/mysql-oracle.sh) 调用
[mysql-service.sh](../../scripts/dev-env/mysql-service.sh) 启动受管 MySQL。范围与证据边界见末节。

## 1. 范围与证据边界

- 运行 `tests/integration/test_gateway_abort_ledger.py`（随 PR #435 引入，已合并）的 `mysql` 参数：
  真实 MySQL 8.4.8 + InnoDB，与 PostgreSQL 侧共用同一组取消/中止场景断言。
- 只证明 MySQL/InnoDB 侧行为，不证明 PostgreSQL 侧、真实引擎停止或资源释放、HA 与产品 MySQL
  支持。单库通过不能报为双库验收。
- `-k mysql` 不连接 PostgreSQL；共享双库 fixture 仍要求两个 DSN 同时存在，脚本因此会在未设置
  `SPEECHNEXUS_TEST_DATABASE_URL` 时注入明确的不可达占位值。该占位值不是 PostgreSQL 证据。

## 2. 版本与实例

| 项 | 值 |
| --- | --- |
| MySQL | 8.4.8，镜像 `mysql@sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf`；CI 使用 `mysql:8.4.8` |
| 存储引擎 | InnoDB（容器启动参数 `--default-storage-engine=InnoDB`） |
| 字符集 / 排序 | `utf8mb4` / `utf8mb4_0900_bin` |
| 监听 | `127.0.0.1:55434`，仅回环 |
| 容器名 | `speechnexus-coordination-mysql` |
| 数据目录 | `.runtime/data/mysql`（Git 忽略） |
| 根口令 | `.runtime/config/mysql.env`，权限 `0600` |
| 资源上限 | 2 GiB 内存、2 个 CPU 核 |
| 生命周期 | Docker `unless-stopped`；脚本幂等安装与启动，不需要用户级 systemd |

CI 的 `integration_shards` 作业使用 `mysql:8.4.8`（3306，`root/speechnexus_test`）和
`postgres:16.15-alpine`；本地容器与 CI 同 MySQL 大版本，端口和口令不同，实际版本以
`SELECT VERSION()` 为准。容器本身、数据归属和端口占用须在执行前按目标主机核实。

## 3. 前置条件

- Docker 守护进程可用（`docker info` 成功）。Linux 参考宿主和 macOS Docker Desktop 都走同一脚本，
  MySQL 侧不依赖 systemd。
- 已同步锁定的 Python 3.14 环境（`uv sync --locked --all-packages --all-groups`）。
- PostgreSQL 仅在完整双库门禁时需要。只跑 MySQL 参数时不需要可达的 PG；要跑完整双库，另外提供
  隔离的 PostgreSQL 测试 DSN。

### 临时 PostgreSQL 实例

参考宿主的隔离 PG 不可用时，可为本轮验证启动一次性容器（仅回环、无持久卷）：

```bash
docker run --rm --name speechnexus-test-pg \
  -e POSTGRES_USER=speechnexus -e POSTGRES_PASSWORD=speechnexus_test \
  -e POSTGRES_DB=speechnexus_test \
  -p 127.0.0.1:55436:5432 postgres:16.15
```

另开终端确认就绪：`docker exec speechnexus-test-pg pg_isready -U speechnexus -d speechnexus_test`。
DSN 为 `postgresql://speechnexus:speechnexus_test@127.0.0.1:55436/speechnexus_test`。项目隔离 PG
固定使用 `127.0.0.1:55432`，这里刻意改用 `55436`，避免误连或覆盖项目实例。若 `55436` 或容器名
已被占用，先用 `lsof -nP -iTCP:55436 -sTCP:LISTEN` 与 `docker ps` 核实归属；只停止确认属于本验证的
`speechnexus-test-pg`，或换一个空闲回环端口并同步替换 DSN。不要停止或复用项目 `55432` 实例。
容器停止即删除数据，只服务本轮门禁，不是开发或生产服务；CI 使用同版本的 `postgres:16.15-alpine`。

## 4. 启动 MySQL

```bash
./scripts/dev-env/mysql-service.sh          # 初始化并启动；可重复执行
./scripts/dev-env/mysql-service.sh status   # 就绪探测，就绪时退出码为 0
./scripts/dev-env/mysql-service.sh stop
make mysql-service                          # 等价入口
```

脚本在首次运行时创建 `.runtime/data/mysql` 与权限 `0600` 的
`.runtime/config/mysql.env`，拉取固定摘要镜像，并以受管容器方式启动；就绪后输出：

```text
ok   coordination MySQL ready on 127.0.0.1:55434
```

DSN 由 `SPEECHNEXUS_TEST_MYSQL_URL` 传入，形如：

```bash
export SPEECHNEXUS_TEST_MYSQL_URL="mysql://root:<password>@127.0.0.1:55434/"
```

口令在 `.runtime/config/mysql.env`。URL 末尾的 `/` 必须保留：这里不指定数据库，测试为每个用例
创建独立的 database。脚本只会接管带 `speechnexus.role=coordination-mysql` 标签且镜像匹配的容器；
同名但归属或镜像不符的容器会被强制删除后重建，执行前须核对归属，见
[开发与测试环境](../development-environment.md#运行组件)。若 `SPEECHNEXUS_TEST_MYSQL_URL` 指向
其他隔离实例，脚本不会启动本地容器。

## 5. 只跑取消/中止 oracle 的 MySQL 参数

```bash
make abort-ledger-mysql
```

该目标依次执行：

1. `scripts/dev-env/mysql-service.sh` 启动（或复用）受管 MySQL；
2. 未设置 `SPEECHNEXUS_TEST_MYSQL_URL` 时，从 `.runtime/config/mysql.env` 推导 DSN；
3. 未设置 `SPEECHNEXUS_TEST_DATABASE_URL` 时，注入不可达的 PostgreSQL 占位 DSN；
4. `uv run pytest tests/integration/test_gateway_abort_ledger.py -k mysql -q`（该 oracle 随 PR #435
   引入，已合并）。

可覆盖的变量：

| 变量 | 作用 |
| --- | --- |
| `SPEECHNEXUS_TEST_MYSQL_URL` | 指向其他已获准的隔离 MySQL；脚本不再启动本地容器 |
| `SPEECHNEXUS_TEST_DATABASE_URL` | 提供真实隔离 PG DSN；`-k mysql` 仍只选 MySQL 参数 |
| `SPEECHNEXUS_MYSQL_ROOT_PASSWORD` | 仅在首次创建容器时生效的合成根口令 |

取消/中止 oracle 模块（`tests/integration/test_gateway_abort_ledger.py`）随 PR #435 引入（已合并）。
当前检出没有该模块时，目标以退出码 2 和明确提示失败，不跳过、不静默通过；此时可先用已存在的
InnoDB oracle 验证本机链路：

```bash
./scripts/dev-env/mysql-oracle.sh tests/integration/test_gateway_stop_evidence.py
```

## 6. 完整双库门禁

两个 DSN 都指向真实隔离实例、不加 `-k` 过滤，才是双库（oracle 随 PR #435 引入，已合并）：

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://... \
SPEECHNEXUS_TEST_MYSQL_URL=mysql://... \
  uv run pytest tests/integration/test_gateway_abort_ledger.py -q

# 协调事务共享门禁，同样要求两个 DSN
make coordination-test
```

只配置一个 DSN 会失败而不是跳过；跳过不算通过。SQLite 或内存 fake 不能替代这里的任一引擎。

## 7. 初始化与授权模型

- 测试管理员（本机 root）只用于建库、建角色和执行 DDL。每个用例创建独立 database
  （`sn_jobs_<suffix>` 等）与 `*_speech` / `*_gateway` 角色，结束删除。
- 产品运行角色不用于 DDL。回执与停止相关授权由 fixture 从产品授权文本
  （`gateway_abort_grants` 等，随 PR #435 引入，已合并）应用到测试库；MySQL 8 支持列级
  `GRANT UPDATE (col, ...) ON db.table TO 'role'@'%'`，不要为省事改成整表授权。
- 手工排查可以用 root 连接，但 root 口令与 DSN 只用于隔离测试，不进入产品配置、日志或报告。

## 8. 常见失败与排查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 建表报 MySQL 3813 | MySQL 8 拒绝列级 CHECK 引用其他列 | 把跨列约束改为具名表级 `CONSTRAINT ... CHECK (...)`；产品 DDL 的内联 CHECK 集合有守卫，改动时同步 |
| 写入报 MySQL 3819（`ER_CHECK_CONSTRAINT_VIOLATED`） | 数据违反 CHECK | 约束已生效；检查构造的非法行，不放宽约束来通过 |
| PG 通过、MySQL 读取结果判为损坏 | `IS NOT NULL` 一类表达式在 PG 返回 boolean、在 MySQL 返回 0/1；`is True` / `is not False` 把 1 判成损坏 | 用 `CASE` 让两侧返回整数，或让读取端同时接受 bool 与 0/1；不要只改断言掩盖差异 |
| 精确表集合断言失败 | MySQL 侧多出或缺少表，新表没有进入共享期望/排除集合 | 把新表加入共享集合（如 `abort_tables`，随 PR #435 引入，已合并），保留 `==` 断言；`alembic_version` 只存在于 PG |
| 授权失败 1142 / 1143 | 表或列权限不足 | 核对产品授权文本与 fixture；PG 对应 `InsufficientPrivilege`。列级 UPDATE 授权要与 PG 列集一致 |
| 连接被拒绝或超时 | 容器未就绪、端口被占或守护进程未启动 | `docker info`、`docker ps -a --filter name=speechnexus-coordination-mysql`、`lsof -nP -iTCP:55434 -sTCP:LISTEN`、`docker logs` |
| 拉取镜像失败或摘要不符 | 网络、镜像源或本地缓存的镜像异常 | 核对镜像摘要 `sha256:2952e3be…`；不要改用浮动标签替代 |

本机若把 uv 索引指向镜像源，`uv run` 可能把 `uv.lock` 的 source URL 重写为镜像地址；提交前用
`git restore uv.lock` 还原，或用 `UV_NO_SYNC=1` 复用已同步环境。该差异不改变包版本，但不应进入提交。

## 9. 排查命令

```bash
docker logs --tail 100 speechnexus-coordination-mysql
docker exec -it speechnexus-coordination-mysql mysqladmin ping -h127.0.0.1 -uroot -p
docker exec -it speechnexus-coordination-mysql mysql -h127.0.0.1 -uroot -p
```

`-p` 后不跟口令值，由客户端在终端交互读取；root 口令因此不进入 argv、日志或 shell history。

进入客户端后按需核对：

```sql
SELECT VERSION(), @@default_storage_engine, @@collation_server;
SHOW DATABASES LIKE 'sn_%';
SHOW CREATE TABLE sn_jobs_<suffix>.gateway_abort_intents;
SHOW GRANTS FOR 'sn_jobs_<suffix>_gateway'@'%';
```

- 失败后残留的 `sn_*` 库只来自测试；确认后可删除，不要清理未知库。
- 数据重置是破坏性操作：停止容器后删除 `.runtime/data/mysql` 需要明确确认数据范围。
- `-k mysql` 选择是否符合预期可用 `uv run pytest <module> -k mysql --collect-only -q` 核对。

## 10. 证据记录

记录实际命令、镜像摘要、`SELECT VERSION()`、pytest 选择与结果；区分通过、失败、跳过与未运行。
`make abort-ledger-mysql` 通过只覆盖 MySQL 参数；完整双库、引擎停止、资源释放和 HA 各自需要独立证据。
