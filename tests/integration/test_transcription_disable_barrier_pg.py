"""Synchronous ASR admission races an actual binding disable in PostgreSQL."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_admission_pg import (
    FrozenFacts,
    _adapter,
    _audio,
    _facts,
    _identity,
    _plan,
    _recipe,
)

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.transcription import TranscriptionUnavailableError
from speechnexus_backend.speech.domain.binding_lifecycle import BindingRetirementCommand
from speechnexus_backend.speech.infrastructure import admission_postgres
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
    binding_disable_admission_grants,
)
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import lock_binding
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
    _Cursor,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    binding_lifecycle_grants,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


def _seed(admission: AdmissionDatabase) -> None:
    db = admission.runtime.foundation.database
    digest = "sha256:" + "b" * 64
    db.query(
        "INSERT INTO speech_binding_families (binding_id,latest_revision) "
        "VALUES ('asr-quality','1')"
    )
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'asr-quality',1,'{}',%s)",
        (digest, digest),
    )
    db.query(
        "INSERT INTO speech_binding_revisions (binding_id,revision,revision_id,key_digest,"
        "document_version,definition_json,definition_digest,request_digest,created_at_us) "
        "VALUES ('asr-quality','1','binding-test',%s,1,'{}',%s,%s,1)",
        (digest, digest, digest),
    )
    db.query(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','asr-quality','1','binding-test',1)"
    )
    db.query(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind,object_id,revision,revision_id,capacity_manifest_id,qualification_report_id,"
        "evidence_source,recorded_at_us) VALUES ('binding','asr-quality','1','binding-test',"
        "'manifest-test','qualification-test','operator_cli',1)"
    )
    db.query(
        "INSERT INTO speech_publication_heads (kind,object_id,revision,revision_id,updated_at_us) "
        "VALUES ('binding','asr-quality','1','binding-test',1)"
    )
    for grant in binding_disable_admission_grants(
        db.schema, admission.runtime.foundation.speech_role
    ):
        db.query(grant)
    for grant in binding_lifecycle_grants(db.schema, db.role):
        db.query(grant)


@pytest.mark.parametrize("disable_first", [True, False])
def test_transcription_admission_and_disable_serialize(
    admission_database: AdmissionDatabase, monkeypatch: pytest.MonkeyPatch, disable_first: bool
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _seed(admission)
    assert admission.runtime.pool is not None and db.pool is not None
    port = PostgresAdmissionTransactions(
        admission.runtime.pool,
        max_command_bytes=1_048_576,
        timeout_seconds=10,
        enforce_binding_disable=True,
    )
    recipe = _recipe(admission)
    plan = _plan("request-disable-race")
    adapter = _adapter(
        admission,
        recipe,
        facts=FrozenFacts(_facts(admission).read(plan, recipe)),
        service=AdmissionService(port),
    )
    acquired, release = threading.Event(), threading.Event()
    pid: list[int] = []
    original_lock = lock_binding

    def pause_admission(cursor: Any, binding_id: str) -> None:
        original_lock(cursor, binding_id)
        if not disable_first:
            cursor.execute("SELECT pg_backend_pid() AS pid")
            pid.append(cursor.fetchall()[0]["pid"])
            acquired.set()
            if not release.wait(8):
                raise RuntimeError("admission was not released")

    monkeypatch.setattr(admission_postgres, "lock_binding", pause_admission)

    class PausedLifecycle(PostgresBindingLifecycle):
        def _lock(self, cursor: _Cursor, binding_id: str) -> None:
            super()._lock(cursor, binding_id)
            if disable_first:
                pid.append(cursor.raw.connection.info.backend_pid)
                acquired.set()
                if not release.wait(8):
                    raise RuntimeError("disable was not released")

    lifecycle = PausedLifecycle(db.pool, timeout_seconds=10)
    disable = BindingRetirementCommand(
        binding_id="asr-quality",
        revision="1",
        request_id="disable-asr-race",
        actor_type="anonymous",
        credential_label=None,
        key_digest=None,
        intent_digest="sha256:" + "d" * 64,
        mode="disable",
        reason="synthetic test",
    )
    before = admission.snapshot()
    with ThreadPoolExecutor(max_workers=2) as workers:

        def admit() -> Any:
            return adapter.admit(plan, submit_intent_id="submit-disable-race", audio=_audio(plan))

        if disable_first:
            disabled = workers.submit(lifecycle.retire, disable)
            assert acquired.wait(5)
            pending = workers.submit(admit)
        else:
            pending = workers.submit(admit)
            assert acquired.wait(5)
            disabled = workers.submit(lifecycle.retire, disable)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (pid[0],),
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("no competing transaction waited on the binding lock")
        finally:
            release.set()
        assert disabled.result(timeout=10).status == "disabled"
        if disable_first:
            with pytest.raises(TranscriptionUnavailableError, match="policy_or_resource_disabled"):
                pending.result(timeout=10)
            assert admission.snapshot() == before
        else:
            admitted = pending.result(timeout=10)
            operation, attempt, _ = _identity(plan.request_id)
            assert (admitted.operation_id, admitted.attempt_id) == (operation, attempt)
            after = admission.snapshot()
            replay = admit()
            assert (replay.operation_id, replay.attempt_id, replay.lease_id) == (
                admitted.operation_id,
                admitted.attempt_id,
                admitted.lease_id,
            )
            assert admission.snapshot() == after
            different = replace(plan, language="en")
            with pytest.raises(TranscriptionUnavailableError, match="idempotency_conflict"):
                adapter.admit(
                    different, submit_intent_id="submit-disable-race", audio=_audio(different)
                )
            assert admission.snapshot() == after
            new_plan = _plan("request-after-disable")
            with pytest.raises(TranscriptionUnavailableError, match="policy_or_resource_disabled"):
                adapter.admit(
                    new_plan, submit_intent_id="submit-after-disable", audio=_audio(new_plan)
                )
            assert admission.snapshot() == after
