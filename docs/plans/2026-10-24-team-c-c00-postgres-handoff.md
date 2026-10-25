---
title: "SpeechNexus C00 PostgreSQL 版本事务交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C00 PostgreSQL 版本事务交接

后续进展见 [C00 删除事务交接](2026-10-24-team-c-c00-deletion-handoff.md)：基线已快进到 `f210821d655bd7deb326825d750d83584c3ce159`，补充 V2 tombstone、物理正文清理及删除回执竞争修复。以下保留本轮 `b3e0ed3` 的原时点证据；当前版本适配器须与 V2 一并集成，不能据此仅部署 V1。

## 身份与边界

- 团队 C / C00 #35，[持续目标](2026-10-24-team-c-goal.md)仍为 active；本单元不关闭工作包或 Gate。
- 基线 `b3e0ed340763b827333747d464b68fd7f4981116`，分支 `codex/team-c-c00-versions`。本轮没有 commit、push、外部消息或远端状态修改；没有承载未提交改动的 `head_sha`。
- 延续[领域/应用版本单元](2026-10-24-team-c-c00-version-handoff.md)。参照同基线 workbench/shared 契约 `0.1.0.dev0`；未修改公共机器契约、生成器、锁文件、部署配置或迁移框架。
- 当前模块可表达并保存项目和转写版本。生产组合根、HTTP、列表/游标、对象区、speech 导入核验及业务删除尚未接入；不能据此宣称 C00 或真实文件闭环完成。

## 实现

`backend/src/speechnexus_backend/workbench/infrastructure/postgres_versions.py` 实现既有 `VersionUnitOfWork`。每次使用独立、非池化连接，READ COMMITTED，先锁资源 header，再读取不可变版本，避免锁等待后仍消费旧 statement 中的关联 head。数据库时钟在获取锁后读取。新稿件须锁定父项目，修改须从锁定快照进行单步版本转换；运行角色只可追加历史和更新 head 列。

`version_schema.py` 提供版本化迁移片段和最小运行角色授权片段。项目身份、项目历史、稿件身份、稿件历史、幂等回执均属于工作台 schema；没有 speech 表外键、内部导入或引擎调用。head 外键延迟到提交检查，历史/回执外键及来源约束防止缺失版本与跨稿件引用。`numeric(19,0)` 保存共享 Revision 的完整 19 位范围；领域层同时补齐项目计数上限。

幂等键只保存 SHA-256 摘要。事务级 advisory lock 序列化尚不存在的 scoped key，完整 `(operation, scope_id, key_digest)` 主键决定回放身份；锁桶碰撞只增加等待。回执引用原始不可变版本，后续编辑不会改变重放结果；内容、head 和回执在同一短事务提交，未提交内容不会成功返回。

连接槽默认 8、连接建立期限 5 秒、请求期限 10 秒、锁等待 2 秒、SQL/idle-in-transaction 5 秒。事务退出另有默认 1 秒预算，取消或异常时关闭该私有连接；不等待无限长的取消握手来归还应用槽。请求期限后清理最多另用退出预算。运行时固定 `search_path=pg_catalog`，不执行 DDL；错误不包含原始 SQL、DSN 或业务正文。

COMMIT 阶段只有完整性错误和明确的 `40001` / `40P01` 证明回滚；传输错误、`57P01`、`40003` 及未分类错误保留 `commit_unknown`。不自动重试。取消继续传播，消费者必须保留原 scoped key 重查；此内部错误尚未映射成公共 HTTP 协议。

## 本机证据

核验时间：2026-10-24（Asia/Shanghai）。macOS、Python 3.14.9、psycopg 3.3.6、PostgreSQL 18.6（Homebrew）。仅使用合成内容。

`scripts/dev-env/workbench-pg-probe.py` 每次在 `.runtime/team-c/` 新建临时集群，关闭 TCP，只开放私有 Unix socket；清除子进程继承的 `PG*` 连接默认值，防止 `PGHOSTADDR` / `PGSERVICE` 改变目标。测试写入前核对专用数据库名、owner、空 `listen_addresses` 和实例标记。每项数据库测试独立 schema/非 owner 角色，探针在 `finally` 停止集群并核对 PID 文件和 `pg_ctl status`。保留日志与合成数据以便检查，不访问共享 5432、现有服务、模型或 MinIO。

装好 PostgreSQL 工具并将其 `pg_config` 放入 PATH 后，可从仓库根目录复现：

