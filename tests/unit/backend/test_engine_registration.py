"""Registration command parsing, capacity, probing and stable operator failures."""

from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError

import psycopg
import pytest
import rfc8785

from speechnexus_backend import engine_registration as registration
from speechnexus_backend.engine_registration import (
    EXIT_CONFLICT,
    EXIT_INVALID,
    EXIT_UNAVAILABLE,
    EngineProbe,
    RegistrationError,
    RegistrationResult,
    RegistrationSpec,
    _Cursor,
    _default_capacity,
    _evidence,
    _intent_id,
    _iso,
    _one,
    _positive,
    _probe_url,
    _result_json,
    _settings,
    _spec_from_args,
    _spec_from_manual,
    _vector,
    _vector_text,
    main,
    probe_engine,
    register_engine,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
    RuntimeRejectedError,
)

DIGEST = "sha256:" + "a" * 64
DSN = "postgresql://registry:synthetic-private@127.0.0.1:5432/admission_test"
ENGINE_URL = "http://127.0.0.1:18100"
PEAK = PhysicalResources(0, 4_000_000_000, 1_000_000_000, 1_000, 0, 0)
STEADY = PhysicalResources(0, 4_000_000_000, 1_000_000_000, 1_000, 0, 0)
PER_SLOT = PhysicalResources(0, 500_000_000, 250_000_000, 100, 0, 0)


def make_plan(
    *,
    max_slots: int = 2,
    peak: PhysicalResources = PEAK,
    steady: PhysicalResources = STEADY,
    per_slot: PhysicalResources = PER_SLOT,
) -> ApprovedRuntimePlan:
    return ApprovedRuntimePlan(
        RuntimeBinding(
            "node-1",
            "asr-batch",
            "gateway-1",
            "template-1",
            "binding-rev-1",
            "manifest-1",
            DIGEST,
        ),
        DIGEST,
        DIGEST,
        "discrete_device_memory",
        "batch",
        "resident",
        max_slots,
        0,
        peak,
        steady,
        per_slot,
    )


def make_spec(
    *,
    generation: int = 1,
    host_capacity: PhysicalResources | None = None,
    plan: ApprovedRuntimePlan | None = None,
) -> RegistrationSpec:
    runtime_plan = plan or make_plan()
    return RegistrationSpec(
        model_id="asr-quality",
        workspace_id="workspace-default",
        policy_revision_id="policy-revision-1",
        instance_id="instance-1",
        generation=generation,
        plan=runtime_plan,
        host_capacity=host_capacity or _default_capacity(runtime_plan),
        max_attempts=8,
    )


MANUAL_ARGUMENTS: dict[str, object] = {
    "recipe_file": None,
    "database_url": None,
    "schema": None,
    "engine_url": ENGINE_URL,
    "generation": 1,
    "workspace": "workspace-default",
    "max_attempts": 8,
    "owner_id": "speechnexus-operator",
    "fencing_token": None,
    "owner_ttl_seconds": 86_400,
    "readiness_ttl_seconds": 86_400,
    "intent_deadline_seconds": 300,
    "intent_id": None,
    "health_timeout_seconds": 5.0,
    "transaction_timeout_seconds": 10.0,
    "model": "asr-quality",
    "node": "node-1",
    "pool": "asr-batch",
    "gateway": "gateway-1",
    "instance": "instance-1",
    "template_id": "template-1",
    "binding_revision_id": "binding-rev-1",
    "manifest_id": "manifest-1",
    "config_digest": DIGEST,
    "topology_digest": DIGEST,
    "qualification_digest": DIGEST,
    "architecture": "discrete_device_memory",
    "service_class": "batch",
    "loading_policy": "resident",
    "max_slots": 2,
    "realtime_reserved_slots": 0,
    "peak": PEAK,
    "steady": STEADY,
    "per_slot": PER_SLOT,
    "host_capacity": None,
    "policy_revision": "policy-revision-1",
}


def manual_args(**changes: object) -> argparse.Namespace:
    return argparse.Namespace(**(MANUAL_ARGUMENTS | changes))


