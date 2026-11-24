"""协调命名空间的键锁行:两个**不同角色**在同一行上排队,异键不排队。

这一条判据存在的理由就是它要防的失效:被替代的那把 catalog 互斥
(`pg_advisory_xact_lock(hashtextextended('speechnexus:catalog:…'))`)两侧分别是 backend 与
执行网关,它们是 `speech` schema 里的**两个不同角色**。锁行若落在只被一侧授权的命名空间里,
另一侧就锁不到同一行 —— 互斥**静默裂成两把**,没有报错、没有日志,只是两侧不再互相等待。
所以这里的用例不用「两条同超级用户连接」自证:两个连接分别以
``job_dispatch_grants``(backend)与 ``gateway_keyed_lock_grants``(网关)授权的**真实角色**登录,
授权与 DDL 都来自生产预览(迁移 ``0067`` + 各自唯一的那份声明)。

四条判据:

* **同键串行**:backend 先在事务里锁住键 `K`,网关对同一个 `K` 的第二笔事务必须等它 —— 由
  第三个观察连接读 `pg_blocking_pids` 点名「等的是哪个后端」,不靠 sleep 猜时序;释放后两笔都
  提交完成,没有死锁。
* **异键不串行**:backend 仍持 `K` 时,网关对 `K2` 的同类事务照常完成 —— 这是替代 advisory
  lock 必须保住的性质;把键改成常量,这条就红。
* **撤掉行锁许可必须响亮失败**:`REVOKE UPDATE (version)` 之后,同一段代码以
  ``InsufficientPrivilege``(42501)失败,而不是「没锁也算过」。授权不是装饰。
* 桶行懒建:第一次取锁 ``created=True``,同一行被第二个角色复用(``created=False``),两个角色
  看的确实是同一行。

观测口径:两个角色连接各自 `pg_backend_pid()`,`pg_blocking_pids(<pid>)` 返回**阻塞它的后端
pid 集合**。断言的是集合里正好有对方那一对 —— 不是「过了一会儿它就好了」。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN,
    COORDINATION_KEYED_LOCK_TABLE,
    COORDINATION_KEYED_LOCK_UPDATE_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_portable_sql.keyed_lock import KEYED_LOCK_COLUMNS, acquire, bucket_for_key
from speechnexus_portable_sql.psycopg_driver import connect as portable_connect

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"

#: 合成口令。CI 的 `SPEECHNEXUS_TEST_DATABASE_URL` 指向 TCP 的 `postgres:16.15-alpine`
#: 服务容器(host 认证 scram),无口令角色在**第一条连接**就被拒,报的是「数据库不可用」,
#: 与授权面无关;本机 trust 集群会把这一点藏起来。
_BACKEND_PASSWORD = "synthetic_coord_lock_backend"
_GATEWAY_PASSWORD = "synthetic_coord_lock_gateway"

#: 被串行化的键文本:形状与那 27 处 catalog advisory lock 一致,只是把 binding id 换成探针值。
KEY = "speechnexus:catalog:speech:binding:coord-lock-probe"
OTHER_KEY = "speechnexus:catalog:speech:binding:coord-lock-probe-other"

_LOCK_WAIT_LIMIT = 20.0


@dataclass(frozen=True)
class Sandbox:
    """一次运行独占的 schema 与两个非 owner 角色。"""

    dsn: str
    schema: str
    backend_role: str
    gateway_role: str
    backend_dsn: str
    gateway_dsn: str
    admin: Any

    @property
    def table(self) -> str:
        return f'"{self.schema}".{COORDINATION_KEYED_LOCK_TABLE}'


def _config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


def _role_names(prefix: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:10]
    return f"{prefix}_be_{suffix}", f"{prefix}_gw_{suffix}"


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    """真库、真迁移头、两个真角色:授权面不是用例自己编的。

    角色属性照 ``provision-job-roles.py`` 给的那一组(``LOGIN NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOINHERIT``),授权照两个生产定义点逐个语句执行:backend 身份拿
    ``job_runtime_grants`` + ``job_dispatch_grants``(派发/写入者身份的完整面),网关身份只拿
    ``gateway_keyed_lock_grants``。网关这条尤其重要 —— 如果它不成立,第二笔事务会以 42501
    失败而不是排队,用例在第一条断言上就红。
    """

    dsn = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_coord_lock_" + uuid.uuid4().hex[:12]
    backend_role, gateway_role = _role_names("sn_cl")
    engine = create_engine(
        dsn.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute("SET TimeZone TO 'UTC'")
    for role, password in (
        (backend_role, _BACKEND_PASSWORD),
        (gateway_role, _GATEWAY_PASSWORD),
    ):
        admin.execute(
            psycopg.sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
            ).format(psycopg.sql.Identifier(role), psycopg.sql.Literal(password))
        )
    try:
        with engine.connect() as connection:
            config = _config(schema)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        head = admin.execute("SELECT version_num FROM alembic_version").fetchone()
        assert head is not None and tuple(head) == ("0067_coordination_keyed_locks",), (
            f"the sandbox is not at the migration head: {head}"
        )
        created = admin.execute(
            "SELECT to_regclass(%s) AS name", (f"{schema}.{COORDINATION_KEYED_LOCK_TABLE}",)
        ).fetchone()
        assert created is not None and created[0] is not None, (
            "migration 0067 did not create the coordination keyed-lock table"
        )
        for statement in (
            *job_runtime_grants(schema, backend_role),
            *job_dispatch_grants(schema, backend_role),
            *gateway_keyed_lock_grants(schema, gateway_role),
        ):
            admin.execute(statement)
        yield Sandbox(
            dsn=dsn,
            schema=schema,
            backend_role=backend_role,
            gateway_role=gateway_role,
            backend_dsn=make_conninfo(dsn, user=backend_role, password=_BACKEND_PASSWORD),
            gateway_dsn=make_conninfo(dsn, user=gateway_role, password=_GATEWAY_PASSWORD),
            admin=admin,
        )
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in (backend_role, gateway_role):
            admin.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(psycopg.sql.Identifier(role)))
            admin.execute(
                psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(psycopg.sql.Identifier(role))
            )
        admin.close()
        engine.dispose()


def _connection(dsn: str, *, autocommit: bool = True) -> Any:
    """角色连接走同一个驱动适配层,所以这里的语句也用契约的 `?` 占位符。"""

    connection = portable_connect(dsn, autocommit=autocommit)
    connection.execute("SET TimeZone TO 'UTC'")
    return connection


def _pid(connection: Any) -> int:
    row = connection.execute("SELECT pg_backend_pid()").fetchone()
    assert row is not None
    return int(row[0])


def _blockers(observer: Any, pid: int) -> set[int]:
    """`pg_blocking_pids` 返回阻塞该后端的 pid 集合,空集表示它没有在等锁。"""

    rows = observer.execute("SELECT pg_blocking_pids(?)", (pid,)).fetchall()
    assert rows, "pg_blocking_pids returned no row"
    return {int(value) for value in rows[0][0]}


def _await_blockers(observer: Any, pid: int, *, limit: float = _LOCK_WAIT_LIMIT) -> set[int]:
    """等到该后端确实在等锁,返回阻塞它的 pid 集合;超时返回空集。

    轮询的是**可观测的锁状态**而不是猜一个时长:等不到就返回空集,由断言把它变成「第二笔
    事务没有排队」的失败,而不是被 sleep 掩盖的通过。
    """

    deadline = time.monotonic() + limit
    while True:
        blockers = _blockers(observer, pid)
        if blockers:
            return blockers
        if time.monotonic() >= deadline:
            return set()
        time.sleep(0.01)


def _warm(sandbox: Sandbox, key: str) -> None:
    """先让该键的桶行**已提交**地存在。

    桶行是懒建的:第一次用到它的事务在 INSERT 上就会被唯一索引挡住(未提交的索引项),那段等待
    与行锁无关。只有行已存在时,同键排序才完全由 `SELECT … FOR UPDATE` 给出 —— 那也是所有
    重放路径的真实形态(第二次请求时行一定在了)。
    """

    connection = _connection(sandbox.backend_dsn)
    try:
        with connection.transaction():
            created = acquire(
                connection,
                key=key,
                schema=sandbox.schema,
                table=COORDINATION_KEYED_LOCK_TABLE,
            )
        assert created.created is True, "the warm-up must be the transaction that creates the row"
    finally:
        connection.close()


def _acquire(sandbox: Sandbox, connection: Any, key: str) -> Any:
    return acquire(
        connection,
        key=key,
        schema=sandbox.schema,
        table=COORDINATION_KEYED_LOCK_TABLE,
    )


def _row_count(sandbox: Sandbox, key: str) -> int:
    row = sandbox.admin.execute(
        f"SELECT COUNT(*) FROM {sandbox.table} WHERE bucket_no = %s",
        (bucket_for_key(key),),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_the_table_and_both_roles_come_from_the_production_declarations(
    sandbox: Sandbox,
) -> None:
    """迁移建出的形状 == 声明;两个角色是两个不同的非 owner 登录。

    这条与下面的并发判据是同一条主张的两半:形状或授权只要有一半漂了,「两个角色取到同一行」
    就只是巧合。列顺序来自 ``KEYED_LOCK_COLUMNS`` 的清单,所以 DDL 与该清单不一致时这里红。
    """

    rows = sandbox.admin.execute(
        "SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (sandbox.schema, COORDINATION_KEYED_LOCK_TABLE),
    ).fetchall()
    assert [row[0] for row in rows] == [
        *KEYED_LOCK_COLUMNS,
        COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN,
    ], "the migrated table does not follow the portable declaration plus created_at"
    by_name = {row[0]: row for row in rows}
    for column in KEYED_LOCK_COLUMNS:
        assert by_name[column][1] == "numeric", by_name[column]
        assert (by_name[column][3], by_name[column][4]) == (38, 0), by_name[column]
        assert by_name[column][2] == "NO", f"{column} must not be nullable"
    assert by_name[COORDINATION_KEYED_LOCK_CREATED_AT_COLUMN][1] == "timestamp without time zone"
    primary_key = sandbox.admin.execute(
        "SELECT a.attname FROM pg_catalog.pg_index i "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary",
        (sandbox.schema, COORDINATION_KEYED_LOCK_TABLE),
    ).fetchall()
    assert [row[0] for row in primary_key] == [KEYED_LOCK_COLUMNS[0]]

    assert sandbox.backend_role != sandbox.gateway_role
    for dsn, role in (
        (sandbox.backend_dsn, sandbox.backend_role),
        (sandbox.gateway_dsn, sandbox.gateway_role),
    ):
        connection = _connection(dsn)
        try:
            identity = connection.execute(
                "SELECT current_user AS name, "
                "(SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) AS owner"
            ).fetchone()
            assert identity is not None
            assert identity[0] == role
            assert identity[1] is False, f"{role} is a superuser; the oracle would prove nothing"
            granted = connection.execute(
                "SELECT pg_catalog.has_column_privilege(current_user, ?, ?, 'UPDATE') AS held",
                (
                    f"{sandbox.schema}.{COORDINATION_KEYED_LOCK_TABLE}",
                    COORDINATION_KEYED_LOCK_UPDATE_COLUMNS[0],
                ),
            ).fetchone()
            assert granted is not None and granted[0] is True, (
                f"{role} lacks the row-lock allowance; the mutex halves would not meet"
            )
        finally:
            connection.close()


def test_the_gateway_waits_for_the_backend_on_the_same_key(sandbox: Sandbox) -> None:
    """同键:网关的第二笔事务在行锁上等 backend,backend 提交后两笔都完成。

    注入「删掉 `FOR UPDATE`」时这条红:行已提交存在,插入走唯一冲突分支,两笔都不等,
    `_await_blockers` 拿到空集,第一条断言失败。
    """

    _warm(sandbox, KEY)
    assert _row_count(sandbox, KEY) == 1
    backend = _connection(sandbox.backend_dsn)
    gateway = _connection(sandbox.gateway_dsn)
    observer = _connection(sandbox.dsn)
    backend_pid, gateway_pid = _pid(backend), _pid(gateway)
    held, release = threading.Event(), threading.Event()
    finished: list[str] = []
    errors: list[BaseException] = []

    def hold_the_key() -> None:
        try:
            with backend.transaction():
                lock = _acquire(sandbox, backend, KEY)
                assert lock.created is False, "the row was warmed, so this must not re-create it"
                assert lock.bucket_no == bucket_for_key(KEY)
                held.set()
                assert release.wait(30), "the release event never arrived"
            finished.append("backend")
        except BaseException as error:
            errors.append(error)
        finally:
            backend.close()

    def take_the_same_key() -> None:
        try:
            with gateway.transaction():
                _acquire(sandbox, gateway, KEY)
            finished.append("gateway")
        except BaseException as error:
            errors.append(error)
        finally:
            gateway.close()

    holder = threading.Thread(target=hold_the_key, name="coord-lock-backend")
    holder.start()
    try:
        assert held.wait(30), "the backend never took the keyed lock"
        waiter = threading.Thread(target=take_the_same_key, name="coord-lock-gateway")
        waiter.start()
        try:
            blockers = _await_blockers(observer, gateway_pid)
            assert backend_pid in blockers, (
                "the gateway transaction did not wait on the backend holding the same key: "
                f"blockers={sorted(blockers)} backend={backend_pid}"
            )
            assert waiter.is_alive(), "the gateway finished although the key was still held"
            assert "gateway" not in finished
        finally:
            release.set()
            waiter.join(30)
            assert not waiter.is_alive(), "the gateway transaction never completed after release"
    finally:
        holder.join(30)
        assert not holder.is_alive(), "the backend transaction never completed"
    assert errors == [], f"a role failed instead of queueing: {errors[0]!r}"
    assert finished == ["backend", "gateway"]
    assert _blockers(observer, gateway_pid) == set(), (
        "the gateway is still waiting after the commit"
    )
    observer.close()


def test_two_different_keys_do_not_serialize(sandbox: Sandbox) -> None:
    """异键:backend 持 `K` 时,网关对 `K2` 照常提交(替代 advisory lock 要保住的性质)。

    注入「把键改成常量」时这条红:第二笔也等同一行,`waiter.join(30)` 之后仍然活着,或者
    `errors` 里带 `ContractError`/超时。
    """

    _warm(sandbox, KEY)
    _warm(sandbox, OTHER_KEY)
    assert bucket_for_key(KEY) != bucket_for_key(OTHER_KEY), (
        "the two probe keys collided on one bucket; pick keys in different buckets so the "
        "case cannot pass for the wrong reason"
    )
    backend = _connection(sandbox.backend_dsn)
    gateway = _connection(sandbox.gateway_dsn)
    observer = _connection(sandbox.dsn)
    gateway_pid = _pid(gateway)
    held, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def hold_the_first_key() -> None:
        try:
            with backend.transaction():
                _acquire(sandbox, backend, KEY)
                held.set()
                assert release.wait(30), "the release event never arrived"
        except BaseException as error:
            errors.append(error)
        finally:
            backend.close()

    holder = threading.Thread(target=hold_the_first_key, name="coord-lock-backend")
    holder.start()
    try:
        assert held.wait(30), "the backend never took the first key"
        with gateway.transaction():
            _acquire(sandbox, gateway, OTHER_KEY)
        assert _blockers(observer, gateway_pid) == set(), (
            "the other key waited on the held key's row; the keys are not independent"
        )
    finally:
        release.set()
        holder.join(30)
        assert not holder.is_alive(), "the backend transaction never completed"
    assert errors == [], f"the backend failed while holding the key: {errors[0]!r}"
    observer.close()


def test_a_gateway_without_the_row_lock_privilege_fails_loudly(sandbox: Sandbox) -> None:
    """负例:撤掉列级 `UPDATE (version)` 后必须 42501,而不是静默「没锁也算过」。

    行已经提交存在,所以这里的失败只能是行锁许可缺失:`SELECT … FOR UPDATE` 在
    PostgreSQL 里要求行上某列有 `UPDATE` 权限。如果哪天这条断言变绿(或换成
    「有没有锁都行」),说明授权面不再被任何东西检查。
    """

    _warm(sandbox, KEY)
    sandbox.admin.execute(
        psycopg.sql.SQL("REVOKE UPDATE ({column}) ON {schema}.{table} FROM {role}").format(
            column=psycopg.sql.Identifier(COORDINATION_KEYED_LOCK_UPDATE_COLUMNS[0]),
            schema=psycopg.sql.Identifier(sandbox.schema),
            table=psycopg.sql.Identifier(COORDINATION_KEYED_LOCK_TABLE),
            role=psycopg.sql.Identifier(sandbox.gateway_role),
        )
    )
    connection = _connection(sandbox.gateway_dsn)
    try:
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege) as caught,
            connection.transaction(),
        ):
            _acquire(sandbox, connection, KEY)
        assert f"permission denied for table {COORDINATION_KEYED_LOCK_TABLE}" in str(
            caught.value
        ), str(caught.value)
    finally:
        connection.close()
