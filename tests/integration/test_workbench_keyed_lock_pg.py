"""workbench 回执键锁:同键排队、异键并行、碰撞只加竞争(真 PG、真 runtime 角色).

三条性质都是**产品路径**上的判据:用例走 `PostgresVersionUnitOfWork` 的
`claim_receipt` / `WorkbenchVersions.create_project`,不直接压库里那两条语句。锁等待由第三个
连接读 `pg_stat_activity`(`wait_event_type='Lock'`)观测,不用 sleep 猜时序。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from speechnexus_backend.workbench.application.version_ports import MutationScope
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    receipt_lock_key,
)
from speechnexus_backend.workbench.infrastructure.runtime_schema import (
    bootstrap_sql,
    runtime_grants_sql,
)
from speechnexus_backend.workbench.runtime import RuntimeStartupError, check_database
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings
from speechnexus_portable_sql.keyed_lock import (
    KEYED_LOCK_ROW_LOCK_COLUMN,
    KEYED_LOCK_TABLE,
    bucket_for_key,
)
from speechnexus_portable_sql.psycopg_driver import connect_async as portable_connect_async

pytestmark = pytest.mark.integration

WORKSPACE = "workspace-keyed-lock"
SCOPE: MutationScope = ("project.create", WORKSPACE)

#: `PostgresVersionUnitOfWork` 在事务里给会话起的名字:观测锁等待时按它筛后端,
#: 不按「碰巧只有两条连接」猜。
_APPLICATION_NAME = "speechnexus-workbench-versions"
_LOCK_WAIT_SQL = (
    "SELECT COUNT(*) FROM pg_stat_activity WHERE application_name = ? AND wait_event_type = 'Lock'"
)


#: 运行时登录的合成口令。`SPEECHNEXUS_TEST_DATABASE_URL` 在 CI 指向 TCP 的
#: `postgres:16.15-alpine` 服务容器(host 认证 scram),而无口令角色只在本机 trust 集群上
#: 能连:不设口令时网关在第一条连接就被拒绝,报的是「数据库不可用」,与授权面无关。
_RUNTIME_PASSWORD = "synthetic_keyed_lock_runtime"


@dataclass
class Store:
    owner_dsn: str
    runtime_dsn: str
    schema: str
    role: str

    def settings(self) -> WorkbenchSettings:
        return WorkbenchSettings(
            database_url=SecretStr(self.runtime_dsn),
            database_schema=self.schema,
            workspace_id=WORKSPACE,
            deployment_id="deployment-keyed-lock",
            cursor_key=SecretStr("keyed-lock-cursor-key-0000000000"),
        )

    @property
    def unit(self) -> WorkbenchVersions:
        return WorkbenchVersions(self.uow())

    def uow(self) -> PostgresVersionUnitOfWork:
        """等待预算高于默认值:默认 `lock_timeout` 会让「排队」变成「超时失败」."""

        return PostgresVersionUnitOfWork(
            self.runtime_dsn,
            schema=self.schema,
            transaction_timeout=30,
            lock_timeout_ms=15_000,
            statement_timeout_ms=20_000,
        )


@asynccontextmanager
async def _observer(dsn: str) -> AsyncIterator[Any]:
    """只读观测连接:走同一个驱动适配层,所以这里的语句也用契约的 `?`."""

    connection = await portable_connect_async(dsn, autocommit=True)
    try:
        yield connection
    finally:
        connection.pgconn.finish()


async def _lock_waits(observer: Any) -> int:
    cursor = await observer.execute(_LOCK_WAIT_SQL, (_APPLICATION_NAME,))
    row = await cursor.fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, int)
    return value


async def _await_lock_wait(observer: Any, *, limit: float = 15.0) -> int:
    """等到至少有一个本模块的后端在等锁,返回观测到的等待数(0 表示等不到)."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while True:
        waits = await _lock_waits(observer)
        if waits:
            return waits
        if loop.time() >= deadline:
            return 0
        await asyncio.sleep(0.05)


async def _commit_one_lock(store: Store, key: str) -> None:
    """先让这个键的桶行**已提交地存在**,再压并发.

    为什么必须先热身:桶行是懒建的,第一次使用它的事务在插入时会被唯一索引挡住(未提交的
    索引项),那一段等待与行锁无关。只有「行已存在」时,同键排序才完全由 `SELECT … FOR UPDATE`
    给出 —— 这也是重放路径的真实形态(第二次请求时行一定在了)。
    """

    async with store.uow().transaction() as session:
        assert await session.claim_receipt(SCOPE, key) is None


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    dsn = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not dsn:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")

    schema = "sn_keyed_lock_" + uuid4().hex[:12]
    role = "sn_kl_rt_" + uuid4().hex[:12]
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as owner:
        await owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(_RUNTIME_PASSWORD)
            )
        )
        try:
            await owner.execute(bootstrap_sql(schema))
            await owner.execute(runtime_grants_sql(schema, role))
            yield Store(
                dsn,
                make_conninfo(dsn, user=role, password=_RUNTIME_PASSWORD),
                schema,
                role,
            )
        finally:
            await owner.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