def manual_argv(*, drop: tuple[str, ...] = (), **changes: str) -> list[str]:
    values: dict[str, str] = {
        "--database-url": DSN,
        "--schema": "speech",
        "--engine-url": ENGINE_URL,
        "--generation": "1",
        "--model": "asr-quality",
        "--node": "node-1",
        "--pool": "asr-batch",
        "--gateway": "gateway-1",
        "--instance": "instance-1",
        "--template-id": "template-1",
        "--binding-revision-id": "binding-rev-1",
        "--manifest-id": "manifest-1",
        "--config-digest": DIGEST,
        "--topology-digest": DIGEST,
        "--qualification-digest": DIGEST,
        "--architecture": "discrete_device_memory",
        "--service-class": "batch",
        "--loading-policy": "resident",
        "--max-slots": "2",
        "--realtime-reserved-slots": "0",
        "--peak": _vector_text(PEAK),
        "--steady": _vector_text(STEADY),
        "--per-slot": _vector_text(PER_SLOT),
        "--policy-revision": "policy-revision-1",
        "--workspace": "workspace-default",
        "--max-attempts": "8",
    }
    for flag in drop:
        values.pop(flag)
    values.update({("--" + key.replace("_", "-")): value for key, value in changes.items()})
    argv: list[str] = []
    for flag, value in values.items():
        argv.extend((flag, value))
    return argv


def make_probe() -> EngineProbe:
    return EngineProbe(
        url=ENGINE_URL + "/health",
        status=200,
        content_type="application/json",
        body_sha256="sha256:" + "b" * 64,
        body_bytes=15,
        observed_at_us=1_700_000_000_000_000,
    )


def recipe_document() -> dict[str, Any]:
    peak = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 4_000_000_000,
        "host_memory_bytes": 1_000_000_000,
        "cpu_decode_millicores": 1_000,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    per_slot = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 500_000_000,
        "host_memory_bytes": 250_000_000,
        "cpu_decode_millicores": 100,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    document: dict[str, Any] = {
        "protocol": "transcription_admission_v1",
        "model_id": "asr-quality",
        "binding_publication_id": "binding-publication-1",
        "binding_revision_id": "binding-revision-1",
        "manifest_id": "manifest-1",
        "policy_revision_id": "policy-revision-1",
        "service_class": "batch",
        "admission_expires_at_us": 4_000_000_000_000_000,
        "quota_dimensions": ["concurrent_attempts"],
        "stage": {
            "stage_id": "stage-asr-1",
            "instance_id": "instance-reg-1",
            "minimum_slots": 1,
            "reserved_slots": 1,
        },
        "runtime_plan": {
            "binding": {
                "target_node_id": "node-1",
                "pool_id": "asr-batch",
                "gateway_id": "gateway-reg-1",
                "approved_template_id": "template-1",
                "binding_revision_id": "binding-revision-1",
                "manifest_id": "manifest-1",
                "config_digest": DIGEST,
            },
            "topology_digest": DIGEST,
            "qualification_digest": DIGEST,
            "memory_architecture": "discrete_device_memory",
            "service_class": "batch",
            "loading_policy": "resident",
            "max_slots": 2,
            "realtime_reserved_slots": 0,
            "peak": peak,
            "steady": peak,
            "per_slot": per_slot,
        },
        "asr": {
            "silence_duration_ms": 400,
            "preview_interval_ms": 1_000,
            "temperature": 0.0,
            "final_deadline_ms": 1_000,
            "auxiliary_deadline_ms": 5_000,
        },
    }
    body = rfc8785.dumps(cast(Any, document))
    document["recipe_digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return document


def write_recipe(path: Path, *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(recipe_document(), separators=(",", ":")), encoding="utf-8")
    path.chmod(mode)
    return path


def recipe_args(path: Path, **changes: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "recipe_file": str(path),
        "generation": 1,
        "workspace": "workspace-default",
        "max_attempts": 8,
        "host_capacity": None,
        "node": None,
        "pool": None,
        "gateway": None,
        "instance": None,
        "model": None,
    }
    return argparse.Namespace(**(values | changes))


class Health(BaseHTTPRequestHandler):
    body = b'{"status":"ok"}'

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.fixture
def health_origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Response:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class RawCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = -1

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RawCursor:
        self.queries.append((query, parameters))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class Connection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


class RefusesConnect:
    def __init__(self, records: list[str]) -> None:
        self.records = records

    def __call__(self, *args: object, **kwargs: object) -> Connection:
        self.records.append("connect")
        raise AssertionError("validation must fail before connecting")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1,2,3", "six comma-separated integers"),
        ("0,0,0,0,0", "six comma-separated integers"),
        ("0,0,0,0,0,0,0", "six comma-separated integers"),
        ("0,0,0,0,0,x", "decimal integers"),
        ("0,0,0,0,0,1.5", "decimal integers"),
        ("0,0,0,0,0,-1", "non-negative"),
        ("0,0,0,0,0,9007199254740992", "non-negative"),
    ],
)
def test_capacity_vector_rejects_malformed_or_out_of_range_values(text: str, message: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=message):
        _vector(text)


