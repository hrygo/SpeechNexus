"""建 ``coordination_claim_buckets`` 并播种 ``BUCKETS`` 行(0066,派发认领前置 1)。

canonical shape 定在 ``migrations/0003_portable_claim_buckets.sql``:``bucket_no`` 是桶身份,
``claim_owner_id`` / ``lease_until`` 是租约,``version`` 是 CAS 版本。此前这张表只存在于
canonical schema 与按该 schema 建表的演练里,生产 alembic 链到 0065 既没有表也没有种子;
而派发认领(`claim_batch`)的第一步就是 ``read_fence``,所以缺表时零认领,表现为 42P01 或
``None``。本修订只补「表 + 种子」,不改任何既有列与读写路径。

三条口径,读者不必再猜:

* **种子行数取 Python 常量。** ``BUCKETS``(`speechnexus_portable_sql.claims`)是桶数唯一
  定义点;SQL 里出现字面量 256 就等于第二份实现,以后扩桶会只扩一边。
* **只补缺失的桶,不重写已存在的行。** 迁移可重入:若干集成测试直接回退
  ``alembic_version`` 再 ``upgrade head``,而真实部署里桶行可能带着在飞租约——重播种若
  先清空/覆盖,就会把生产租约擦掉并让版本回退。这里先读已有桶号,再逐行 INSERT 缺的那些。
  ``ON CONFLICT DO NOTHING`` 是 PostgreSQL 方言;本仓共同子集只允许「先 SELECT 唯一键,
  再单行 INSERT」,所以用后者。
* **提交前对账。** 播种后桶数必须正好 ``BUCKETS`` 且桶号覆盖 ``[0, BUCKETS)``;不满足就
  抛错随迁移事务回滚,而不是留一个「迁移成功但某些桶永远取不到」的库。

``downgrade`` 只 DROP 这张表。种子是表的内容,表不在则内容无从谈起;其余协调表由更早的
修订拥有,本修订不碰。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Connection, text

from speechnexus_backend.speech.infrastructure.claim_bucket_schema import (
    CLAIM_BUCKET_DDL,
    CLAIM_BUCKET_TABLE,
)
from speechnexus_portable_sql.claims import BUCKETS

revision = "0066_claim_bucket_table"
down_revision = "0065_canonical_jobs_reconcile"
branch_labels = None
depends_on = None

_EXISTING_BUCKETS_SQL = f"SELECT bucket_no FROM {CLAIM_BUCKET_TABLE}"
_INSERT_BUCKET_SQL = (
    f"INSERT INTO {CLAIM_BUCKET_TABLE} "
    "(bucket_no, claim_owner_id, lease_until, version) "
    "VALUES (:bucket_no, NULL, NULL, :version)"
)
_BUCKET_COUNT_SQL = f"SELECT COUNT(*) FROM {CLAIM_BUCKET_TABLE}"
_OUT_OF_RANGE_SQL = (
    f"SELECT COUNT(*) FROM {CLAIM_BUCKET_TABLE} WHERE bucket_no < 0 OR bucket_no >= :bucket_count"
)


def _seed_missing_buckets(connection: Connection) -> int:
    """按 ``BUCKETS`` 补齐缺失的桶;返回本次插入的行数,供调用方/测试核对。"""

    existing = {int(value) for value in connection.execute(text(_EXISTING_BUCKETS_SQL)).scalars()}
    missing = [
        {"bucket_no": bucket_no, "version": 0}
        for bucket_no in range(BUCKETS)
        if bucket_no not in existing
    ]
    if not missing:
        return 0
    connection.execute(text(_INSERT_BUCKET_SQL), missing)
    return len(missing)


def _require_seeded(connection: Connection) -> None:
    seeded = connection.execute(text(_BUCKET_COUNT_SQL)).scalar_one()
    if seeded != BUCKETS:
        raise RuntimeError(
            f"0066 seeding left {seeded} {CLAIM_BUCKET_TABLE} row(s), expected {BUCKETS}"
        )
    out_of_range = connection.execute(
        text(_OUT_OF_RANGE_SQL), {"bucket_count": BUCKETS}
    ).scalar_one()
    if out_of_range:
        raise RuntimeError(
            f"0066 seeding left {out_of_range} {CLAIM_BUCKET_TABLE} row(s) outside [0, {BUCKETS})"
        )


def upgrade() -> None:
    for statement in CLAIM_BUCKET_DDL:
        op.execute(statement)
    connection = op.get_bind()
    _seed_missing_buckets(connection)
    _require_seeded(connection)


def downgrade() -> None:
    # 表是种子的载体,种子不是独立对象;回到 0065 时这张表本该不存在,所以只 DROP 它。
    op.execute(f"DROP TABLE IF EXISTS {CLAIM_BUCKET_TABLE}")