async def test_the_same_key_waits_for_the_first_receipt_and_then_completes(
    store: Store,
) -> None:
    """同键:第二个受理在行锁上排队,第一个提交后它才继续,两笔都成功。"""

    unit = store.unit
    await _commit_one_lock(store, "same-key")
    claimed = asyncio.Event()
    released = asyncio.Event()

    async def hold_the_key() -> None:
        async with store.uow().transaction() as session:
            assert await session.claim_receipt(SCOPE, "same-key") is None
            claimed.set()
            await released.wait()

    async def create_with_the_same_key() -> str:
        project = await unit.create_project(
            workspace_id=WORKSPACE, name="同键串行", idempotency_key="same-key"
        )
        return project.id

    async with _observer(store.owner_dsn) as observer:
        first = asyncio.create_task(hold_the_key())
        try:
            await asyncio.wait_for(claimed.wait(), 20)
            second = asyncio.create_task(create_with_the_same_key())
            waits = await _await_lock_wait(observer)
            assert waits >= 1, "the second receipt did not wait on the keyed lock row"
            assert not second.done()
            released.set()
            project_id = await asyncio.wait_for(second, 30)
            await asyncio.wait_for(first, 30)
            assert await _lock_waits(observer) == 0
        finally:
            released.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    assert await unit.create_project(
        workspace_id=WORKSPACE, name="同键串行", idempotency_key="same-key"
    ) == await unit.get_project(project_id)


async def test_two_different_keys_both_proceed_while_one_holds_its_key(store: Store) -> None:
    """异键:第一个键被持有时,另一个键照常开始并提交(这是替代 advisory lock 要保住的性质)。"""

    unit = store.unit
    await _commit_one_lock(store, "first-key")
    claimed = asyncio.Event()
    released = asyncio.Event()

    async def hold_the_first_key() -> None:
        async with store.uow().transaction() as session:
            assert await session.claim_receipt(SCOPE, "first-key") is None
            claimed.set()
            await released.wait()

    async with _observer(store.owner_dsn) as observer:
        first = asyncio.create_task(hold_the_first_key())
        try:
            await asyncio.wait_for(claimed.wait(), 20)
            second = asyncio.create_task(
                unit.create_project(
                    workspace_id=WORKSPACE, name="异键并行", idempotency_key="second-key"
                )
            )
            project = await asyncio.wait_for(second, 15)
            assert not first.done(), "the other key's transaction finished before ours"
            assert await _lock_waits(observer) == 0
        finally:
            released.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    replayed = await unit.create_project(
        workspace_id=WORKSPACE, name="异键并行", idempotency_key="second-key"
    )
    assert replayed == project


def _keys_sharing_one_bucket(schema: str) -> tuple[str, str]:
    """找出两个**完整键不同、桶号相同**的幂等键:碰撞是设计内的情形,用例要能构造它."""

    seen: dict[int, str] = {}
    for index in range(20_000):
        key = f"collision-{index}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(bucket_for_key(receipt_lock_key(schema, SCOPE, digest)))
        if bucket in seen:
            return seen[bucket], key
        seen[bucket] = key
    raise AssertionError("no two keys shared a bucket; the probe would be vacuous")


async def test_two_keys_on_one_bucket_row_contend_but_keep_separate_identities(
    store: Store,
) -> None:
    """碰撞:两笔事务等同一行(竞争),但各自的重放身份互不覆盖(没有别名)。"""

    first_key, second_key = _keys_sharing_one_bucket(store.schema)
    unit = store.unit
    await _commit_one_lock(store, first_key)
    claimed = asyncio.Event()
    released = asyncio.Event()

    async def hold_the_first_key() -> None:
        async with store.uow().transaction() as session:
            assert await session.claim_receipt(SCOPE, first_key) is None
            claimed.set()
            await released.wait()

    async with _observer(store.owner_dsn) as observer:
        first = asyncio.create_task(hold_the_first_key())
        try:
            await asyncio.wait_for(claimed.wait(), 20)
            second = asyncio.create_task(
                unit.create_project(
                    workspace_id=WORKSPACE, name="碰撞竞争", idempotency_key=second_key
                )
            )
            waits = await _await_lock_wait(observer)
            assert waits >= 1, "the colliding key did not wait on the shared bucket row"
            released.set()
            second_project = await asyncio.wait_for(second, 30)
            await asyncio.wait_for(first, 30)
        finally:
            released.set()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    first_project = await unit.create_project(
        workspace_id=WORKSPACE, name="碰撞竞争", idempotency_key=first_key
    )
    assert first_project.id != second_project.id
    # 两把不同的键各自重放自己的结果:碰撞只发生在锁行上,没有渗进回执身份。
    assert (
        await unit.create_project(
            workspace_id=WORKSPACE, name="碰撞竞争", idempotency_key=first_key
        )
        == first_project
    )
    assert (
        await unit.create_project(
            workspace_id=WORKSPACE, name="碰撞竞争", idempotency_key=second_key
        )
        == second_project
    )


async def test_the_runtime_role_passes_the_startup_gate_with_the_row_lock_table(
    store: Store,
) -> None:
    """启动闸门逐表、逐列核对:新表的授权面必须正好是「读+插入,列级更新 version」。"""

    await check_database(store.settings())


async def test_the_startup_gate_refuses_a_missing_row_lock_column_grant(store: Store) -> None:
    """注入:撤掉列级 `UPDATE (version)`,闸门必须拒绝启动(否则那一列只是装饰).

    断言点名到具体那一列:只要闸门放行(或换成一个不核对列级授权的检查),这里就红。
    同时核对消息只带目录常量,不回显被配置的 schema。
    """

    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(
            sql.SQL("REVOKE UPDATE ({column}) ON {schema}.{table} FROM {role}").format(
                column=sql.Identifier(KEYED_LOCK_ROW_LOCK_COLUMN),
                schema=sql.Identifier(store.schema),
                table=sql.Identifier(KEYED_LOCK_TABLE),
                role=sql.Identifier(store.role),
            )
        )

    with pytest.raises(RuntimeStartupError) as caught:
        await check_database(store.settings())
    message = str(caught.value)
    assert "column UPDATE surface refused: missing=['keyed_locks.version']" in message, message
    assert store.schema not in message