def test_capacity_vector_round_trips_six_dimensions() -> None:
    vector = _vector(" 1, 2,3,4,5,6 ")
    assert vector == PhysicalResources(1, 2, 3, 4, 5, 6)
    assert _vector_text(vector) == "1,2,3,4,5,6"
    assert _vector_text(_vector("0,0,0,0,0,0")) == "0,0,0,0,0,0"


@pytest.mark.parametrize(
    ("text", "expected"), [("1", 1), ("8", 8), ("9007199254740991", 9007199254740991)]
)
def test_positive_counter_accepts_decimal_integers(text: str, expected: int) -> None:
    assert _positive(text) == expected


@pytest.mark.parametrize("text", ["0", "-1", "1.5", "many"])
def test_positive_counter_rejects_zero_negative_and_text(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive integer"):
        _positive(text)


def test_default_host_capacity_reserves_every_slot_and_covers_the_peak() -> None:
    runtime_plan = make_plan()
    expected = PhysicalResources(0, 5_000_000_000, 1_500_000_000, 1_200, 0, 0)
    assert _default_capacity(runtime_plan) == expected
    assert _vector_text(_default_capacity(runtime_plan)) == "0,5000000000,1500000000,1200,0,0"


def test_default_host_capacity_uses_the_peak_when_it_exceeds_steady_plus_slots() -> None:
    runtime_plan = make_plan(
        steady=PhysicalResources(0, 1_000_000_000, 0, 100, 0, 0),
        per_slot=PhysicalResources(0, 100_000_000, 0, 10, 0, 0),
        peak=PhysicalResources(0, 9_000_000_000, 0, 900, 0, 0),
    )
    assert _default_capacity(runtime_plan) == PhysicalResources(0, 9_000_000_000, 0, 900, 0, 0)


def test_probe_url_appends_health_only_to_a_bare_origin() -> None:
    assert _probe_url("http://127.0.0.1:18100") == "http://127.0.0.1:18100/health"
    assert _probe_url("https://engine.internal/health") == "https://engine.internal/health"
    assert _probe_url("http://engine.internal/custom") == "http://engine.internal/custom"
    assert _probe_url("http://engine.internal/health#frag") == "http://engine.internal/health"
    assert _probe_url("http://engine.internal/health?ready=1") == (
        "http://engine.internal/health?ready=1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1:18100",
        "http://user:pass@127.0.0.1:18100",
        "http:///health",
        "/health",
        "",
    ],
)
def test_probe_url_rejects_non_http_or_credentialed_origins(url: str) -> None:
    with pytest.raises(RegistrationError) as caught:
        _probe_url(url)
    assert (caught.value.code, caught.value.exit_code) == ("invalid_engine_url", EXIT_INVALID)


def test_probe_engine_reports_the_bounded_health_body(health_origin: str) -> None:
    probe = probe_engine(health_origin, timeout_seconds=2)
    assert probe.url == health_origin + "/health"
    assert probe.status == 200
    assert probe.content_type == "application/json"
    assert probe.body_bytes == len(Health.body)
    assert probe.body_sha256 == "sha256:" + hashlib.sha256(Health.body).hexdigest()
    assert probe.observed_at_us > 0


@pytest.mark.parametrize("timeout", [0, -1, 30.5, float("inf")])
def test_probe_engine_rejects_an_unbounded_health_timeout(timeout: float) -> None:
    with pytest.raises(RegistrationError) as caught:
        probe_engine(ENGINE_URL, timeout_seconds=timeout)
    assert (caught.value.code, caught.value.exit_code) == ("invalid_health_timeout", EXIT_INVALID)


def test_probe_engine_rejects_a_non_200_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registration, "urlopen", lambda *_, **__: Response(204, b""))
    with pytest.raises(RegistrationError) as caught:
        probe_engine(ENGINE_URL)
    assert (caught.value.code, caught.value.exit_code) == ("engine_unhealthy", EXIT_UNAVAILABLE)
    assert "HTTP 204" in caught.value.message


