"""Opt-in reproductions of the two known slow-transport conditions.

Nothing here is active by default: every condition is a context manager that a
caller must enter explicitly (see `tests/integration/flake_harness.py`), and no
conftest hook imports or enables it. The conditions change only the test
environment -- the runtime, the DSN and every product code path stay unchanged:

* slow commits -- PostgreSQL `commit_delay` (which needs `commit_siblings`
  *and* concurrently committing backends to take effect at all) and the InnoDB
  equivalent `binlog_group_commit_sync_delay`. The commit itself becomes slow;
  statement round trips stay fast.
* slow round trips -- `TcpLatencyProxy`, a loopback TCP proxy that sleeps a
  fixed delay per forwarded chunk in both directions, so connect, handshake,
  every statement and every commit reply cost more.

The two conditions are not interchangeable. `commit_delay` does not delay
statements at all, and the proxy delays bytes rather than commits, so a test
that only races its own commit latency is red under one and green under the
other.

Both conditions modify server-global state and background load, so each context
manager restores exactly what it changed and exposes the measurement that
proves it was actually in effect.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pymysql

# 10 ms per chunk is the value that reproduced the adjacent lease-window red:
# enough to blow a 1 s observation window while keeping a whole test file
# runnable.
DEFAULT_CHUNK_DELAY_MS = 10.0

# PostgreSQL rejects larger values ("200000 is outside the valid range for
# parameter commit_delay (0 .. 100000)").
POSTGRES_COMMIT_DELAY_US = 100_000
# One sibling is enough once the busy committers keep a queue of waiting
# committers; the default (5) needs many more concurrent backends.
POSTGRES_COMMIT_SIBLINGS = 1
POSTGRES_BUSY_COMMITTERS = 12
# InnoDB applies the group-commit delay to a lone committing transaction, so no
# busy load is required for this side.
MYSQL_COMMIT_DELAY_US = 200_000

# The measured median must reach half the configured delay, otherwise the
# condition is not in effect and a green run would be meaningless evidence.
_MINIMUM_EFFECT_RATIO = 0.5
_READ_SIZE = 65536
_TIMEOUT_S = 10.0


def _host_port(dsn: str) -> tuple[str, int]:
    parsed = urlsplit(dsn)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"DSN must name a host and a port so it can be proxied: {parsed.scheme}")
    return parsed.hostname, parsed.port


def _replace_host_port(dsn: str, host: str, port: int) -> str:
    parsed = urlsplit(dsn)
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(parsed.password, safe="")
        userinfo += "@"
    netloc = f"{userinfo}{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass
class _Forward:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


class TcpLatencyProxy:
    """Loopback TCP relay that sleeps `chunk_delay_ms` per forwarded chunk.

    The proxy runs its own event loop in a background thread, so synchronous
    test code (and pytest itself) can call `rewrite()` and carry on. Only
    loopback listeners are created; the upstream address comes from the DSN.

    Usage::

        with TcpLatencyProxy(chunk_delay_ms=10) as proxy:
            environment["SPEECHNEXUS_TEST_DATABASE_URL"] = proxy.rewrite(pg_dsn)
            environment["SPEECHNEXUS_TEST_MYSQL_URL"] = proxy.rewrite(mysql_dsn)
    """

    def __init__(self, chunk_delay_ms: float = DEFAULT_CHUNK_DELAY_MS) -> None:
        if chunk_delay_ms <= 0:
            raise ValueError("A latency proxy without a delay is not a condition")
        self.chunk_delay_ms = chunk_delay_ms
        self._delay_s = chunk_delay_ms / 1000.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: list[asyncio.AbstractServer] = []
        self._connections: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._ports: dict[tuple[str, int], int] = {}

    def __enter__(self) -> TcpLatencyProxy:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def listeners(self) -> dict[tuple[str, int], int]:
        """Upstream address to loopback listener port, for reporting."""
        return dict(self._ports)

    def start(self) -> None:
        if self._loop is not None:
            raise RuntimeError("Latency proxy is already started")
        loop = asyncio.new_event_loop()
        # The loop only produces connections, never exits by itself, so it is
        # safe to run it forever in a daemon thread and stop it explicitly.
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        thread = threading.Thread(target=run, name="sn-flake-latency-proxy", daemon=True)
        thread.start()
        if not ready.wait(timeout=_TIMEOUT_S):
            raise RuntimeError("Latency proxy event loop did not start")
        self._loop = loop
        self._thread = thread

    def stop(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        self._loop = None
        self._thread = None
        shutdown = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        try:
            shutdown.result(timeout=_TIMEOUT_S)
        except TimeoutError, RuntimeError:
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=_TIMEOUT_S)
        loop.close()
        self._servers.clear()
        self._connections.clear()

    def rewrite(self, dsn: str) -> str:
        """Return the same DSN pointed at a loopback listener for its upstream."""
        upstream = _host_port(dsn)
        port = self._ports.get(upstream)
        if port is None:
            port = self._listen(upstream)
            self._ports[upstream] = port
        return _replace_host_port(dsn, "127.0.0.1", port)

    def _listen(self, upstream: tuple[str, int]) -> int:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Latency proxy is not started")

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._serve(reader, writer, upstream)

        future = asyncio.run_coroutine_threadsafe(
            asyncio.start_server(serve, host="127.0.0.1", port=0), loop
        )
        server = future.result(timeout=_TIMEOUT_S)
        self._servers.append(server)
        return int(server.sockets[0].getsockname()[1])

    async def _serve(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream: tuple[str, int],
    ) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(*upstream)
        except OSError:
            client_writer.close()
            return
        pair = (client_writer, server_writer)
        self._connections.add(pair)
        client = _Forward(client_reader, client_writer)
        server = _Forward(server_reader, server_writer)
        try:
            await asyncio.gather(self._forward(client, server), self._forward(server, client))
        finally:
            self._connections.discard(pair)
            for writer in pair:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    writer.close()

    async def _forward(self, source: _Forward, destination: _Forward) -> None:
        try:
            while True:
                chunk = await source.reader.read(_READ_SIZE)
                if not chunk:
                    break
                await asyncio.sleep(self._delay_s)
                destination.writer.write(chunk)
                await destination.writer.drain()
        except ConnectionError, asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(ConnectionError, RuntimeError):
                if destination.writer.can_write_eof():
                    destination.writer.write_eof()

    async def _shutdown(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        for pair in list(self._connections):
            for writer in pair:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    writer.close()
        for server in self._servers:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=_TIMEOUT_S)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        asyncio.get_running_loop().stop()


def measure_commit_latency_ms(dsn: str, samples: int = 5) -> list[float]:
    """Commit latency, in milliseconds, for one committing transaction.

    Uses a scratch schema/database or a temporary table so the measurement does
    not depend on any test fixture and leaves nothing behind.
    """
    parsed = urlsplit(dsn)
    samples = max(2, samples)
    latencies: list[float] = []
    if parsed.scheme.startswith("postgres"):
        # A temporary table would be wrong here: its writes produce no WAL, so
        # the commit takes the no-flush fast path and commit_delay never
        # applies to it. Only a logged write reaches the delayed commit.
        schema = "sn_commit_probe_" + uuid.uuid4().hex[:12]
        connection = psycopg.connect(dsn, autocommit=False)
        try:
            connection.execute(f"CREATE SCHEMA {schema}")
            connection.execute(f"CREATE TABLE {schema}.probe (i int primary key)")
            connection.commit()
            for index in range(samples):
                connection.execute(
                    f"INSERT INTO {schema}.probe VALUES (%s) "
                    "ON CONFLICT (i) DO UPDATE SET i=EXCLUDED.i",
                    (index,),
                )
                started = time.perf_counter()
                connection.commit()
                latencies.append((time.perf_counter() - started) * 1000)
        finally:
            with contextlib.suppress(psycopg.Error):
                connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                connection.commit()
            connection.close()
        return latencies
    database = "sn_commit_probe_" + uuid.uuid4().hex[:12]
    admin = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        autocommit=True,
    )
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}`")
            cursor.execute(f"CREATE TABLE `{database}`.probe (i int primary key)")
        worker = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            database=database,
            autocommit=False,
        )
        try:
            with worker.cursor() as cursor:
                for index in range(samples):
                    cursor.execute("REPLACE INTO probe VALUES (%s)", (index,))
                    started = time.perf_counter()
                    worker.commit()
                    latencies.append((time.perf_counter() - started) * 1000)
        finally:
            worker.close()
    finally:
        with contextlib.suppress(Exception), admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        admin.close()
    return latencies


def _wait_for(probe: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + _TIMEOUT_S
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {description}")


class PostgresSlowCommits:
    """Make every PostgreSQL commit slow without touching product code.

    `commit_delay` alone is inert: it only sleeps when at least
    `commit_siblings` other backends are concurrently committing, and it is
    capped at 100 ms. So this context manager sets both parameters, keeps
    `POSTGRES_BUSY_COMMITTERS` synthetic committers committing in a loop, and
    measures a real commit to prove the delay is actually observed.

    Cleanup uses `ALTER SYSTEM RESET` plus `pg_reload_conf`, then reads the
    parameters back so a leftover delay cannot silently outlive the run.
    """

    def __init__(
        self,
        dsn: str,
        *,
        commit_delay_us: int = POSTGRES_COMMIT_DELAY_US,
        commit_siblings: int = POSTGRES_COMMIT_SIBLINGS,
        committers: int = POSTGRES_BUSY_COMMITTERS,
    ) -> None:
        if commit_delay_us > 100_000:
            raise ValueError("PostgreSQL commit_delay is capped at 100000 microseconds")
        self.dsn = dsn
        self.commit_delay_us = commit_delay_us
        self.commit_siblings = commit_siblings
        self.committers = committers
        self.baseline_latency_ms = 0.0
        self.commit_latency_ms = 0.0
        self._original_delay = 0
        self._original_siblings = 0
        self._admin: psycopg.Connection[tuple[object, ...]] | None = None
        self._schema = "sn_flake_commit_" + uuid.uuid4().hex[:12]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> PostgresSlowCommits:
        admin = psycopg.connect(self.dsn, autocommit=True)
        self._admin = admin
        try:
            self._original_delay = self._parameter(admin, "commit_delay")
            self._original_siblings = self._parameter(admin, "commit_siblings")
            self.baseline_latency_ms = statistics.median(measure_commit_latency_ms(self.dsn))
            admin.execute(f"ALTER SYSTEM SET commit_delay = '{self.commit_delay_us}'")
            admin.execute(f"ALTER SYSTEM SET commit_siblings = '{self.commit_siblings}'")
            admin.execute("SELECT pg_reload_conf()")
            self._wait_for_parameter(admin, "commit_delay", self.commit_delay_us)
            admin.execute(f"CREATE SCHEMA {self._schema}")
            admin.execute(f"CREATE TABLE {self._schema}.busy (payload int)")
            for _ in range(self.committers):
                thread = threading.Thread(target=self._keep_committing, daemon=True)
                thread.start()
                self._threads.append(thread)
            time.sleep(0.5)
            self.commit_latency_ms = statistics.median(measure_commit_latency_ms(self.dsn))
            self._require_effect()
        except BaseException:
            # Keep the original failure visible; the restore is best effort here
            # because it has no evidence value once the condition never started.
            self._cleanup(raise_on_failure=False)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._cleanup(raise_on_failure=True)

    def _cleanup(self, *, raise_on_failure: bool) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
        admin = self._admin
        self._admin = None
        if admin is None:
            return
        failure: str | None = None
        try:
            admin.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")
            admin.execute("ALTER SYSTEM RESET commit_delay")
            admin.execute("ALTER SYSTEM RESET commit_siblings")
            admin.execute("SELECT pg_reload_conf()")
            try:
                _wait_for(
                    lambda: (
                        self._parameter(admin, "commit_delay") == self._original_delay
                        and self._parameter(admin, "commit_siblings") == self._original_siblings
                    ),
                    "PostgreSQL commit_delay/commit_siblings to be reset",
                )
            except RuntimeError as error:
                failure = str(error)
        finally:
            admin.close()
        if failure is not None and raise_on_failure:
            raise RuntimeError(failure)

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "condition": "postgres-commit-delay",
            "commit_delay_us": self.commit_delay_us,
            "commit_siblings": self.commit_siblings,
            "busy_committers": self.committers,
            "baseline_commit_median_ms": round(self.baseline_latency_ms, 2),
            "slow_commit_median_ms": round(self.commit_latency_ms, 2),
        }

    @staticmethod
    def _parameter(admin: psycopg.Connection[tuple[object, ...]], name: str) -> int:
        rows = admin.execute(f"SHOW {name}").fetchall()
        return int(str(rows[0][0]))

    def _wait_for_parameter(
        self, admin: psycopg.Connection[tuple[object, ...]], name: str, value: int
    ) -> None:
        _wait_for(
            lambda: self._parameter(admin, name) == value,
            f"PostgreSQL {name}={value} (only a superuser DSN can reload postgresql.auto.conf)",
        )

    def _require_effect(self) -> None:
        minimum = self.commit_delay_us / 1000.0 * _MINIMUM_EFFECT_RATIO
        if self.commit_latency_ms < minimum:
            raise RuntimeError(
                "PostgreSQL commit_delay is not in effect: measured median "
                f"{self.commit_latency_ms:.2f} ms < {minimum:.2f} ms; commit_delay needs "
                "at least commit_siblings concurrently committing backends"
            )

    def _keep_committing(self) -> None:
        connection = psycopg.connect(self.dsn, autocommit=False)
        try:
            while not self._stop.is_set():
                try:
                    connection.execute(f"INSERT INTO {self._schema}.busy (payload) VALUES (1)")
                    connection.commit()
                except psycopg.Error:
                    connection.rollback()
        except psycopg.Error:
            return
        finally:
            connection.close()


class MySQLSlowCommits:
    """Make every InnoDB commit slow through binary-log group commit delay.

    `binlog_group_commit_sync_delay` delays the group commit before it syncs
    the binary log, which is the InnoDB analogue of PostgreSQL `commit_delay`.
    Unlike PostgreSQL it applies to a single committing transaction, so no busy
    load is needed; it does require `log_bin` to be enabled on the isolated
    server. The previous global value is restored and read back on exit.
    """

    def __init__(self, dsn: str, *, commit_delay_us: int = MYSQL_COMMIT_DELAY_US) -> None:
        parsed = urlsplit(dsn)
        if parsed.hostname is None or parsed.port is None:
            raise ValueError("MySQL DSN must name a host and a port")
        self.dsn = dsn
        self.commit_delay_us = commit_delay_us
        self.baseline_latency_ms = 0.0
        self.commit_latency_ms = 0.0
        self._original_delay = 0
        self._connection = pymysql.connect(
            host=parsed.hostname,
            port=parsed.port,
            user=parsed.username or "root",
            password=parsed.password or "",
            autocommit=True,
        )

    def __enter__(self) -> MySQLSlowCommits:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT @@GLOBAL.log_bin, @@GLOBAL.binlog_group_commit_sync_delay")
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("MySQL did not report its binary-log settings")
            log_bin, original = int(row[0]), int(row[1])
            if not log_bin:
                raise RuntimeError(
                    "MySQL slow commits need binary logging: set log_bin=ON on the "
                    "isolated test server (binlog_group_commit_sync_delay is a no-op "
                    "without it)"
                )
            self._original_delay = original
            self.baseline_latency_ms = statistics.median(measure_commit_latency_ms(self.dsn))
            with self._connection.cursor() as cursor:
                cursor.execute(
                    f"SET GLOBAL binlog_group_commit_sync_delay = {self.commit_delay_us}"
                )
                cursor.execute("SELECT @@GLOBAL.binlog_group_commit_sync_delay")
                readback = cursor.fetchone()
            self._expect_delay(readback, self.commit_delay_us)
            self.commit_latency_ms = statistics.median(measure_commit_latency_ms(self.dsn))
            self._require_effect()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    f"SET GLOBAL binlog_group_commit_sync_delay = {self._original_delay}"
                )
                cursor.execute("SELECT @@GLOBAL.binlog_group_commit_sync_delay")
                self._expect_delay(cursor.fetchone(), self._original_delay)
        finally:
            self._connection.close()

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "condition": "mysql-binlog-group-commit-delay",
            "binlog_group_commit_sync_delay_us": self.commit_delay_us,
            "baseline_commit_median_ms": round(self.baseline_latency_ms, 2),
            "slow_commit_median_ms": round(self.commit_latency_ms, 2),
        }

    @staticmethod
    def _expect_delay(row: tuple[object, ...] | None, expected: int) -> None:
        if row is None or int(str(row[0])) != expected:
            raise RuntimeError(
                "MySQL binlog_group_commit_sync_delay did not take the expected value "
                f"{expected}: reported {row}"
            )

    def _require_effect(self) -> None:
        minimum = self.commit_delay_us / 1000.0 * _MINIMUM_EFFECT_RATIO
        if self.commit_latency_ms < minimum:
            raise RuntimeError(
                "MySQL binlog_group_commit_sync_delay is not in effect: measured median "
                f"{self.commit_latency_ms:.2f} ms < {minimum:.2f} ms; check log_bin and "
                "that the probe writes go through the binary log"
            )
