"""The planner replays exactly the admission rows; no default is ever invented."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    StageAllocation,
)
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchJobKind,
    DispatchProtocolError,
    DispatchRequestUnavailableError,
    DispatchUnavailableError,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.infrastructure.admission_codec import request_to_wire
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.stored_command_planner import (
    StoredCommandPlanner,
    submission_from_admission,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import deadline_text

ROOT = Path(__file__).resolve().parents[3]
WORKSPACE = "workspace-test"
POOL = "tts-realtime"
HOST = "host-test"
GATEWAY = "gateway-test"
INSTANCE = "instance-test"
BINDING = "binding-test"
POLICY = "policy-test"
DEADLINE = 1_900_000_000_000_000
JOB_ID = "job-" + "1" * 32
ATTEMPT = "attempt-" + "2" * 32
STAGE_ID = "stage-" + "3" * 32
LEASE = "lease-" + "4" * 32
SUBMIT = "submit-" + "5" * 32
EVENT = "event-" + "6" * 32


def _command() -> dict[str, Any]:
    schema = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())
    return next(dict(example) for example in schema["examples"] if example["kind"] == "tts")


def _digest(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode()).hexdigest()


def _gateway() -> dict[str, Any]:
    return {
        "submit_intent_id": SUBMIT,
        "lease_id": LEASE,
        "operation_id": JOB_ID,
        "attempt_id": ATTEMPT,
        "stage_id": STAGE_ID,
        "workspace_id": WORKSPACE,
        "pool_id": POOL,
        "host_node_id": HOST,
        "gateway_id": GATEWAY,
        "instance_id": INSTANCE,
        "instance_generation": 1,
        "binding_revision_id": BINDING,
        "policy_revision_id": POLICY,
        "voice_revision_id": None,
        "fencing_token": 7,
        "command_digest": "sha256:" + "a" * 64,
        "command": _command(),
        "deadline_at": deadline_text(DEADLINE),
        "max_output_audio_ms": None,
        "sample_rate_hz": None,
    }


def _business() -> str:
    return rfc8785.dumps({"kind": "tts", "gateway_request": _gateway()}).decode()


def _envelope(business_digest: str) -> str:
    request = AdmissionRequest(
        AdmissionIdentity(JOB_ID, ATTEMPT),
        WORKSPACE,
        BINDING,
        "manifest-test",
        "sha256:" + "f" * 64,
        POLICY,
        1,
        "realtime",
        (OwnerAuthority(POOL, "scheduler-test", 7),),
        (StageAllocation(STAGE_ID, LEASE, POOL, HOST, GATEWAY, INSTANCE, 1, 2, 1),),
        DEADLINE,
        CreateJob(JOB_ID, JOB_ID, ATTEMPT, EVENT, "sha256:" + "d" * 64, business_digest),
    )
    return rfc8785.dumps(request_to_wire(request)).decode()


def _job(*, kind: DispatchJobKind = "tts", business: str | None = None) -> DispatchJob:
    document = business if business is not None else _business()
    return DispatchJob(JOB_ID, WORKSPACE, kind, "pending", 1, 1, _digest(document), document)


def _rows(**changes: Any) -> dict[str, list[dict[str, Any]]]:
    business = changes.pop("business", None) or _business()
    envelope = _envelope(_digest(business))
    operation: dict[str, Any] = {
        "operation_id": JOB_ID,
        "workspace_id": WORKSPACE,
        "request_digest": _digest(business),
        "request_document": business,
        "current_attempt_id": ATTEMPT,
    }
    attempt: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "workspace_id": WORKSPACE,
        "binding_revision_id": BINDING,
        "policy_revision_id": POLICY,
        "request_digest": _digest(envelope),
    }
    request: dict[str, Any] = {
        "request_document": envelope,
        "request_digest": _digest(envelope),
    }
    lease: dict[str, Any] = {
        "lease_id": LEASE,
        "stage_id": STAGE_ID,
        "pool_id": POOL,
        "host_node_id": HOST,
        "gateway_id": GATEWAY,
        "instance_id": INSTANCE,
        "instance_generation": 1,
        "fencing_token": 7,
        "deadline_at_us": DEADLINE,
        "lease_state": "reserved",
        "cleanup_state": "pending",
    }
    tables = {"operations": operation, "attempts": attempt, "requests": request, "leases": lease}
    for name, values in changes.items():
        tables[name].update(values)
    return {
        "FROM admission_operations": [operation],
        "FROM admission_attempts": [attempt],
        "FROM admission_requests": [request],
        "FROM admission_leases": [lease],
    }


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Connection:
    def __init__(self, rows: Mapping[str, Any], *, fail: bool = False) -> None:
        self._rows, self._fail = rows, fail
        self.statements: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> _Result:
        if self._fail:
            raise RuntimeError("connection lost")
        self.statements.append(statement)
        for table, rows in self._rows.items():
            if table in statement:
                return _Result(rows)
        return _Result([])


class _Pool:
    def __init__(self, rows: Mapping[str, Any], *, fail: bool = False) -> None:
        self._rows, self._fail = rows, fail
        self.timeouts: list[float] = []
        self.connections: list[_Connection] = []

    def connection(self, *, timeout: float) -> _Connection:
        self.timeouts.append(timeout)
        connection = _Connection(self._rows, fail=self._fail)
        self.connections.append(connection)
        return connection


def test_plan_replays_the_frozen_command_and_identity() -> None:
    planner = _planner(_rows())

    submission = _submit(planner)

    assert dict(submission.body) == _gateway()
    assert submission.submit_intent_id == SUBMIT


def _planner(
    rows: Mapping[str, Any], *, fail: bool = False, timeout: float = 3
) -> StoredCommandPlanner:
    pool = _Pool(rows, fail=fail)
    return StoredCommandPlanner(cast(SpeechPool, pool), timeout_seconds=timeout)


def _submit(planner: StoredCommandPlanner, job: DispatchJob | None = None) -> Any:
    import asyncio

    return asyncio.run(planner.plan(job or _job(), SUBMIT))


def test_a_missing_operation_is_unavailable() -> None:
    rows = _rows()
    rows["FROM admission_operations"] = []

    with pytest.raises(DispatchRequestUnavailableError):
        _submit(_planner(rows))


def test_a_redacted_command_is_unavailable() -> None:
    rows = _rows()
    rows["FROM admission_operations"][0]["request_document"] = None

    with pytest.raises(DispatchRequestUnavailableError):
        _submit(_planner(rows))


def test_a_command_whose_digest_does_not_match_is_refused() -> None:
    rows = _rows()
    rows["FROM admission_operations"][0]["request_document"] = "{}"

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(rows))


def test_a_command_outside_the_frozen_contract_is_refused() -> None:
    gateway = _gateway()
    gateway.pop("max_output_audio_ms")
    business = rfc8785.dumps({"kind": "tts", "gateway_request": gateway}).decode()
    rows = _rows(business=business)

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(rows))


def test_a_quarantined_lease_is_never_dispatched() -> None:
    rows = _rows(leases={"lease_state": "quarantined"})

    with pytest.raises(DispatchUnavailableError):
        _submit(_planner(rows))


def test_a_released_lease_is_refused_not_replayed() -> None:
    rows = _rows(leases={"lease_state": "released"})

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(rows))


def test_a_lease_that_disagrees_with_the_envelope_is_refused() -> None:
    rows = _rows(leases={"stage_id": "stage-" + "9" * 32})

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(rows))


def test_a_fenced_lease_that_disagrees_with_the_authority_is_refused() -> None:
    rows = _rows(leases={"fencing_token": 8})

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(rows))


def test_a_command_of_another_kind_is_refused() -> None:
    job = _job(kind="asr")

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(_rows()), job)


def test_the_workspace_of_the_admission_must_match_the_job() -> None:
    job = DispatchJob(
        JOB_ID, "workspace-other", "tts", "pending", 1, 1, _digest(_business()), _business()
    )

    with pytest.raises(DispatchProtocolError):
        _submit(_planner(_rows()), job)


def test_an_unreadable_admission_is_unavailable() -> None:
    with pytest.raises(DispatchUnavailableError):
        _submit(_planner(_rows(), fail=True))


def test_a_missing_attempt_or_request_is_unavailable() -> None:
    rows = _rows()
    rows["FROM admission_attempts"] = []

    with pytest.raises(DispatchRequestUnavailableError):
        _submit(_planner(rows))


def test_an_admission_with_two_leases_is_unavailable() -> None:
    rows = _rows()
    rows["FROM admission_leases"] = rows["FROM admission_leases"] * 2

    with pytest.raises(DispatchRequestUnavailableError):
        _submit(_planner(rows))


def test_the_planner_reads_inside_a_bounded_read_only_transaction() -> None:
    pool = _Pool(_rows())

    _submit(StoredCommandPlanner(cast(SpeechPool, pool), timeout_seconds=2))

    assert pool.timeouts == [2]
    statements = pool.connections[0].statements
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert "statement_timeout" in statements[1]
    assert statements[2].startswith("SELECT operation_id")


def test_the_planner_refuses_an_unbounded_timeout() -> None:
    with pytest.raises(ValueError):
        StoredCommandPlanner(cast(SpeechPool, _Pool({})), timeout_seconds=0)


def test_submission_from_admission_is_pure_and_reusable() -> None:
    planner = _planner(_rows())
    job = _job()
    admission = planner._read(job)

    first = submission_from_admission(job, SUBMIT, admission)
    second = submission_from_admission(job, SUBMIT, admission)

    assert dict(first.body) == dict(second.body)