def test_probe_engine_rejects_an_oversized_health_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registration,
        "urlopen",
        lambda *_, **__: Response(200, b"x" * (registration._MAX_PROBE_BYTES + 1)),
    )
    with pytest.raises(RegistrationError) as caught:
        probe_engine(ENGINE_URL)
    assert (caught.value.code, caught.value.exit_code) == (
        "engine_health_too_large",
        EXIT_UNAVAILABLE,
    )


def test_probe_engine_maps_http_errors_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object, **__: object) -> Response:
        raise HTTPError(ENGINE_URL + "/health", 503, "busy", cast(Any, {}), None)

    monkeypatch.setattr(registration, "urlopen", fail)
    with pytest.raises(RegistrationError) as caught:
        probe_engine(ENGINE_URL)
    assert (caught.value.code, caught.value.exit_code) == ("engine_unhealthy", EXIT_UNAVAILABLE)
    assert "HTTP 503" in caught.value.message


@pytest.mark.parametrize("error", [URLError("refused"), TimeoutError(), OSError("reset")])
def test_probe_engine_maps_transport_failures_to_unreachable(
    monkeypatch: pytest.MonkeyPatch, error: object
) -> None:
    def fail(*_: object, **__: object) -> Response:
        raise cast(Any, error)

    monkeypatch.setattr(registration, "urlopen", fail)
    with pytest.raises(RegistrationError) as caught:
        probe_engine(ENGINE_URL)
    assert (caught.value.code, caught.value.exit_code) == (
        "engine_unreachable",
        EXIT_UNAVAILABLE,
    )


def test_evidence_digest_is_stable_over_the_probe_document() -> None:
    spec, probe = make_spec(), make_probe()
    digest, document = _evidence(spec, probe)
    assert (
        digest
        == "sha256:"
        + hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )
    assert digest == _evidence(spec, probe)[0]
    assert document["kind"] == "operator_engine_health_probe_v1"
    assert document["probe_url"] == probe.url
    assert document["http_status"] == 200
    assert document["instance_generation"] == 1
    assert document["target_node_id"] == "node-1"


def test_intent_id_stays_bounded_for_long_instance_names() -> None:
    assert _intent_id("instance-1", 3) == "register-instance-1-g3"
    long_name = "instance-" + "a" * 200
    value = _intent_id(long_name, 2)
    assert value.startswith("register-")
    assert len(value) == len("register-") + 48
    assert value != _intent_id(long_name, 3)


def test_iso_formats_microsecond_utc_timestamps() -> None:
    assert _iso(1_700_000_000_123_456) == "2023-11-14T22:13:20.123456Z"
    assert _iso(0) == "1970-01-01T00:00:00.000000Z"


def test_cursor_bounds_every_statement_by_the_transaction_deadline() -> None:
    raw = RawCursor([{"now_us": 7}])
    cursor = _Cursor(raw, time.monotonic() + 5)
    cursor.execute("SELECT 1", (1,))
    assert raw.queries[0][0].startswith("SELECT set_config('statement_timeout'")
    assert raw.queries[1] == ("SELECT 1", (1,))
    assert cursor.fetchall() == [{"now_us": 7}]
    assert cursor.rowcount == -1


def test_cursor_refuses_to_start_past_its_deadline() -> None:
    raw = RawCursor()
    with pytest.raises(RegistrationError) as caught:
        _Cursor(raw, time.monotonic() - 1).execute("SELECT 1")
    assert (caught.value.code, caught.value.exit_code) == (
        "transaction_timeout",
        EXIT_UNAVAILABLE,
    )
    assert raw.queries == []


