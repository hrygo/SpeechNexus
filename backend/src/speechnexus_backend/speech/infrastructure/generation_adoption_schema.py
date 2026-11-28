"""账本代次采纳的审计表:``runtime_generation_adoptions``。

**这张表为什么存在。** 应用面协调账本 ``runtime_instance_heads.current_generation`` 与节点
控制器在自己意图库里维护的代次从来没有人对齐过。实测现场读到 1 与 11:真正在服务的 Pod 是第
11 代,账本却仍把第 1 代当当前代,于是就绪收据为没人服务的那一代签名、续租每 5 分钟以
``instance_generation_mismatch`` 失败(#1282)。收敛有两条路:把节点往前推(停第 11 代、prepare
第 12 代、登记它,这会重启引擎),或者在账本里做一次**带审计的采纳**——不动在跑的引擎,把账本
推进到节点实际准备好的那一代。本表是第二条路留下的唯一痕迹。

**它记的不是「发生了一次写」,而是「凭什么可以写」。** 采纳会用节点自己的生命周期证词
(``stopped_generations`` / ``voided_generations`` / ``last_fencing_token``)替换注册路径要求的那
份停止证据。法律事实是节点证词,不是操作者的判断,所以 ``document`` 保存当时的完整证词快照与
采纳理由,``previous_stop_evidence`` 单独成列以便按证据强弱检索:``node_reported_stopped``(节点
报了停止)与 ``node_voided_prepare``(节点说这一代的 prepare 从未跑过)是节点证词;
``missing`` 是操作者显式接受「没有停止证据」,是更弱的主张,读审计的人必须一眼能分辨。

**列清单只有一个定义点。** ``GENERATION_ADOPTION_COLUMN_DEFINITIONS`` 逐列声明类型,建表文本、
插入语句的列序与授权面都从它派生;给表加一列只改这一处。``tests/unit/backend/
test_generation_adoption_statements.py`` 另有一份**独立**写的期望值(列名与顺序),刻意不从本
模块派生:判据与实现同源时,两边一起漂而注入永远不红。

**只追加。** 采纳是历史事实,不是可编辑状态:本表没有 ``UPDATE``/``DELETE`` 授权面,重跑同一次
采纳由主键 ``(instance_id, adopted_generation)`` 拦下。回退要回退整笔采纳,不是改这一行。

DDL 的消费者只有 alembic 迁移树(``0071_generation_adoptions``),与 ``coordination_schema`` 等
模块一样在 ``scripts/dev-env/portable-sql-profiles.json`` 里声明;运行时角色不做 DDL。
"""

from __future__ import annotations

#: 审计表的物理名字,只有这一处字面量:迁移、写入语句、授权面都引用它。
GENERATION_ADOPTION_TABLE = "runtime_generation_adoptions"

#: 逐列物理类型,顺序即建表顺序,也是写入语句的列序。``instance_id`` 的定长与 collation 抄自
#: ``engine_instances``:外键两侧的字符类型与 collation 必须一致,否则 PostgreSQL 建表即失败。
GENERATION_ADOPTION_COLUMN_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("instance_id", 'VARCHAR(128) COLLATE "C" NOT NULL'),
    ("adopted_generation", "BIGINT NOT NULL"),
    ("ledger_generation", "BIGINT NOT NULL"),
    ("node_high_water", "BIGINT NOT NULL"),
    ("node_last_fencing_token", "BIGINT NOT NULL"),
    ("previous_stop_evidence", 'VARCHAR(32) COLLATE "C" NOT NULL'),
    ("operator_id", 'VARCHAR(128) COLLATE "C" NOT NULL'),
    ("adopted_at_us", "BIGINT NOT NULL"),
    ("document", "TEXT NOT NULL"),
)

#: 列名投影,只从上面那份声明派生。
GENERATION_ADOPTION_COLUMNS: tuple[str, ...] = tuple(
    name for name, _ in GENERATION_ADOPTION_COLUMN_DEFINITIONS
)

#: 写入语句写整行,所以插入列就是声明列。
GENERATION_ADOPTION_INSERT_COLUMNS: tuple[str, ...] = GENERATION_ADOPTION_COLUMNS

#: 一次采纳一行:同一实例同一代只能被采纳一次。主键同时是「重跑不会写第二行」的机制。
GENERATION_ADOPTION_PRIMARY_KEY: tuple[str, ...] = ("instance_id", "adopted_generation")

#: 审计行必须指向真实存在的被采纳代次。没有这条外键,删掉实例行之后仍会留下「属于某个不存在
#: 代次」的采纳记录,而按实例查询永远读不到它为什么还能被追认。
GENERATION_ADOPTION_FOREIGN_KEY: tuple[tuple[str, ...], str, tuple[str, ...]] = (
    ("instance_id", "adopted_generation"),
    "engine_instances",
    ("instance_id", "instance_generation"),
)

#: 建表文本。约束与 ``coordination_schema`` 的风格一致,全部是表级 CHECK(MySQL 8.0.16+ 也接受
#: 表级 CHECK;列级 CHECK 引用别的列在 InnoDB 上是 3813)。``IF NOT EXISTS`` 不是保险:若干集成
#: 用例把 ``alembic_version`` 回退再前滚,迁移必须可重放(同 ``0068_canonical_event_tables``)。
GENERATION_ADOPTION_DDL: tuple[str, ...] = (
    f"CREATE TABLE IF NOT EXISTS {GENERATION_ADOPTION_TABLE} (\n    "
    + ",\n    ".join(
        f"{name} {definition}" for name, definition in GENERATION_ADOPTION_COLUMN_DEFINITIONS
    )
    + f",\n    PRIMARY KEY ({', '.join(GENERATION_ADOPTION_PRIMARY_KEY)})"
    + f",\n    FOREIGN KEY ({', '.join(GENERATION_ADOPTION_FOREIGN_KEY[0])}) "
    + f"REFERENCES {GENERATION_ADOPTION_FOREIGN_KEY[1]} "
    + f"({', '.join(GENERATION_ADOPTION_FOREIGN_KEY[2])})"
    # 采纳只往前推:被采纳的代次必须高于账本当时的当前代次。写反了就是一次无审计的回退。
    + ",\n    CHECK (adopted_generation > ledger_generation)"
    + ",\n    CHECK (ledger_generation >= 1)"
    # 节点报的高水位不可能低于它的当前代次;不一致的证词不接受。
    + ",\n    CHECK (node_high_water >= adopted_generation)"
    # 节点报的 fencing token 是 prepare 请求里那个必须为正的授权序号。
    + ",\n    CHECK (node_last_fencing_token >= 1)"
    + ",\n    CHECK (adopted_at_us >= 1)"
    + "\n)",
)

__all__ = [
    "GENERATION_ADOPTION_COLUMNS",
    "GENERATION_ADOPTION_COLUMN_DEFINITIONS",
    "GENERATION_ADOPTION_DDL",
    "GENERATION_ADOPTION_FOREIGN_KEY",
    "GENERATION_ADOPTION_INSERT_COLUMNS",
    "GENERATION_ADOPTION_PRIMARY_KEY",
    "GENERATION_ADOPTION_TABLE",
]
