# 生产 owner 任期事务

本单元属于 [A01 #20](https://github.com/hrygo/SpeechNexus/issues/20)，在包内
`0002_coordination` 与既有 A/B 最小角色上实现 `acquire_owner`、`renew_owner`、
`activate_owner` 以及 `query_outcome(pool)` 的生产路径。不修改 B 的屏障端口，
不导入测试原型，不新增机器协议 operation 或 HTTP 路由。

## 实现与使用

`domain/owners.py` 独立定义任期、命令、屏障事实与裁决；application 暴露四个窄方法，
不自动重试未知结果。`owners_sql.py` 只执行显式 SQL，不拥有连接或 commit；
`owners_postgres.py` 管理实际 PG 事务与驱动失败。边界经生成的 coordination 离线
bundle 校验输入/完整输出，再将 DTO 转为领域对象。JSON 整数的等价数字表示在转换
时映射为 Python int，不修改原 DTO，不接受数字字符串、布尔值或有小数部分的计数。

```python
from speechnexus_backend.speech.application.owners import OwnerService
from speechnexus_backend.speech.infrastructure.owners_postgres import PostgresOwnerTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.interfaces.owner_transactions import OwnerTransactionBoundary

# 由部署注入 A 的非 owner 运行角色 DSN；schema 是服务端配置。
pool = speech_pool(runtime_database_url, schema="speech")
pool.open()
try:
    boundary = OwnerTransactionBoundary(OwnerService(PostgresOwnerTransactions(pool)))
    result = boundary.acquire_owner(
        {
            "pool_id": "tts-realtime",
            "owner_id": "scheduler-main",
            "expected_version": "1",
            "ttl_ms": 60000,
        }
    )
finally:
    pool.close()
```

控制面先创建目标池；运行角色不通过 acquire 自动创建配置。共享池 min=0、max=4、
等待队列最多 16，Read Committed/UTC/显式 search_path；`pg_temp` 显式放在权威 schema
和 `pg_catalog` 之后，避免默认临时表优先级覆盖真实表。不在模块导入时打开连接。
原 Job 模块的 `job_pool`/连接类型入口继续可用，委托相同池策略。

## 任期、时间与激活

获取先锁 pool，核对 version 且旧任期已到期，再增加 fencing token 和 version，
进入 `reconciling_takeover`。同请求重复发送不会再发一个 token：旧 version 冲突，
调用方读取当前权威。续约要求 owner/token/version 全匹配且旧任期仍有效；到期不能
靠 renew 复活。fencing/时间遵循协议安全整数范围，version 遵循有符号 BIGINT；
溢出拒绝，不重置高水位。

激活锁 pool、非 stopped 实例、未释放 lease、屏障及在途 submit，按既有全局顺序
和标识字典序执行。领域规则要求每个受影响 gateway 的持久屏障达到当前 token，且
没有 submitting/executing/stopping 提交。所有相关受控写入者也必须先锁 pool；
不能用任意直接 SQL 绕过这个跨所有者串行化前提。

时间来自数据库，在等待 pool/屏障等行锁之后取得；获取/续约的到期时间与激活前的
有效性判断都使用这次实际时间。PG 每条语句按剩余预算设置 timeout，排队和已确认
回滚的最多三次重试共享预算，commit 前预算耗尽会回滚。它不是已验证的任意网络
黑洞/数据库提交硬截止时间。

激活不调用物理 fencing，不替 B 生成或证明屏障；它消费 B 所有表中的事实。
未知执行即便已隔离，也继续占预算。owner 任期/激活不删除历史、不释放 lease、
不归还节点/实例/quota；真实屏障和在途收敛仍需 B 的生产实现与联合证据。

## 未知恢复与边界

只有 PG 已确认中止的 deadlock/serialization failure 允许整事务有界重试，且抖动
在连接归还后发生。连接/提交确认丢失返回 unknown/query_current_authority，不自动
再次 acquire、renew 或 activate。`query_outcome` 在此边界只接受 pool target，使用
read-only 事务返回完整 OwnerSnapshot：authority、expires_at_us、owner_state 和 version。
空池或未分配 owner 返回 unknown，不伪造零 token 的 authority，也不补写池。

读取的是当前事实，可能已经是其他 owner 或到期的历史任期，不证明原调用一定提交，
也不授予新执行权限。后续事务必须重新核对当前身份、token、version 与数据库时间。
其他 query target 仍由其事实所有者实现，本单元不更改 Job 的 ClaimSnapshot 映射。

```sh
uv run pytest tests/unit/test_production_owners.py -q
# 两个项目隔离测试 DSN 都必须配置，跳过不算资格。
make coordination-test
make integration-test
```

PG 使用实际 adapter/包内迁移/最小运行角色，InnoDB 只用测试 driver 执行相同生产
SQL/领域裁决。同组行为涵盖 version/token/expiry、全部 gateway 屏障、在途提交、
预算不释放、两个调度者实际行锁竞争及等待到期后续约/激活拒绝。
PG 故障测试在 commit 前终止真实会话，或在实际 commit 后模拟丢失回执，验证只查询
当前事实且不再获取 token；后者不等于真实网络丢包或 HA 资格。

runtime/admission/claim、B 的真实 seal/dispatch、scheduler 循环与多主机仍待后续。
ADR 0007 的旧不变量尚未全部进入生产路径，继续保留旧门禁。
