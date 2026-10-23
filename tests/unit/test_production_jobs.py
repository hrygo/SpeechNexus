"""Job boundary semantics and conservative real-driver error classification."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import PoolTimeout
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.jobs import CreateJobService
from speechnexus_backend.speech.domain.jobs import CreateJob, JobFact, JobResult, unknown_result
from speechnexus_backend.speech.infrastructure.job_schema import job_runtime_grants
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    JobPool,
    PostgresJobTransactions,
    job_pool,
)
from speechnexus_backend.speech.interfaces.job_transactions import JobTransactionBoundary

ROOT = Path(__file__).resolve().parents[2]
WIRE = next(
    c["request"]
    for c in json.loads(
        (ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text()
    )["valid_commands"]
    if c["operation"] == "create_job"
)
DIGEST = "sha256:" + "a" * 64
REQUEST = CreateJob("job-test", "operation-test", "attempt-test", "event-test", DIGEST, DIGEST)


class RecordingPort:
    def __init__(self) -> None:
        self.writes = 0
        self.reads = 0

    def create_job(self, request: CreateJob) -> JobResult:
        self.writes += 1
        return unknown_result(request)

    def recover_create_job(self, request: CreateJob) -> JobResult:
        self.reads += 1
        return JobResult(
            "replayed", request.job_id, JobFact(request.job_id, request.request_digest)
        )


def test_unknown_boundary_result_does_not_retry_or_start_recovery_implicitly() -> None:
    port = RecordingPort()
    boundary = JobTransactionBoundary(CreateJobService(port))
    wire = copy.deepcopy(WIRE)
    assert boundary.create_job(wire)["recovery"] == "query_original"
    assert (port.writes, port.reads) == (1, 0)
    assert boundary.recover_create_job(wire)["value"]["fact_id"] == REQUEST.job_id
    assert (port.writes, port.reads) == (1, 1)
    assert wire == WIRE


@pytest.mark.parametrize("mutation", ["extra", "bool", "uppercase", "newline"])
def test_invalid_wire_is_rejected_before_database_access(mutation: str) -> None:
    port = RecordingPort()
    boundary = JobTransactionBoundary(CreateJobService(port))
    wire = copy.deepcopy(WIRE)
    if mutation == "extra":
        wire["private_payload"] = "synthetic-secret-marker"
    elif mutation == "bool":
        wire["job"]["job_id"] = True
    elif mutation == "uppercase":
        wire["request_digest"] = "SHA256:" + "a" * 64
    else:
        wire["job"]["job_id"] += "\n"
    with pytest.raises(ContractValidationError) as caught:
        boundary.create_job(wire)
    assert "synthetic-secret-marker" not in str(caught.value)
    assert (port.writes, port.reads) == (0, 0)


def test_internal_bundle_rejects_unknown_entry_and_invalid_result() -> None:
    with pytest.raises(ValueError, match="Unknown coordination contract"):
        validate_coordination("not_an_entry", {})
    with pytest.raises(ContractValidationError):
        validate_coordination(
            "TransactionResult",
            {
                "schema_version": "1.0.0",
                "operation": "create_job",
                "outcome": "committed",
                "scope_id": "job-test",
                "recovery": "none",
            },
        )


def test_domain_prevents_success_without_its_original_fact_or_unknown_retry() -> None:
    with pytest.raises(ValueError, match="original Job fact"):
        JobResult("committed", REQUEST.job_id)
    with pytest.raises(ValueError, match="original Job fact"):
        JobResult("replayed", REQUEST.job_id, JobFact("job-other", DIGEST))
    with pytest.raises(ValueError, match="cannot expose"):
        JobResult("conflict", REQUEST.job_id, JobFact(REQUEST.job_id, DIGEST))
    with pytest.raises(ValueError, match="only query"):
        JobResult(
            "unknown", REQUEST.job_id, error_code="outcome_unknown", recovery="retry_transaction"
        )


@dataclass
class FaultScenario:
    failure: Exception | None
    existing: bool = False


class DriverCursor:
    def __init__(self, pool: DriverPool, existing: bool) -> None:
        self.pool = pool
        self.existing = existing
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> DriverCursor:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        if "INSERT INTO" in query:
            self.pool.inserts += 1
        if "SELECT job_id, operation_id" in query:
            self.rows = [asdict(REQUEST)] if self.existing else []
        elif "SELECT job_id, event_digest" in query:
            self.rows = [{"job_id": REQUEST.job_id, "event_digest": DIGEST}]

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class DriverConnection:
    def __init__(self, pool: DriverPool, scenario: FaultScenario) -> None:
        self.pool, self.scenario = pool, scenario

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        if query == "SET TRANSACTION READ ONLY":
            self.pool.read_only += 1

    def cursor(self) -> DriverCursor:
        return DriverCursor(self.pool, self.scenario.existing)


class DriverPool:
    """Only transport faults are simulated; real atomicity is covered on both DBs."""

    def __init__(self, scenarios: list[FaultScenario]) -> None:
        self.scenarios = scenarios
        self.calls = self.inserts = self.read_only = 0

    @contextmanager
    def connection(self, timeout: float) -> Iterator[DriverConnection]:
        assert 0 < timeout <= 3
        scenario = self.scenarios[self.calls]
        self.calls += 1
        if isinstance(scenario.failure, PoolTimeout):
            raise scenario.failure
        yield DriverConnection(self, scenario)
        if scenario.failure is not None:
            raise scenario.failure


@pytest.mark.parametrize("failure", [psycopg.OperationalError, psycopg.InterfaceError])
def test_ambiguous_driver_failure_never_retries_a_write(failure: type[Exception]) -> None:
    pool = DriverPool([FaultScenario(failure("synthetic transport loss"))])
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert pool.calls == 1 and pool.inserts == 2


@pytest.mark.parametrize(
    "error", [psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure]
)
def test_only_confirmed_abort_gets_a_bounded_whole_transaction_retry(
    error: type[Exception],
) -> None:
    pool = DriverPool([FaultScenario(error()) for _ in range(3)])
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"
    assert pool.calls == 3 and pool.inserts == 6


def test_confirmed_abort_then_success_is_committed_once() -> None:
    pool = DriverPool([FaultScenario(psycopg.errors.DeadlockDetected()), FaultScenario(None)])
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "committed" and pool.calls == 2


def test_unique_conflict_resolves_winner_using_a_read_only_transaction() -> None:
    pool = DriverPool([FaultScenario(psycopg.errors.UniqueViolation()), FaultScenario(None, True)])
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "replayed"
    assert pool.calls == 2 and pool.inserts == 2 and pool.read_only == 1


def test_failed_winner_read_is_unknown_instead_of_an_invented_conflict() -> None:
    pool = DriverPool(
        [FaultScenario(psycopg.errors.UniqueViolation()), FaultScenario(psycopg.OperationalError())]
    )
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "unknown" and pool.inserts == 2


def test_queue_timeout_is_known_not_attempted() -> None:
    pool = DriverPool([FaultScenario(PoolTimeout())])
    result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"
    assert pool.inserts == 0


def test_queue_wait_cannot_reuse_the_time_budget_from_before_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = DriverPool([FaultScenario(None)])
    clock = iter((0.0, 0.0, 4.0))
    with monkeypatch.context() as patch:
        patch.setattr(
            "speechnexus_backend.speech.infrastructure.jobs_postgres.time.monotonic",
            lambda: next(clock, 4.0),
        )
        result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    assert result.outcome == "rejected" and result.recovery == "retry_transaction"
    assert pool.calls == 1 and pool.inserts == 0


def test_pool_retry_and_grant_configuration_is_bounded_and_not_sql_injectable() -> None:
    for maximum in (0, 65):
        with pytest.raises(ValueError):
            job_pool("unused", max_size=maximum)
    with pytest.raises(ValueError):
        job_pool("unused", max_waiting=0)
    with pytest.raises(ValueError):
        job_pool("unused", schema="speech;drop_schema")
    with pytest.raises(ValueError):
        job_runtime_grants("speech", 'role";drop_role')
    with pytest.raises(ValueError):
        PostgresJobTransactions(cast(JobPool, None), max_attempts=4)
    with pytest.raises(ValueError):
        PostgresJobTransactions(cast(JobPool, None), timeout_seconds=0)
