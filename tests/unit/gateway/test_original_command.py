"""Original request verification and one-dispatch bridge; no engine or private data."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import DispatchOutcome
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure import original_command_sql as sql
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress
from tests.fake.execution_command import original_request

NOW = datetime.now(UTC)


def intent_for(request: SubmitRequest) -> SubmissionIntent:
    return SubmissionIntent(
        identity=request.identity,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
        recorded_at=NOW,
        updated_at=NOW,
    )


class Cursor:
    rowcount = 1

    def __init__(self, request: SubmitRequest) -> None:
        self.row: dict[str, Any] = {
            "version": 1,
            "request_digest": request.request_digest,
            "command_digest": request.identity.command_digest,
            "document_present": True,
            "document_bytes": len(request.request_document.encode()),
            "bounded_document": request.request_document,
            "command_redacted_at_us": None,
            "command_deletion_job_id": None,
            "command_deletion_evidence_digest": None,
        }
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        self.calls.append((query, parameters))
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return [self.row]


@pytest.fixture
def setup_reader(monkeypatch: pytest.MonkeyPatch) -> tuple[SubmitRequest, SubmissionIntent, Cursor]:
    request = original_request()
    intent = intent_for(request)
    monkeypatch.setattr(sql, "read_submit_intent", lambda cursor, key: intent)
    return request, intent, Cursor(request)


def test_read_exact_original_and_bound_document_inside_sql(setup_reader: Any) -> None:
    request, intent, cursor = setup_reader
    restored = sql.read_original_request(cursor, intent, max_document_bytes=16384)
    assert restored == request and restored.request_document == request.request_document
    assert len(cursor.calls) == 1
    query, params = cursor.calls[0]
    assert "CASE WHEN OCTET_LENGTH(request_document) <= %s" in query
    assert params == (16384, request.identity.submit_intent_id)
    assert query.startswith("SELECT ") and "FOR UPDATE" not in query


@pytest.mark.parametrize(
    "field,value",
    [
        ("document_present", False),
        ("document_bytes", 999999),
        ("document_bytes", True),
        ("bounded_document", None),
        ("request_digest", "sha256:" + "0" * 64),
        ("command_digest", "sha256:" + "0" * 64),
        ("command_redacted_at_us", 1),
        ("command_deletion_job_id", "delete-input"),
        ("command_deletion_evidence_digest", "sha256:" + "0" * 64),
    ],
)
def test_corrupt_or_redacted_original_is_not_read(
    setup_reader: Any, field: str, value: Any
) -> None:
    _, intent, cursor = setup_reader
    cursor.row[field] = value
    with pytest.raises(SubmissionStoreError, match="cannot be verified"):
        sql.read_original_request(cursor, intent, max_document_bytes=16384)


@pytest.mark.parametrize("corruption", ["identity", "pretty", "duplicate", "nan", "schema", "utf8"])
def test_json_or_identity_tampering_never_reconstructs_defaults(
    setup_reader: Any, corruption: str
) -> None:
    request, intent, cursor = setup_reader
    value = json.loads(request.request_document)
    if corruption == "identity":
        value["execution_request"]["instance_generation"] = 2
    if corruption == "schema":
        del value["execution_request"]["command"]["parameters"]["language"]
    document = (
        json.dumps(value)
        if corruption == "pretty"
        else json.dumps(value, separators=(",", ":"), sort_keys=True)
    )
    if corruption == "duplicate":
        document = '{"output_stream_id":null,' + document[1:]
    if corruption == "nan":
        document = document.replace('"temperature":0', '"temperature":NaN')
    if corruption == "utf8":
        document += "\ud800"
    cursor.row["bounded_document"] = document
    cursor.row["document_bytes"] = len(document.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(SubmissionStoreError):
        sql.read_original_request(cursor, intent, max_document_bytes=16384)


def test_version_change_during_read_is_stale(setup_reader: Any) -> None:
    _, intent, cursor = setup_reader
    cursor.row["version"] = 2
    with pytest.raises(StaleSubmissionVersionError):
        sql.read_original_request(cursor, intent, max_document_bytes=16384)


@pytest.mark.parametrize("change", ["missing", "version", "identity", "digest"])
def test_missing_or_changed_identity_never_reads_body(
    setup_reader: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _, intent, cursor = setup_reader
    current = None if change == "missing" else replace(intent)
    if current is not None:
        if change == "version":
            current.version += 1
        elif change == "identity":
            current.identity = replace(current.identity, instance_generation=2)
        else:
            current.request_digest = "sha256:" + "0" * 64
    monkeypatch.setattr(sql, "read_submit_intent", lambda cursor, key: current)
    expected = SubmissionStoreError if change == "missing" else StaleSubmissionVersionError
    with pytest.raises(expected):
        sql.read_original_request(cursor, intent, max_document_bytes=16384)
    assert not cursor.calls


@pytest.mark.parametrize("limit", [0, True, 32 * 1024 * 1024 + 1])
def test_invalid_read_budget_never_queries(setup_reader: Any, limit: int) -> None:
    _, intent, cursor = setup_reader
    with pytest.raises(ValueError):
        sql.read_original_request(cursor, intent, max_document_bytes=limit)
    assert not cursor.calls


class Source:
    def __init__(self, request: SubmitRequest) -> None:
        self.request = request
        self.fail = False
        self.calls = 0

    def read_request(self, intent: SubmissionIntent, *, max_document_bytes: int) -> SubmitRequest:
        self.calls += 1
        assert max_document_bytes == 16384
        if self.fail:
            raise SubmissionStoreError("unavailable")
        return self.request


class Engine:
    def __init__(self) -> None:
        self.calls: list[SubmitRequest] = []
        self.fail = False

    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        self.calls.append(request)
        if self.fail:
            raise ConnectionError("synthetic lost response")
        return DispatchOutcome(DispatchResult.ACKNOWLEDGED, NOW, "engine-original")


def test_bridge_replays_query_original_and_never_dispatch_again() -> None:
    request = original_request()
    source, engine = Source(request), Engine()
    bridge = OriginalCommandDispatch(commands=source, engine=engine, max_document_bytes=16384)
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    service = SubmitExecutionService(
        ingress=fake_ingress(node_id="node-input", gateway_id="gateway-input"),
        ledger=ledger,
        dispatch=bridge,
        observation=ScriptedEngine(now=lambda: NOW),
        now=lambda: NOW,
    )
    first = service.submit(request)
    replay = service.submit(request)
    assert first.intent.execution_state is ExecutionState.EXECUTING and replay.idempotent_replay
    assert source.calls == 1 and engine.calls == [request]
    assert ledger.get(request.identity.submit_intent_id).version == 2  # type: ignore[union-attr]
    with pytest.raises(InvalidExecutionRequestError):
        bridge.dispatch(first.intent)
    assert len(engine.calls) == 1


@pytest.mark.parametrize("failure", ["read", "identity", "digest"])
def test_bridge_rejects_invalid_source_without_engine_send(failure: str) -> None:
    request = original_request()
    source, engine = Source(request), Engine()
    if failure == "read":
        source.fail = True
    elif failure == "identity":
        source.request = replace(request, identity=replace(request.identity, instance_generation=2))
    else:
        source.request = replace(request, command={"corrupt": True})
    bridge = OriginalCommandDispatch(commands=source, engine=engine, max_document_bytes=16384)
    assert bridge.dispatch(intent_for(request)).result is DispatchResult.NOT_SENT
    assert not engine.calls


def test_engine_adapter_failure_after_handoff_is_unknown() -> None:
    request = original_request()
    source, engine = Source(request), Engine()
    engine.fail = True
    bridge = OriginalCommandDispatch(commands=source, engine=engine, max_document_bytes=16384)
    assert bridge.dispatch(intent_for(request)).result is DispatchResult.RESPONSE_LOST
    assert engine.calls == [request]