@pytest.mark.parametrize("rows", [[], [{"pool_id": "a"}, {"pool_id": "b"}]])
def test_one_requires_exactly_one_coordination_row(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(RegistrationError) as caught:
        _one(_Cursor(RawCursor(rows), time.monotonic() + 5), "SELECT 1", ())
    assert caught.value.code == "reconciliation_required"


def test_one_returns_the_single_row() -> None:
    row = _one(_Cursor(RawCursor([{"pool_id": "asr-batch"}]), time.monotonic() + 5), "SELECT 1", ())
    assert row == {"pool_id": "asr-batch"}


def test_manual_mode_lists_every_missing_frozen_parameter() -> None:
    with pytest.raises(RegistrationError) as caught:
        _spec_from_manual(argparse.Namespace())
    assert (caught.value.code, caught.value.exit_code) == ("missing_parameters", EXIT_INVALID)
    for flag in ("--model", "--node", "--peak", "--steady", "--per-slot", "--policy-revision"):
        assert flag in caught.value.message


def test_manual_mode_builds_the_spec_from_explicit_vectors() -> None:
    capacity = PhysicalResources(1, 9_000_000_000, 2_000_000_000, 2_000, 10, 20)
    spec = _spec_from_manual(manual_args(host_capacity=capacity))
    assert (spec.model_id, spec.instance_id, spec.generation) == ("asr-quality", "instance-1", 1)
    assert (spec.workspace_id, spec.policy_revision_id, spec.max_attempts) == (
        "workspace-default",
        "policy-revision-1",
        8,
    )
    assert spec.plan.binding.target_node_id == "node-1"
    assert spec.plan.max_slots == 2
    assert spec.host_capacity == capacity


def test_manual_mode_derives_host_capacity_when_omitted() -> None:
    spec = _spec_from_manual(manual_args())
    assert spec.host_capacity == _default_capacity(spec.plan)


def test_manual_mode_rejects_an_inconsistent_plan(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid runtime digest"):
        _spec_from_manual(manual_args(config_digest="sha256:short"))


def test_recipe_mode_reads_every_fact_from_the_private_recipe(tmp_path: Path) -> None:
    spec = _spec_from_args(recipe_args(write_recipe(tmp_path / "recipe.json")))
    assert (spec.model_id, spec.instance_id) == ("asr-quality", "instance-reg-1")
    assert (spec.workspace_id, spec.policy_revision_id) == (
        "workspace-default",
        "policy-revision-1",
    )
    assert spec.plan.binding.pool_id == "asr-batch"
    assert spec.host_capacity == _default_capacity(spec.plan)


def test_recipe_mode_accepts_overrides_that_match_the_recipe(tmp_path: Path) -> None:
    spec = _spec_from_args(
        recipe_args(
            write_recipe(tmp_path / "recipe.json"),
            node="node-1",
            pool="asr-batch",
            gateway="gateway-reg-1",
            instance="instance-reg-1",
            model="asr-quality",
            generation=4,
        )
    )
    assert spec.generation == 4


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("node", "node-2", "node-1"),
        ("pool", "tts-batch", "asr-batch"),
        ("gateway", "gateway-2", "gateway-reg-1"),
        ("instance", "instance-2", "instance-reg-1"),
        ("model", "asr-other", "asr-quality"),
    ],
)
def test_recipe_mode_refuses_overrides_that_contradict_the_recipe(
    tmp_path: Path, name: str, value: str, expected: str
) -> None:
    with pytest.raises(RegistrationError) as caught:
        _spec_from_args(recipe_args(write_recipe(tmp_path / "recipe.json"), **{name: value}))
    assert (caught.value.code, caught.value.exit_code) == (
        f"{name}_override_conflict",
        EXIT_INVALID,
    )
    assert expected in caught.value.message


def test_recipe_mode_reports_an_unreadable_recipe(tmp_path: Path) -> None:
    shared = write_recipe(tmp_path / "recipe.json", mode=0o644)
    with pytest.raises(RegistrationError) as caught:
        _spec_from_args(recipe_args(shared))
    assert (caught.value.code, caught.value.exit_code) == ("invalid_recipe", EXIT_INVALID)


def test_settings_require_a_dsn_and_accept_the_deployment_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEECHNEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("SPEECHNEXUS_DATABASE_SCHEMA", raising=False)
    with pytest.raises(RegistrationError) as caught:
        _settings(argparse.Namespace(database_url=None, schema=None))
    assert (caught.value.code, caught.value.exit_code) == ("missing_database_url", EXIT_INVALID)
    assert _settings(argparse.Namespace(database_url=DSN, schema=None)) == (DSN, "speech")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", "sn_e2e")
    assert _settings(argparse.Namespace(database_url=DSN, schema=None)) == (DSN, "sn_e2e")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_URL", DSN)
    assert _settings(argparse.Namespace(database_url=None, schema="sn_e2e")) == (DSN, "sn_e2e")