```bash
uv run --locked python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin "$(pg_config --bindir)" --coverage
uv run --locked pytest tests/unit/backend/test_workbench_postgres_cleanup.py -q
```

| 检查 | 结果与边界 |
| --- | --- |
| 隔离 PG 探针 | 31 passed：15 项数据库/故障场景与 16 项参数边界；工作台 infrastructure 分支覆盖率 91.42%；实例已确认停止 |
| 卡住的清理 fake | 4 passed：业务期限、提交、业务异常、外部取消；验证请求终止、socket 关闭、会话失效和唯一连接槽可重用 |
| `uv run --locked pytest tests/unit tests/contract -q` | 348 passed；不把未配置服务的集成跳过计入通过 |
| `uv run --locked ruff format --check .` / `ruff check .` | 135 files formatted；lint passed |
| `uv run --locked mypy` / `lint-imports` | 80 source files passed；6 kept、0 broken |
| `pnpm format:check` / 文档格式、本地链接检查 | passed；26 个相关本地链接有效 |

本轮未改 Web、公共契约或生成包的实现；复用同基线 C02 交接中的 Web 115 项测试、build 和契约漂移证据，不声称本轮重跑。全部 C 单元的未提交补丁保存在 `.runtime/team-c/team-c-current.patch`，对应基线、文件摘要、补丁 SHA-256 与重放核验记录见旁边的 `team-c-current.manifest.json`；打包不修改真实 Git index。

数据库场景包括历史回放、来源引用、原文不可变、完整 decimal 范围、同键并发认领、异键 CAS、取消后回滚、提交期真实外键失败、权限拒绝、锁超时及实际锁等待后的新 head/时钟。锁等待用 `pg_blocking_pids` 观察，非仅依靠 sleep 推断。

回包丢失测试先让真实服务器提交，再在应用返回或驱动事务退出处注入异常，随后原键回放同一资源。它不证明真实网络丢包、进程崩溃、主从切换或 HA 恢复。清理卡住由内存 fake 复现，不证明操作系统/真实网络黑洞已通过。

本机证据路径 `.runtime/team-c/c00-pg-probe.json` 指向本次集群、pytest/coverage 日志及停止结果。未配置专用 DSN 的普通 pytest 会跳过数据库场景；跳过不算通过。

## 审查与修复

独立只读审查发现两项必须修复，均由主代理补充失败测试后修复：

1. 原先整类 SQLSTATE `40` 当作已知回滚，遗漏 `40003`。实际 COMMIT 后注入该错误，先观察失败，再改为明确允许的回滚码；原键回放恢复同一身份。
2. 外层 `asyncio.timeout` 无法约束取消后的事务退出等待。先复现清理卡住与连接槽未释放，再增加独立退出预算、关闭私有 socket 和有界任务清理，4 项回归通过。

非阻断、后续补充：transcript 独立 SQL 分支的并发 CAS/等待后 head 测试，以及直接观察缺省回执 advisory wait。当前 project 有真实等待证据，回执并发有结果/行数断言；不将其泛化为所有交错均已证明。

图谱未索引本仓库，当前工具面不能创建索引；结构判断采用直接源码核对。

## 集成条件与未验证项

1. A 协调把模块迁移片段纳入 Alembic/全局 owner 锁与部署步骤，在版本化 owner 事务执行并核对角色不继承额外权限；运行进程不能成为 owner 或执行 DDL。片段不会自动检测/迁移已有 schema，遇到同名 namespace 应失败。
2. C/A07 完成工作台版本化删除预检/确认、tombstone、回执清理和删除竞争。回执虽然不复制正文，仍能定位历史内容，必须随所属业务生命周期清理；删除后不能通过原键重新公开内容。在此前不接生产暴露。
3. C/A 接公共 workbench DTO、稳定错误/提交未知恢复、列表/游标与组合根；speech 导入必须走公开 Artifact/receipt 核验，无跨模块表写入。
4. Linux PostgreSQL 16/镜像、迁移框架、MinIO、真实多进程/跨主机/HA、真实提交网络故障与负载、删除恢复均未验证。协调层既有双库测试不能代替本模块证据，本模块 PG 测试也不增加产品 MySQL 支持。

PostgreSQL 行锁与 READ COMMITTED 语义核对自[官方锁文档](https://www.postgresql.org/docs/18/explicit-locking.html)和[隔离级别文档](https://www.postgresql.org/docs/18/transaction-iso.html)；驱动事务边界参考 [psycopg 官方文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)，实际行为以本次锁定依赖与测试结果为准。
