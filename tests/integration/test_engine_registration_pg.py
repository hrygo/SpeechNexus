"""Real PostgreSQL registration through the operator command and admission."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import rfc8785
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database

from speechnexus_backend import engine_readiness
from speechnexus_backend.engine_registration import (
    EXIT_UNAVAILABLE,
    RegistrationResult,
    RegistrationSpec,
    _spec_from_recipe,
    _vector,
    register_engine,
)
from speechnexus_backend.engine_registration import main as register_main
from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.transcription import AdmittedExecution, StagedAudio
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_to_document,
)
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    PostgresTranscriptionAdmission,
    PostgresTranscriptionAdmissionFacts,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    load_transcription_recipe,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

WORKSPACE = "workspace-default"
DIGEST = "sha256:" + "a" * 64
AUDIO = b"\x00" * 1024
# steady (4e9, 1e9, 1000) + required (5e9, 1.5e9, 1200): exactly two same-shape
# instances, so a third would fail while the first two stay silent.
TWO_INSTANCE_CAPACITY = "0,9000000000,2500000000,2200,0,0"


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.fixture
def engine_origin() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _recipe_document(
    *,
    instance_id: str = "instance-reg-1",
    model_id: str = "asr-quality",
    node_id: str = "node-reg-1",
    pool_id: str = "asr-batch",
    max_slots: int = 2,
    steady_device_memory_bytes: int | None = None,
) -> dict[str, Any]:
    peak = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 4_000_000_000,
        "host_memory_bytes": 1_000_000_000,
        "cpu_decode_millicores": 1_000,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    # Only a steady below peak makes an observation distinguishable from the
    # prepare-time peak hold; the default keeps the original peak == steady recipe.
    steady = (
        peak
        if steady_device_memory_bytes is None
        else {**peak, "device_memory_bytes": steady_device_memory_bytes}
    )
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
        "model_id": model_id,
        "binding_publication_id": "binding-revision-1",
        "binding_revision_id": "binding-revision-1",
        "manifest_id": "manifest-1",
        "policy_revision_id": "policy-revision-1",
        "service_class": "batch",
        "admission_expires_at_us": 4_000_000_000_000_000,
        "quota_dimensions": ["concurrent_attempts"],
        "stage": {
            "stage_id": "stage-asr-1",
            "instance_id": instance_id,
            "minimum_slots": 1,
            "reserved_slots": 1,
        },
        "runtime_plan": {
            "binding": {
                "target_node_id": node_id,
                "pool_id": pool_id,
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
            "max_slots": max_slots,
            "realtime_reserved_slots": 0,
            "peak": peak,
            "steady": steady,
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


def _write_recipe(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)
    return path


def _register_args(
    database: JobDatabase,
    recipe: Path,
    engine_origin: str,
    *,
    generation: int = 1,
    host_capacity: str | None = TWO_INSTANCE_CAPACITY,
) -> list[str]:
    """Registration arguments with an explicit capacity by default.

    The default command value is this plan's own need, which would leave every
    first registration in this file warning about a second instance. Sizing the
    shared host for two instances keeps the repeat/generation/readiness cases
    (whose subject is not capacity) on a silent stderr; the two capacity tests
    below pass ``host_capacity=None`` or their own value explicitly.
    """
    args = [
        "--database-url",
        database.url,
        "--schema",
        database.schema,
        "--recipe-file",
        str(recipe),
        "--engine-url",
        engine_origin,
        "--generation",
        str(generation),
        "--workspace",
        WORKSPACE,
        "--max-attempts",
        "8",
        "--owner-id",
        "speechnexus-operator",
        "--transaction-timeout-seconds",
        "10",
    ]
    if host_capacity is not None:
        args.extend(["--host-capacity", host_capacity])
    return args


def _run_registration(capsys: pytest.CaptureFixture[str], args: list[str]) -> dict[str, Any]:
    document, stderr = _run_registration_with_stderr(capsys, args)
    assert stderr == "", stderr
    return document


def _run_registration_with_stderr(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> tuple[dict[str, Any], str]:
    """Run the command and keep both streams: the renewal warning is the operator signal."""
    register_main(args)
    captured = capsys.readouterr()
    return cast(dict[str, Any], json.loads(captured.out)), captured.err


def _admit(
    database: JobDatabase,
    recipe: Any,
    *,
    request_id: str,
    submit_intent_id: str,
    asset_id: str,
) -> AdmittedExecution:
    pool = speech_pool(database.url, schema=database.schema, max_size=2)
    pool.open()
    try:
        admission = PostgresTranscriptionAdmission(
            PostgresTranscriptionAdmissionFacts(pool, timeout_seconds=3),
            AdmissionService(PostgresAdmissionTransactions(pool, max_command_bytes=65_536)),
            recipe,
            max_command_bytes=65_536,
        )
        plan = TranscriptionPlan(
            request_id=request_id,
            workspace_id=WORKSPACE,
            model=recipe.model_id,
            binding_publication_id=recipe.binding_publication_id,
            audio=AUDIO,
            audio_media_type="audio/wav",
            audio_sample_rate_hz=16_000,
            audio_channels=1,
            response_format=TranscriptFormat.JSON,
            language="zh",
            timeout_seconds=30,
        )
        staged = StagedAudio(
            asset_id,
            WORKSPACE,
            "transcription-inputs",
            f"private/transcription-inputs/{asset_id}.audio",
            "sha256:" + hashlib.sha256(AUDIO).hexdigest(),
            len(AUDIO),
        )
        return admission.admit(plan, submit_intent_id=submit_intent_id, audio=staged)
    finally:
        pool.close()


def test_command_registers_ready_instance_and_real_admission_succeeds(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    recipe = load_transcription_recipe(recipe_path)

    result = _run_registration(capsys, _register_args(database, recipe_path, engine_origin))

    assert result["outcome"] == "registered"
    assert result["instance_id"] == recipe.instance_id
    assert result["instance_generation"] == 1
    assert result["pool_id"] == "asr-batch"
    assert result["readiness"]["evidence_digest"].startswith("sha256:")
    rows = result["rows"]
    assert isinstance(rows, dict)
    instance = rows["engine_instances"]
    head = rows["runtime_instance_heads"]
    intent = rows["runtime_instance_intents"]
    assert instance["instance_state"] == "ready"
    assert instance["base_budget_kind"] == "steady"
    assert instance["ready_at_us"] is not None
    assert head["current_generation"] == 1
    assert head["current_prepare_intent_id"] == intent["intent_id"]
    assert intent["intent_state"] == "succeeded"
    assert intent["has_observation"] is True and intent["has_readiness"] is True
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1

    admitted = _admit(
        database,
        recipe,
        request_id="request-registration-1",
        submit_intent_id="submit-registration-1",
        asset_id="asset-registration-1",
    )

    assert admitted.instance_id == recipe.instance_id
    assert admitted.instance_generation == 1
    assert admitted.host_node_id == "node-reg-1"
    leases = database.query(
        "SELECT instance_id, instance_generation, pool_id, gateway_id, fencing_token "
        "FROM admission_leases WHERE lease_id = %s",
        (admitted.lease_id,),
    )
    assert leases == [
        {
            "instance_id": recipe.instance_id,
            "instance_generation": 1,
            "pool_id": "asr-batch",
            "gateway_id": "gateway-reg-1",
            "fencing_token": admitted.fencing_token,
        }
    ]


def test_default_capacity_first_registration_warns_that_no_second_instance_fits(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real PG is the only path that runs ``held = held - peak + steady``.

    The default ``--host-capacity`` is this plan's own need, so the committed
    registration leaves exactly one instance worth of room and the warning must
    name the per-dimension remainder a second instance would need.
    """
    recipe_path = _write_recipe(
        tmp_path / "recipe.json",
        _recipe_document(steady_device_memory_bytes=3_000_000_000),
    )
    document, stderr = _run_registration_with_stderr(
        capsys,
        _register_args(database, recipe_path, engine_origin, host_capacity=None),
    )

    assert document["outcome"] == "registered"
    assert document["instance_id"] == "instance-reg-1"
    # steady=3e9 with peak=4e9: the committed hold can only be 3e9 if the successful
    # observation ran held = held - peak + steady. Keeping peak would leave 4e9.
    assert (
        "warning: host node-reg-1 cannot hold another instance of this shape under the "
        "current holds: remaining 0,1000000000,500000000,200,0,0, one more requires "
        "0,4000000000,1500000000,1200,0,0"
    ) in stderr
    assert "release held or quarantined capacity first" in stderr
    assert "--host-capacity" in stderr
    assert stderr.count("\n") == 1
    hosts = database.query(
        "SELECT allocatable_device_memory_bytes, held_device_memory_bytes, "
        "quarantined_device_memory_bytes FROM host_nodes WHERE host_node_id = 'node-reg-1'"
    )
    assert hosts == [
        {
            "allocatable_device_memory_bytes": 4_000_000_000,
            "held_device_memory_bytes": 3_000_000_000,
            "quarantined_device_memory_bytes": 0,
        }
    ]


