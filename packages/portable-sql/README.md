# speechnexus-portable-sql

产品唯一的数据库访问契约，按 [ADR 0016](../../docs/decisions/0016-database-portability-core.md)
与[可移植数据契约设计](../../docs/architecture/database-portability.md) 实现。首发数据库是
PostgreSQL；本包不含任何数据库品牌分支，也不按品牌加载业务实现。

包内四件事：

| 模块 | 职责 |
| --- | --- |
| `subset.py` | 共同 SQL 子集的**唯一定义点**：禁止构造清单、参数标记识别与 `?` 计数。静态 guard 与运行时校验共用这一份。 |
| `contract.py` | PEP 249 的 qmark 子集：`Connection` / `Cursor` 协议、`Row`、参数与语句校验、异常类型。 |
| `schema.py` / `transaction.py` / `types.py` / `paging.py` | 权威时间探针与专用探针池、显式提交 / 回滚与 `commit_unknown`、文本与十进制边界、keyset 分页。 |
| `manifest.py` / `logical_schema.py` | statement manifest 与逻辑 schema 的定义、摘要和指纹；部署作业与 CI guard 共用同一实现。 |
| `odbc.py` | `pyodbc` 驱动的实现（延迟导入）。没有装驱动时导入不失败，`connect()` 给出明确错误。 |

声明式制品：

| 路径 | 含义 |
| --- | --- |
| `schema/logical.json` | canonical 逻辑 schema；指纹输入，物理 DDL 必须与它等价。 |
| `statements/manifest.json` | canonical statement manifest；`statements/manifest.digest` 固定其摘要。 |
| `statements/profiles.json` | 已登记 profile 的唯一来源；未登记的 profile 不得出现在 manifest 或部署配置中。 |

`subset.py` 必须保持 Python 3.12 可导入：`scripts/dev-env/check-portable-sql.py` 与 `tests/ci`
只带标准库运行，直接从这里读取禁止清单，避免规则出现第二份定义。

## 探针顺序

时间探针不是「读一次数据库时间」，顺序是契约本体：**先从专用探针池取得连接**，再开启写事务、
完成全部可能等待的写入，然后在专用连接上读
`CURRENT_TIMESTAMP, schema_rev, schema_fingerprint, authority_epoch`，最后回到写事务做资格复核。
池满时在开启写事务之前失败；不允许持有写锁等待共享池里的第二条连接。

## 不在本包

- 不提供 PostgreSQL / MySQL / Oracle / OceanBase 适配层，也不按品牌生成 SQL。
- 不建表、不搬迁数据。schema bootstrap 与存量数据搬迁是独立部署作业。
- 不定义 ORM 模型，也不承担运行时 DDL。