@pytest.mark.parametrize("schema", ["Public", "speech;DROP", "sp", "1speech", "speech-" + "a" * 70])
def test_settings_reject_an_invalid_schema(schema: str) -> None:
    with pytest.raises(RegistrationError) as caught:
        _settings(argparse.Namespace(database_url=DSN, schema=schema))
    assert (caught.value.code, caught.value.exit_code) == ("invalid_schema", EXIT_INVALID)


@pytest.mark.parametrize(
    ("code", "call"),
    [
        (
            "invalid_schema",
            lambda: register_engine(
                database_url=DSN, schema="Public", spec=make_spec(), engine_url=ENGINE_URL
            ),
        ),
        (
            "invalid_generation",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(generation=0),
                engine_url=ENGINE_URL,
            ),
        ),
        (
            "invalid_owner_ttl",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(),
                engine_url=ENGINE_URL,
                owner_ttl_seconds=0,
            ),
        ),
        (
            "invalid_owner_ttl",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(),
                engine_url=ENGINE_URL,
                owner_ttl_seconds=31_536_001,
            ),
        ),
        (
            "invalid_readiness_ttl",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(),
                engine_url=ENGINE_URL,
                readiness_ttl_seconds=0,
            ),
        ),
        (
            "invalid_intent_deadline",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(),
                engine_url=ENGINE_URL,
                intent_deadline_seconds=0,
            ),
        ),
        (
            "invalid_intent_deadline",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(),
                engine_url=ENGINE_URL,
                intent_deadline_seconds=3_601,
            ),
        ),
        (
            "host_capacity_insufficient",
            lambda: register_engine(
                database_url=DSN,
                schema="speech",
                spec=make_spec(host_capacity=PhysicalResources(0, 1, 0, 0, 0, 0)),
                engine_url=ENGINE_URL,
            ),
        ),
    ],
)
def test_register_engine_rejects_invalid_requests_before_any_io(
    monkeypatch: pytest.MonkeyPatch, code: str, call: Callable[[], object]
) -> None:
    records: list[str] = []
    monkeypatch.setattr(psycopg, "connect", RefusesConnect(records))
    with pytest.raises(RegistrationError) as caught:
        call()
    assert (caught.value.code, caught.value.exit_code) == (code, EXIT_INVALID)
    assert records == []


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (
            lambda: psycopg.errors.UndefinedTable(),
            "schema_not_migrated",
            EXIT_UNAVAILABLE,
        ),
        (
            lambda: psycopg.errors.UniqueViolation(),
            "concurrent_registration",
            EXIT_CONFLICT,
        ),
        (lambda: psycopg.Error("broken"), "database_unavailable", EXIT_UNAVAILABLE),
        (
            lambda: RuntimeRejectedError("capacity_exhausted"),
            "capacity_exhausted",
            EXIT_CONFLICT,
        ),
    ],
)
def test_register_engine_maps_connection_failures_to_stable_codes(
    monkeypatch: pytest.MonkeyPatch, error: Callable[[], Exception], code: str, exit_code: int
) -> None:
    def refuse(*_: object, **__: object) -> Connection:
        raise error()

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(RegistrationError) as caught:
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
    assert (caught.value.code, caught.value.exit_code) == (code, exit_code)


def test_register_engine_rolls_back_and_closes_when_the_transaction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Connection()

    def fail(*_: object, **__: object) -> None:
        raise RegistrationError("pool_owner_conflict", "pool is owned by another live owner")

    monkeypatch.setattr(psycopg, "connect", lambda *_, **__: connection)
    monkeypatch.setattr(registration, "_register_in_transaction", fail)
    with pytest.raises(RegistrationError) as caught:
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
    assert caught.value.code == "pool_owner_conflict"
    assert (connection.commits, connection.rollbacks, connection.closes) == (0, 1, 1)


def test_register_engine_maps_runtime_rejections_after_the_connection_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = Connection()

    def reject(*_: object, **__: object) -> None:
        raise RuntimeRejectedError("generation_conflict")

    monkeypatch.setattr(psycopg, "connect", lambda *_, **__: connection)
    monkeypatch.setattr(registration, "_register_in_transaction", reject)
    with pytest.raises(RegistrationError) as caught:
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
    assert (caught.value.code, caught.value.exit_code) == ("generation_conflict", EXIT_CONFLICT)
    assert "runtime transaction rejected the registration" in caught.value.message
    assert (connection.commits, connection.rollbacks, connection.closes) == (0, 1, 1)