def test_explicit_capacity_for_two_instances_stays_silent_for_both_and_rejects_a_third(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_recipe = _write_recipe(
        tmp_path / "first.json", _recipe_document(instance_id="instance-reg-1")
    )
    second_recipe = _write_recipe(
        tmp_path / "second.json", _recipe_document(instance_id="instance-reg-2")
    )
    first = _run_registration(capsys, _register_args(database, first_recipe, engine_origin))
    # The host row now exists, so this run cannot rewrite allocatable_*; an explicit
    # capacity that still fits must not produce capacity advice on the second run.
    second = _run_registration(capsys, _register_args(database, second_recipe, engine_origin))

    assert (first["outcome"], second["outcome"]) == ("registered", "registered")
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 2
    hosts = database.query(
        "SELECT allocatable_device_memory_bytes, held_device_memory_bytes "
        "FROM host_nodes WHERE host_node_id = 'node-reg-1'"
    )
    assert hosts == [
        {
            "allocatable_device_memory_bytes": 9_000_000_000,
            "held_device_memory_bytes": 8_000_000_000,
        }
    ]

    third_recipe = _write_recipe(
        tmp_path / "third.json", _recipe_document(instance_id="instance-reg-3")
    )
    with pytest.raises(SystemExit) as exit_info:
        register_main(_register_args(database, third_recipe, engine_origin))
    assert exit_info.value.code == 4
    error = capsys.readouterr().err
    assert "host_capacity_insufficient" in error
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 2


def test_repeat_registration_is_idempotent_and_different_plan_is_rejected(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    args = _register_args(database, recipe_path, engine_origin)
    first = _run_registration(capsys, args)
    _admit(
        database,
        load_transcription_recipe(recipe_path),
        request_id="request-repeat-1",
        submit_intent_id="submit-repeat-1",
        asset_id="asset-repeat-1",
    )
    second = _run_registration(capsys, args)

    assert first["outcome"] == "registered"
    assert second["outcome"] == "already_ready"
    assert second["intent_id"] == first["intent_id"]
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1
    assert database.query("SELECT count(*) AS n FROM runtime_instance_heads")[0]["n"] == 1
    assert database.query("SELECT count(*) AS n FROM runtime_instance_intents")[0]["n"] == 1

    changed = _write_recipe(tmp_path / "changed-recipe.json", _recipe_document(max_slots=1))
    with pytest.raises(SystemExit) as exit_info:
        register_main(_register_args(database, changed, engine_origin))
    assert exit_info.value.code == 4
    error = capsys.readouterr().err
    assert "instance_config_conflict" in error
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1


def test_generation_rollback_is_rejected(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    _run_registration(capsys, _register_args(database, recipe_path, engine_origin))
    database.query(
        "UPDATE runtime_instance_heads SET generation_high_water = 2 WHERE instance_id = %s",
        ("instance-reg-1",),
    )

    with pytest.raises(SystemExit) as exit_info:
        register_main(_register_args(database, recipe_path, engine_origin, generation=2))
    assert exit_info.value.code == 4
    error = capsys.readouterr().err
    assert "stale_instance_generation" in error
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1


def test_expired_readiness_is_refreshed_from_a_new_probe(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    recipe = load_transcription_recipe(recipe_path)
    plan = recipe.approved.stages[0].runtime_plan
    first = _run_registration(capsys, _register_args(database, recipe_path, engine_origin))

    expired = VerifiedRuntimeReadiness(
        plan.binding,
        recipe.instance_id,
        1,
        1,
        plan.topology_digest,
        plan.qualification_digest,
        "sha256:" + "b" * 64,
        1,
        2,
    )
    database.query(
        "UPDATE runtime_instance_intents SET readiness_document = %s, "
        "version = version + 1 WHERE target_node_id = %s AND intent_id = %s",
        (readiness_to_document(expired), "node-reg-1", first["intent_id"]),
    )
    database.query(
        "UPDATE engine_instances SET ready_at_us = 1, version = version + 1 "
        "WHERE instance_id = %s AND instance_generation = 1",
        (recipe.instance_id,),
    )

    refreshed, stderr = _run_registration_with_stderr(
        capsys,
        [*_register_args(database, recipe_path, engine_origin), "--readiness-ttl-seconds", "3600"],
    )

    assert refreshed["outcome"] == "already_ready"
    readiness = refreshed["readiness"]
    assert readiness["verified_at_us"] > 2
    assert readiness["expires_at_us"] > readiness["verified_at_us"]
    assert readiness["evidence_digest"].startswith("sha256:")
    # The run reports the window it found and the fact that it re-signed, and the
    # warning names the refusal an unrenewed deployment would have received (#828).
    assert (readiness["state"], readiness["renewed"]) == ("expired", True)
    assert "warning: readiness expired for instance-reg-1 g1" in stderr
    assert "re-signed by this run" in stderr
    assert "503 engine_not_ready" in stderr
    instance = refreshed["rows"]["engine_instances"]
    assert instance["ready_at_us"] == readiness["verified_at_us"]
    assert database.query("SELECT count(*) AS n FROM engine_instances")[0]["n"] == 1
    admitted = _admit(
        database,
        recipe,
        request_id="request-refresh-1",
        submit_intent_id="submit-refresh-1",
        asset_id="asset-refresh-1",
    )
    assert admitted.instance_id == recipe.instance_id


def _run_report(
    capsys: pytest.CaptureFixture[str], database: JobDatabase, *args: str
) -> tuple[int, list[dict[str, Any]], str]:
    """Run the read-only readiness report against this case's real schema."""
    code = engine_readiness.main(
        ["--database-url", database.url, "--schema", database.schema, *args]
    )
    captured = capsys.readouterr()
    documents = [json.loads(line) for line in captured.out.splitlines() if line]
    return code, documents, captured.err


def test_the_report_and_the_renewal_expose_an_expiring_then_expired_receipt(
    database: JobDatabase,
    engine_origin: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The report names both signals from a real elapsed window, and renewal fixes it.

    A one-second TTL lets the real database clock carry the receipt from `expiring`
    to `expired` without rewriting the stored document, so the classification and
    the exit code are measured against the same window admission would check. The
    renewal then re-signs it from a new `/health` probe -- no clock manipulation and
    no hand-written document -- and the report returns to `valid`.
    """
    import time

    recipe_path = _write_recipe(tmp_path / "recipe.json", _recipe_document())
    recipe = load_transcription_recipe(recipe_path)
    registered = _run_registration(
        capsys,
        [
            *_register_args(database, recipe_path, engine_origin),
            "--readiness-ttl-seconds",
            "1",
        ],
    )
    assert registered["readiness"]["expires_at_us"] > registered["readiness"]["verified_at_us"]

    # Inside the declared window but inside the warning window as well.
    code, report, stderr = _run_report(capsys, database, "--warn-within-seconds", "3600")
    assert code == 0, stderr
    assert report[-1]["summary"] == {
        "ready_instances": 1,
        "valid": 0,
        "expiring": 1,
        "unobserved": 0,
        "expired": 0,
        "missing": 0,
        "invalid": 0,
        "unusable": 0,
    }
    assert report[0]["readiness_state"] == "expiring"
    assert report[0]["remaining_seconds"] >= 0
    assert recipe.instance_id == report[0]["instance_id"]

    # The real clock passes the one-second expiry; nothing is rewritten.
    time.sleep(1.2)
    code, report, stderr = _run_report(capsys, database)
    assert code == EXIT_UNAVAILABLE, stderr
    assert report[0]["readiness_state"] == "expired"
    assert report[0]["remaining_seconds"] < 0
    assert "error: readiness_expired: " in stderr

    # The renewal entry re-signs from a new probe and reports the window it found.
    renewed, stderr = _run_registration_with_stderr(
        capsys,
        [
            *_register_args(database, recipe_path, engine_origin),
            "--readiness-ttl-seconds",
            "3600",
            "--renew-readiness",
        ],
    )
    assert renewed["outcome"] == "already_ready"
    assert (renewed["readiness"]["state"], renewed["readiness"]["renewed"]) == ("expired", True)
    assert "warning: readiness expired for instance-reg-1 g1" in stderr

    code, report, stderr = _run_report(capsys, database, "--warn-within-seconds", "0")
    assert code == 0, stderr
    assert report[0]["readiness_state"] == "valid"
    assert report[-1]["summary"]["unusable"] == 0
    # The renewed window is what the report reads back, not the pre-renewal one.
    assert report[0]["expires_at_us"] == renewed["readiness"]["expires_at_us"]

    # A second renewal on a still-valid receipt is the periodic path: it re-signs
    # unconditionally instead of depending on how early the timer ran.
    again, stderr = _run_registration_with_stderr(
        capsys,
        [
            *_register_args(database, recipe_path, engine_origin),
            "--readiness-ttl-seconds",
            "3600",
            "--renew-readiness",
        ],
    )
    assert (again["readiness"]["state"], again["readiness"]["renewed"]) == ("valid", True)
    assert again["readiness"]["verified_at_us"] > renewed["readiness"]["verified_at_us"]
    # A warning is only printed for a window that needs one.
    assert stderr == ""

    # A short TTL lets the real clock carry a fresh receipt into its own warning
    # window, which is the "renew before it expires" signal the periodic task logs.
    short, _ = _run_registration_with_stderr(
        capsys,
        [
            *_register_args(database, recipe_path, engine_origin),
            "--readiness-ttl-seconds",
            "4",
            "--renew-readiness",
        ],
    )
    time.sleep(3.2)
    warning_ready, stderr = _run_registration_with_stderr(
        capsys,
        [
            *_register_args(database, recipe_path, engine_origin),
            "--readiness-ttl-seconds",
            "4",
            "--renew-readiness",
        ],
    )
    assert (warning_ready["readiness"]["state"], warning_ready["readiness"]["renewed"]) == (
        "expiring",
        True,
    )
    assert "warning: readiness expiring for instance-reg-1 g1" in stderr
    assert short["readiness"]["verified_at_us"] <= warning_ready["readiness"]["verified_at_us"]


def _widened_insert_window(database: JobDatabase) -> None:
    """把两个注册者的竞争窗口拉宽,并留下可复算的「真的同时到过」证据.

    - `workspace_quotas` 的 `BEFORE INSERT` 先 `nextval` 再睡 0.5 s:两个人同时进触发器,
      睡醒后同时撞唯一索引,较晚那位才需要走冲突路径;`nextval` 不随回滚撤销,所以事后
      读到的序列值就是「物理执行过这条 INSERT 的会话数」。
    - `engine_instances` 的 `BEFORE INSERT` 再睡 1 s:把先到者的提交推后,较晚者的
      `SELECT ... FOR UPDATE` 因此有一段可被 `pg_stat_activity` 观察到的真实锁等待。
    """

    database.query("CREATE SEQUENCE quota_insert_attempts")
    database.query(
        "CREATE OR REPLACE FUNCTION quota_insert_probe() RETURNS trigger AS $$ "
        "BEGIN PERFORM nextval('quota_insert_attempts'); PERFORM pg_sleep(0.5); RETURN NEW; END; "
        "$$ LANGUAGE plpgsql"
    )
    database.query(
        "CREATE TRIGGER workspace_quotas_insert_probe BEFORE INSERT ON workspace_quotas "
        "FOR EACH ROW EXECUTE FUNCTION quota_insert_probe()"
    )
    database.query(
        "CREATE OR REPLACE FUNCTION instance_insert_pause() RETURNS trigger AS $$ "
        "BEGIN PERFORM pg_sleep(1.0); RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    database.query(
        "CREATE TRIGGER engine_instances_insert_pause BEFORE INSERT ON engine_instances "
        "FOR EACH ROW EXECUTE FUNCTION instance_insert_pause()"
    )


def _await_quota_lock_wait(database: JobDatabase) -> int:
    """等到一个真的被配额行挡住的后端,返回观察到的并发等待数."""

    import time

    deadline = time.monotonic() + 20
    peak = 0
    while time.monotonic() < deadline:
        rows = database.query(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE cardinality(pg_blocking_pids(pid)) > 0 "
            "AND query LIKE '%workspace_quotas%'"
        )
        peak = max(peak, int(rows[0]["n"]))
        if peak:
            return peak
        time.sleep(0.01)
    return peak


def test_concurrent_registrations_share_one_locked_quota_row(
    database: JobDatabase, engine_origin: str, tmp_path: Path
) -> None:
    """同一工作区的两个并发注册:只有一个配额行,两边拿到的是同一行.

    两个注册者落在不同的 node / pool / instance 上,唯一的共享行就是 `workspace_quotas` ——
    否则他们会先在 `host_nodes` 或 `resource_pools` 的行锁上排队,测到的是别的锁。
    """

    _widened_insert_window(database)
    specs: list[RegistrationSpec] = []
    for suffix, pool_id in (("a", "asr-batch"), ("b", "tts-batch")):
        recipe = load_transcription_recipe(
            _write_recipe(
                tmp_path / f"recipe-race-{suffix}.json",
                _recipe_document(
                    instance_id=f"instance-race-{suffix}",
                    node_id=f"node-race-{suffix}",
                    pool_id=pool_id,
                ),
            )
        )
        specs.append(
            _spec_from_recipe(
                recipe,
                generation=1,
                workspace_id=WORKSPACE,
                max_attempts=8,
                host_capacity=_vector(TWO_INSTANCE_CAPACITY),
                overrides={},
            )
        )

    barrier = threading.Barrier(len(specs))
    results: list[RegistrationResult] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def run(spec: RegistrationSpec) -> None:
        try:
            barrier.wait(timeout=10)
            outcome = register_engine(
                database_url=database.url,
                schema=database.schema,
                spec=spec,
                engine_url=engine_origin,
                transaction_timeout_seconds=20,
            )
        except BaseException as error:
            with guard:
                failures.append(error)
            return
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(spec,), daemon=True) for spec in specs]
    for thread in threads:
        thread.start()
    try:
        observed_wait = _await_quota_lock_wait(database)
    finally:
        for thread in threads:
            thread.join(timeout=120)

    assert not any(thread.is_alive() for thread in threads), "a registration deadlocked"
    assert failures == [], [repr(error) for error in failures]
    assert observed_wait >= 1, "no backend was ever blocked on the quota row"
    quota = database.query(
        "SELECT workspace_id, max_attempts, held_attempts, quarantined_attempts, "
        "policy_revision FROM workspace_quotas"
    )
    assert quota == [
        {
            "workspace_id": WORKSPACE,
            "max_attempts": 8,
            "held_attempts": 0,
            "quarantined_attempts": 0,
            "policy_revision": "policy-revision-1",
        }
    ]
    assert len(results) == 2
    assert {result.outcome for result in results} == {"registered"}
    first, second = results
    assert first.rows["workspace_quotas"] == second.rows["workspace_quotas"]
    attempts = database.query("SELECT last_value FROM quota_insert_attempts")[0]["last_value"]
    assert attempts == 2, "both registrations must have physically executed the quota insert"


def _register_one(
    database: JobDatabase, engine_origin: str, tmp_path: Path, suffix: str, pool_id: str
) -> RegistrationResult:
    recipe = load_transcription_recipe(
        _write_recipe(
            tmp_path / f"recipe-{suffix}.json",
            _recipe_document(
                instance_id=f"instance-{suffix}",
                node_id=f"node-{suffix}",
                pool_id=pool_id,
            ),
        )
    )
    spec = _spec_from_recipe(
        recipe,
        generation=1,
        workspace_id=WORKSPACE,
        max_attempts=8,
        host_capacity=_vector(TWO_INSTANCE_CAPACITY),
        overrides={},
    )
    return register_engine(
        database_url=database.url,
        schema=database.schema,
        spec=spec,
        engine_url=engine_origin,
        transaction_timeout_seconds=20,
    )


def test_the_quota_re_read_waits_for_the_row_lock(
    database: JobDatabase, engine_origin: str, tmp_path: Path
) -> None:
    """配额行已存在时,注册必须在 `SELECT ... FOR UPDATE` 上等锁,而不是读一份快照.

    第一次建行的竞争由插入本身挡住:PostgreSQL 让较晚的写者在唯一索引上等先到者的事务,
    提交后它才看到冲突。所以「同一个锁住的行」这半条保证只能这样证明 —— 第三方持有配额行
    的锁,注册者必须停在那里,直到那把锁释放。
    """

    _register_one(database, engine_origin, tmp_path, "lock-a", "asr-batch")

    locker = cast(Any, database.blocker())
    try:
        locker.execute(
            "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE", (WORKSPACE,)
        )
        result: list[RegistrationResult] = []
        failures: list[BaseException] = []

        def register() -> None:
            try:
                outcome = _register_one(database, engine_origin, tmp_path, "lock-b", "tts-batch")
            except BaseException as error:
                failures.append(error)
                return
            result.append(outcome)

        thread = threading.Thread(target=register, daemon=True)
        thread.start()
        try:
            blocked = _await_quota_lock_wait(database)
        finally:
            locker.rollback()
        thread.join(timeout=120)
    finally:
        locker.close()

    assert not thread.is_alive(), "the registration never returned after the lock was released"
    assert failures == [], [repr(error) for error in failures]
    assert blocked >= 1, "the registration did not wait on the quota row lock"
    assert [outcome.outcome for outcome in result] == ["registered"]
