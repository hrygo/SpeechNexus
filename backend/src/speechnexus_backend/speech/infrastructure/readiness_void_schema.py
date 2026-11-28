"""就绪收据的显式作废审计表:``runtime_readiness_voids``。

**这张表为什么存在。** #1282 现场:账本记第 1 代、节点准备好第 11 代,续租每 5 分钟以
``instance_generation_mismatch`` 失败、小时报把那一行标 ``invalid``,但准入只看**已签发的收据**:
那张 24 h 前签的收据在到期之前仍然 ``valid``,``create`` 照样放行。报告说红、准入说行的窗口,
宽度就是收据 TTL(#1286)。本表是「检测即作废」留下的痕迹:检测方(续租)在这里写一行,准入与
报告用它判「这张收据签完之后,这个实例代次被作废过」。

**只追加。** 一行事实:哪个实例、账本哪一代、节点报的是哪一代、凭什么、何时、从哪条路径发现。
没有 ``UPDATE``/``DELETE`` 授权面。恢复不做「撤销作废」,而是让下一次**成功**的续租签一张新收据:
新收据的 ``verified_at_us`` 晚于 ``voided_at_us``,作废就不再适用于它——覆盖靠时间序,不靠改写
历史。这条规则只有一个定义点(``speech/domain/readiness_void.py``),报告与准入都调它。

**作废只由「确定不一致」触发。** 控制器不可达、超时、响应不合格式都是「不确定」:那条路径**不写**
本表,因为配置写错一次不应该立刻打断生产准入。两种情形在报告里仍可区分——确定的记
``instance_generation_mismatch``,不确定的记控制器自己的错误码(``controller_unreachable`` /
``controller_refused`` / ``controller_head_invalid``)。

**「哪一次作废仍然有效」只有一个读法。** ``latest_void_at_us_expression`` 生成的标量子查询由报告
(``engine_readiness``)与准入(``admission_sql``)共同引用;两边对同一个瞬间不可能给出不同答案。
列清单同理只在 ``READINESS_VOID_COLUMN_DEFINITIONS`` 一处,建表文本、插入列序与授权面都从它派生。
DDL 的消费者只有 alembic 迁移树(``0072_readiness_voids``),运行时角色不做 DDL。
"""

from __future__ import annotations

#: 审计表的物理名字:迁移、写入语句、授权面与两处读法都引用它,不再写字面量。
READINESS_VOID_TABLE = "runtime_readiness_voids"

#: 逐列物理类型,顺序即建表顺序,也是插入语句的列序。``instance_id`` 的定长与 collation 抄自
#: ``engine_instances``:外键两侧的字符类型与 collation 必须一致,否则 PostgreSQL 建表即失败。
READINESS_VOID_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("instance_id", 'VARCHAR(128) COLLATE "C" NOT NULL'),
    # 账本里这个实例当前那一代(收据为它签名);被作废的是这一代。
    ("instance_generation", "BIGINT NOT NULL"),
    # 控制器自己报的当前代次:作废的法律事实是节点的证词,不是操作者的判断。
    ("observed_generation", "BIGINT NOT NULL"),
    ("reason", 'VARCHAR(64) COLLATE "C" NOT NULL'),
    ("void_source", 'VARCHAR(32) COLLATE "C" NOT NULL'),
    ("voided_at_us", "BIGINT NOT NULL"),
    # 当时的完整分歧说明,便于事后审计「凭什么作废」而不必重放控制器。
    ("document", "TEXT NOT NULL"),
)

#: 列名投影,只从上面那份声明派生。
READINESS_VOID_COLUMNS: tuple[str, ...] = tuple(
    name for name, _ in READINESS_VOID_COLUMN_DEFINITIONS
)

#: 写入语句写整行,所以插入列就是声明列。
READINESS_VOID_INSERT_COLUMNS: tuple[str, ...] = READINESS_VOID_COLUMNS

