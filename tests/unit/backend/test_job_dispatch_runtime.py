"""The production runtime only assembles an admission-filtered dispatcher."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr

from speechnexus_backend.job_dispatch_runtime import (
    DispatchDependencies,
    JobDispatchRuntime,
    JobDispatchSettings,
)
from speechnexus_backend.speech.application.job_dispatcher import ResultPublisher
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresDispatchQueue
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


def _settings() -> JobDispatchSettings:
    return JobDispatchSettings(
        process_role="job-worker",
        node_id="node-a",
        instance_id="worker-a",
        database_url=SecretStr("postgresql://speechnexus:x@127.0.0.1:55465/speechnexus"),
        database_schema="speechnexus_test",
        gateway_base_url="http://127.0.0.1:9999",
        gateway_credential=SecretStr("synthetic-dispatch-token-0001"),
        gateway_timeout_seconds=1,
        batch_size=4,
        lease_ms=30_000,
        poll_seconds=0.01,
        job_timeout_seconds=30,
        max_attempts=3,
        interval_seconds=0.1,
        storage_timeout_seconds=3,
        dispatch_kinds="asr,tts",
        object_storage_endpoint="http://127.0.0.1:9000",
        artifacts_bucket="artifacts-synthetic",
        execution_result_bucket="results-synthetic",
        object_storage_access_key=SecretStr("access"),
        object_storage_secret_key=SecretStr("secret"),
    )


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _Connection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> _Result:
        self.statements.append(statement)
        if "SESSION_USER" in statement:
            return _Result({"login": "speech_worker", "effective": "speech_worker"})
        if "AS forbidden" in statement:
            return _Result({"forbidden": False})
        return _Result(None)


class _Pool:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.statements: list[str] = []

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        self.closed += 1

    def connection(self, *, timeout: float) -> _Connection:
        return _Connection(self.statements)


class _Gateway:
    def __init__(self) -> None:
        self.closed = False
        self.queries: list[str] = []

    async def query(self, submit_intent_id: str) -> Any:
        self.queries.append(submit_intent_id)
        raise AssertionError("the runtime test never dispatches")

    async def aclose(self) -> None:
        self.closed = True


class _Publisher:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Planner:
    async def plan(self, job: Any, submit_intent_id: str) -> Any:
        raise AssertionError("the runtime test never plans")


def _dependencies(pool: _Pool, publisher: _Publisher) -> DispatchDependencies:
    return DispatchDependencies(
        _Planner(), cast(ResultPublisher, publisher), cast(SpeechPool, pool)
    )


async def test_open_assembles_an_admission_filtered_dispatcher() -> None:
    pool, publisher = _Pool(), _Publisher()
    runtime = JobDispatchRuntime(
        _settings(),
        _dependencies(pool, publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    await runtime.open()

    assert pool.opened == 1
    assert runtime.dispatcher is not None
    queue = cast(PostgresDispatchQueue, runtime.dispatcher._queue)
    assert queue.require_admission is True
    assert queue.kinds == ("asr", "tts")
    assert "SELECT operation_id, request_document FROM admission_operations LIMIT 0" in (
        pool.statements
    )


class _DeniedConnection(_Connection):
    def execute(self, statement: str, parameters: object = None) -> _Result:
        if "speech_voice_draft_revisions" in statement:
            raise RuntimeError("permission denied for table speech_voice_draft_revisions")
        return super().execute(statement, parameters)


class _DeniedPool(_Pool):
    def connection(self, *, timeout: float) -> _Connection:
        return _DeniedConnection(self.statements)


async def test_default_lane_does_not_probe_the_validation_fact_tables() -> None:
    pool, publisher = _Pool(), _Publisher()
    runtime = JobDispatchRuntime(
        _settings(),
        _dependencies(pool, publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    await runtime.open()

    assert runtime.dispatcher is not None
    assert not any("speech_voice_draft_revisions" in sql for sql in pool.statements)
    assert not any("speech_assets" in sql for sql in pool.statements)


async def test_voice_validation_lane_probes_the_validation_fact_tables() -> None:
    pool, publisher = _Pool(), _Publisher()
    runtime = JobDispatchRuntime(
        _settings().model_copy(update={"dispatch_kinds": "asr,tts,voice_validation"}),
        _dependencies(pool, publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    await runtime.open()

    assert runtime.dispatcher is not None
    assert (
        "SELECT draft_id, revision FROM speech_voice_draft_revisions LIMIT 0"
        in pool.statements
    )
    assert "SELECT workspace_id, asset_id FROM speech_assets LIMIT 0" in pool.statements


async def test_missing_validation_fact_grants_refuse_startup_before_dispatch() -> None:
    pool, publisher = _DeniedPool(), _Publisher()
    runtime = JobDispatchRuntime(
        _settings().model_copy(update={"dispatch_kinds": "voice_validation"}),
        _dependencies(pool, publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    with pytest.raises(RuntimeError, match="speech_voice_draft_revisions"):
        await runtime.open()

    assert runtime.dispatcher is None
    assert publisher.closed == 1


async def test_close_releases_the_injected_gateway_and_the_shared_dependencies() -> None:
    pool, publisher, gateway = _Pool(), _Publisher(), _Gateway()
    runtime = JobDispatchRuntime(
        _settings(), _dependencies(pool, publisher), gateway=cast(ExecutionGatewayAdapter, gateway)
    )
    await runtime.open()

    await runtime.close()

    assert gateway.closed is False
    assert publisher.closed == 1
    assert pool.closed == 1
    assert runtime.dispatcher is None


async def test_the_runtime_owns_an_assembled_gateway() -> None:
    pool, publisher = _Pool(), _Publisher()
    runtime = JobDispatchRuntime(_settings(), _dependencies(pool, publisher))
    await runtime.open()

    await runtime.close()

    assert pool.closed == 1
    assert publisher.closed == 1


async def test_close_is_idempotent() -> None:
    pool, publisher = _Pool(), _Publisher()
    runtime = JobDispatchRuntime(
        _settings(),
        _dependencies(pool, publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    await runtime.close()
    await runtime.close()

    assert pool.closed == 2


async def test_open_failure_closes_the_dependencies() -> None:
    publisher = _Publisher()

    class _Broken(_Pool):
        def connection(self, *, timeout: float) -> _Connection:
            raise RuntimeError("role check failed")

    runtime = JobDispatchRuntime(
        _settings(),
        _dependencies(_Broken(), publisher),
        gateway=cast(ExecutionGatewayAdapter, _Gateway()),
    )

    with pytest.raises(RuntimeError):
        await runtime.open()
    assert publisher.closed == 1