def test_register_engine_passes_a_valid_explicit_intent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Connection()
    seen: dict[str, object] = {}

    def capture(*_: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(psycopg, "connect", lambda *_, **__: connection)
    monkeypatch.setattr(registration, "_register_in_transaction", capture)
    register_engine(
        database_url=DSN,
        schema="speech",
        spec=make_spec(),
        engine_url=ENGINE_URL,
        probe=make_probe(),
        intent_id="operator-intent-1",
    )
    assert seen["intent_id"] == "operator-intent-1"
    assert connection.commits == 1


def test_register_engine_rejects_an_invalid_explicit_intent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []
    monkeypatch.setattr(psycopg, "connect", RefusesConnect(records))
    with pytest.raises(ValueError, match="Invalid runtime identity"):
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
            intent_id="Not Valid",
        )
    assert records == []


def test_register_engine_reraises_its_own_failure_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: object, **__: object) -> Connection:
        raise RegistrationError("database_unavailable", "no server", EXIT_UNAVAILABLE)

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(RegistrationError) as caught:
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
    assert (caught.value.code, caught.value.message) == ("database_unavailable", "no server")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (psycopg.errors.UndefinedTable(), "schema_not_migrated"),
        (psycopg.errors.UniqueViolation(), "concurrent_registration"),
        (psycopg.Error("broken"), "database_unavailable"),
    ],
)
def test_register_engine_rolls_back_database_errors_from_the_transaction(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    connection = Connection()

    def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(psycopg, "connect", lambda *_, **__: connection)
    monkeypatch.setattr(registration, "_register_in_transaction", fail)
    with pytest.raises(RegistrationError) as caught:
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
    assert caught.value.code == code
    assert (connection.commits, connection.rollbacks, connection.closes) == (0, 1, 1)


def test_register_engine_commits_and_closes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Connection()
    result = RegistrationResult(
        outcome="registered",
        model_id="asr-quality",
        instance_id="instance-1",
        instance_generation=1,
        intent_id="register-instance-1-g1",
        target_node_id="node-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        fencing_token=1,
        owner_id="speechnexus-operator",
        workspace_id="workspace-default",
        ready_at_us=1,
        readiness_verified_at_us=1,
        readiness_expires_at_us=2,
        evidence_digest=DIGEST,
        evidence_document={},
        probe=make_probe(),
        rows={},
    )
    monkeypatch.setattr(psycopg, "connect", lambda *_, **__: connection)
    monkeypatch.setattr(registration, "_register_in_transaction", lambda *_, **__: result)
    assert (
        register_engine(
            database_url=DSN,
            schema="speech",
            spec=make_spec(),
            engine_url=ENGINE_URL,
            probe=make_probe(),
        )
        is result
    )
    assert (connection.commits, connection.rollbacks, connection.closes) == (1, 0, 1)


def test_result_json_exposes_identity_readiness_probe_and_rows() -> None:
    result = RegistrationResult(
        outcome="already_ready",
        model_id="asr-quality",
        instance_id="instance-1",
        instance_generation=1,
        intent_id="register-instance-1-g1",
        target_node_id="node-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        fencing_token=1,
        owner_id="speechnexus-operator",
        workspace_id="workspace-default",
        ready_at_us=1,
        readiness_verified_at_us=2,
        readiness_expires_at_us=3,
        evidence_digest=DIGEST,
        evidence_document={"kind": "operator_engine_health_probe_v1"},
        probe=make_probe(),
        rows={"engine_instances": {"instance_state": "ready"}},
    )
    document = cast(dict[str, Any], json.loads(_result_json(result)))
    assert set(document) == {
        "outcome",
        "model",
        "instance_id",
        "instance_generation",
        "intent_id",
        "target_node_id",
        "pool_id",
        "gateway_id",
        "fencing_token",
        "owner_id",
        "workspace_id",
        "ready_at_us",
        "readiness",
        "health_probe",
        "rows",
    }
    assert document["outcome"] == "already_ready"
    assert set(document["readiness"]) == {
        "verified_at_us",
        "expires_at_us",
        "evidence_digest",
    }
    assert set(document["health_probe"]) == {
        "url",
        "status",
        "content_type",
        "body_sha256",
        "body_bytes",
        "observed_at_us",
    }
    assert document["rows"] == {"engine_instances": {"instance_state": "ready"}}


def test_main_requires_an_engine_url_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records: list[str] = []
    monkeypatch.delenv("SPEECHNEXUS_ENGINE_HEALTH_URL", raising=False)
    monkeypatch.delenv("SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE", raising=False)
    monkeypatch.setattr(psycopg, "connect", RefusesConnect(records))
    with pytest.raises(SystemExit) as exited:
        main(manual_argv(drop=("--engine-url",)))
    assert exited.value.code == EXIT_INVALID
    assert "error: missing_engine_url:" in capsys.readouterr().err
    assert records == []


@pytest.mark.parametrize(
    "code",
    [
        "pool_owner_conflict",
        "stale_instance_generation",
        "previous_generation_active",
        "instance_config_conflict",
    ],
)
def test_main_maps_operator_conflicts_to_exit_code_and_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    def fail(*_: object, **__: object) -> RegistrationResult:
        raise RegistrationError(code, "operator must reconcile this instance")

    monkeypatch.setattr(registration, "register_engine", fail)
    with pytest.raises(SystemExit) as exited:
        main(manual_argv())
    assert exited.value.code == EXIT_CONFLICT
    assert capsys.readouterr().err == f"error: {code}: operator must reconcile this instance\n"


def test_main_never_leaks_an_unmodelled_value_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_: object, **__: object) -> RegistrationResult:
        raise ValueError("Invalid runtime identity")

    monkeypatch.setattr(registration, "register_engine", fail)
    with pytest.raises(SystemExit) as exited:
        main(manual_argv())
    assert exited.value.code == EXIT_INVALID
    assert capsys.readouterr().err == "error: invalid_request: Invalid runtime identity\n"