#: 同一次分歧只留一行:同一实例、同一账本代次、同一节点代次重复检测不会写第二遍(续租每 5 分钟
#: 跑一次,主键让重跑去重,审计不被刷屏)。
READINESS_VOID_PRIMARY_KEY: tuple[str, ...] = (
    "instance_id",
    "instance_generation",
    "observed_generation",
)

#: 作废行必须指向真实存在的实例代次。没有这条外键,实例行删掉之后仍会留下「属于某个不存在代次」
#: 的作废记录,而准入按实例查询时永远读不到它为什么被作废过。
READINESS_VOID_FOREIGN_KEY: tuple[tuple[str, ...], str, tuple[str, ...]] = (
    ("instance_id", "instance_generation"),
    "engine_instances",
    ("instance_id", "instance_generation"),
)

#: 确定不一致的作废原因码。它同时是准入拒绝的内部分码(公开响应仍按契约答
#: ``engine_state_mismatch``:契约里的公开词汇表是冻结的,新增一个公开码要走契约变更)。
READINESS_VOID_REASON_GENERATION_MISMATCH = "instance_generation_mismatch"

#: 检测路径。当前只有续租(小时报是只读的,它不写本表),留列是为了以后接入别的检测方时能区分。
READINESS_VOID_SOURCE_RENEWAL = "renewal"

#: 建表文本。约束风格与 ``coordination_schema`` / ``generation_adoption_schema`` 一致:全部表级
#: CHECK(列级 CHECK 引用别的列在 InnoDB 上是 3813)。``IF NOT EXISTS`` 不是保险:若干集成用例把
#: ``alembic_version`` 回退再前滚,迁移必须可重放。
READINESS_VOID_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS {READINESS_VOID_TABLE} (\n    "
    + ",\n    ".join(
        f"{name} {definition}" for name, definition in READINESS_VOID_COLUMN_DEFINITIONS
    )
    + f",\n    PRIMARY KEY ({', '.join(READINESS_VOID_PRIMARY_KEY)})"
    + f",\n    FOREIGN KEY ({', '.join(READINESS_VOID_FOREIGN_KEY[0])}) "
    + f"REFERENCES {READINESS_VOID_FOREIGN_KEY[1]} "
    + f"({', '.join(READINESS_VOID_FOREIGN_KEY[2])})"
    # 作废的是「另一代」:两代相同就不存在分歧,那是一条错误的作废。
    + ",\n    CHECK (observed_generation <> instance_generation)"
    + ",\n    CHECK (instance_generation >= 1)"
    + ",\n    CHECK (observed_generation >= 1)"
    + ",\n    CHECK (voided_at_us >= 1)"
    + "\n)",
)


def latest_void_at_us_expression(*, instance_column: str, generation_column: str) -> str:
    """「这一实例代次最近一次作废的时刻」的唯一读法,报告与准入共用。

    参数是**列引用**,不是值:报告在一条 JOIN 里按 ``e.instance_id`` 读,准入按绑定参数读,两边
    必须是同一段判定。没有作废行时标量子查询返回 NULL,调用方按「没有作废」处理。
    """
    return (
        f"(SELECT MAX(v.voided_at_us) FROM {READINESS_VOID_TABLE} AS v "
        f"WHERE v.instance_id = {instance_column} "
        f"AND v.instance_generation = {generation_column})"
    )


__all__ = [
    "READINESS_VOID_COLUMNS",
    "READINESS_VOID_COLUMN_DEFINITIONS",
    "READINESS_VOID_DDL",
    "READINESS_VOID_FOREIGN_KEY",
    "READINESS_VOID_INSERT_COLUMNS",
    "READINESS_VOID_PRIMARY_KEY",
    "READINESS_VOID_REASON_GENERATION_MISMATCH",
    "READINESS_VOID_SOURCE_RENEWAL",
    "READINESS_VOID_TABLE",
    "latest_void_at_us_expression",
]