def test_main_reports_an_unparseable_plan_as_invalid_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records: list[str] = []
    monkeypatch.setattr(psycopg, "connect", RefusesConnect(records))
    with pytest.raises(SystemExit) as exited:
        main(manual_argv(config_digest="not-a-digest"))
    assert exited.value.code == EXIT_INVALID
    assert "error: invalid_request:" in capsys.readouterr().err
    assert records == []


def test_main_prints_the_registration_result_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = RegistrationResult(
        outcome="registered",
        model_id="asr-quality",
        instance_id="instance-1",
        instance_generation=1,
        intent_id="register-instance-1-g1",
        target_node_id="node-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        fencing_token=1,
        owner_id="speechnexus-operator",
        workspace_id="workspace-default",
        ready_at_us=1,
        readiness_verified_at_us=2,
        readiness_expires_at_us=3,
        evidence_digest=DIGEST,
        evidence_document={},
        probe=make_probe(),
        rows={},
    )
    monkeypatch.setattr(registration, "register_engine", lambda *_, **__: result)
    main(manual_argv())
    document = cast(dict[str, Any], json.loads(capsys.readouterr().out))
    assert document["outcome"] == "registered"
    assert document["instance_id"] == "instance-1"
    assert document["readiness"]["evidence_digest"] == DIGEST


def test_main_reads_the_schema_and_workspace_from_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def capture(**kwargs: object) -> RegistrationResult:
        seen.update(kwargs)
        raise RegistrationError("stop_after_capture", "captured arguments", EXIT_CONFLICT)

    monkeypatch.setattr(registration, "register_engine", capture)
    with pytest.raises(SystemExit) as exited:
        main(manual_argv(schema="sn_e2e", workspace="workspace-custom", generation="3"))
    assert exited.value.code == EXIT_CONFLICT
    assert capsys.readouterr().err.startswith("error: stop_after_capture:")
    assert seen["schema"] == "sn_e2e"
    assert seen["engine_url"] == ENGINE_URL
    spec = cast(RegistrationSpec, seen["spec"])
    assert (spec.workspace_id, spec.generation) == ("workspace-custom", 3)


def test_the_documented_module_entry_point_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["speechnexus_backend.engine_registration", "--help"])
    monkeypatch.delitem(sys.modules, "speechnexus_backend.engine_registration", raising=False)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("speechnexus_backend.engine_registration", run_name="__main__")
    assert exited.value.code == 0
    assert "--generation" in capsys.readouterr().out
