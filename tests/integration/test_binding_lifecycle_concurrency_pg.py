"""Concurrent activation uses the production PostgreSQL binding lock and CAS."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.integration.test_binding_lifecycle_pg import Sandbox
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_publications_storage import (
    DEFINITION,
    create_binding,
    create_binding_revision,
)

from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingActivationConflictError,
    BindingLifecycleConflictError,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
    _Cursor,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "keyed,first_target,second_target,reject_second,final_head,catalog_delta,event_count,replay_second",
    [
        pytest.param(keyed, *scenario, False, id=f"{name}-keyed-{keyed}")
        for keyed in (False, True)
        for name, scenario in [
            ("change-change", ("1", "1", True, "1", 1, 1)),
            ("noop-noop", ("2", "2", False, "2", 0, 2)),
            ("noop-change", ("2", "1", False, "1", 1, 2)),
            ("change-noop", ("1", "2", True, "1", 1, 1)),
        ]
    ]
    + [
        pytest.param(True, "1", "1", False, "1", 1, 1, True, id="change-replay"),
        pytest.param(True, "2", "2", False, "2", 0, 1, True, id="noop-replay"),
    ],
)
def test_competing_activations_serialize_and_reject_stale_head(
    sandbox: Sandbox,
    keyed: bool,
    first_target: str,
    second_target: str,
    reject_second: bool,
    final_head: str,
    catalog_delta: int,
    event_count: int,
    replay_second: bool,
) -> None:
    admin = sandbox.admin
    create_binding(admin, DEFINITION)
    create_binding_revision(admin, {**DEFINITION, "revision": "2", "revision_id": "binding-rev-2"})
    admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) VALUES "
        "('binding','tts-quality','1','binding-rev-1',1700000000000001), "
        "('binding','tts-quality','2','binding-rev-2',1700000000000002)"
    )
    admin.execute(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind, object_id, revision, revision_id, capacity_manifest_id, "
        "qualification_report_id, evidence_source, recorded_at_us) VALUES "
        "('binding','tts-quality','1','binding-rev-1','cap-tts-quality-1',"
        "'qrep-tts-quality-1','operator_cli',1700000000000010), "
        "('binding','tts-quality','2','binding-rev-2','cap-tts-quality-2',"
        "'qrep-tts-quality-2','operator_cli',1700000000000011)"
    )
    admin.execute(
        "INSERT INTO speech_publication_heads "
        "(kind, object_id, revision, revision_id, updated_at_us) VALUES "
        "('binding','tts-quality','2','binding-rev-2',1700000000000002)"
    )
    initial_head = admin.execute(
        "SELECT revision, revision_id FROM speech_publication_heads "
        "WHERE kind='binding' AND object_id='tts-quality'"
    ).fetchone()
    assert initial_head == ("2", "binding-rev-2")
    initial_catalog = admin.execute("SELECT revision FROM speech_catalog_state").fetchone()
    assert initial_catalog is not None
    assert isinstance(initial_catalog[0], int)
    initial_publications = admin.execute(
        "SELECT * FROM speech_publications ORDER BY kind, object_id, revision"
    ).fetchall()
    initial_retirements = admin.execute("SELECT * FROM speech_publication_retirements").fetchall()
    assert admin.execute("SELECT count(*) FROM speech_publication_lifecycle_events").fetchone() == (
        0,
    )

    acquired, release = threading.Event(), threading.Event()
    holder_pid: list[int] = []

    class PausedActivation(PostgresBindingLifecycle):
        def _lock(self, cursor: _Cursor, binding_id: str) -> None:
            super()._lock(cursor, binding_id)
            holder_pid.append(cursor.raw.connection.info.backend_pid)
            acquired.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test did not release the binding lock")

    def command(request_id: str, target: str, digest_digit: str) -> BindingActivationCommand:
        return BindingActivationCommand(
            binding_id="tts-quality",
            revision=target,
            request_id=request_id,
            actor_type="anonymous",
            credential_label=None,
            key_digest="sha256:" + digest_digit * 64 if keyed else None,
            intent_digest="sha256:" + digest_digit * 64,
            expected_current_revision="2",
        )

    first = PausedActivation(sandbox.pool, timeout_seconds=15)
    second = PostgresBindingLifecycle(sandbox.pool, timeout_seconds=15)
    with ThreadPoolExecutor(max_workers=2) as workers:
        winner = workers.submit(first.activate, command("activation-winner", first_target, "a"))
        try:
            assert acquired.wait(timeout=5), "first activation did not acquire its lock"
            loser = workers.submit(
                second.activate,
                command("activation-loser", second_target, "a" if replay_second else "b"),
            )
            deadline = time.monotonic() + 5
            blocked_pid = None
            while time.monotonic() < deadline:
                row = admin.execute(
                    "SELECT pid FROM pg_stat_activity WHERE usename=%s AND pid<>%s "
                    "AND %s=ANY(pg_blocking_pids(pid))",
                    (sandbox.role, holder_pid[0], holder_pid[0]),
                ).fetchone()
                if row is not None:
                    blocked_pid = row[0]
                    break
                time.sleep(0.01)
            assert blocked_pid is not None, "second activation did not wait on the first"
            assert blocked_pid != holder_pid[0]
        finally:
            release.set()
        receipt = winner.result(timeout=10)
        if reject_second:
            with pytest.raises(BindingLifecycleConflictError) as rejected:
                loser.result(timeout=10)
            assert rejected.value.code == "configuration_changed"
            assert isinstance(rejected.value, BindingActivationConflictError)
            assert rejected.value.current_revision == first_target
        else:
            next_receipt = loser.result(timeout=10)
            assert next_receipt.revision == second_target
            assert next_receipt.replayed is replay_second

    assert (receipt.revision, receipt.revision_id, receipt.status, receipt.replayed) == (
        first_target,
        "binding-rev-" + first_target,
        "published",
        False,
    )
    assert admin.execute(
        "SELECT revision, revision_id FROM speech_publication_heads "
        "WHERE kind='binding' AND object_id='tts-quality'"
    ).fetchone() == (final_head, "binding-rev-" + final_head)
    assert admin.execute("SELECT revision FROM speech_catalog_state").fetchone() == (
        initial_catalog[0] + catalog_delta,
    )
    assert admin.execute(
        "SELECT action, binding_id, revision, revision_id, result_status, request_id, "
        "policy_revision FROM speech_publication_lifecycle_events ORDER BY request_id DESC"
    ).fetchall() == [
        (
            "model_binding.activate",
            "tts-quality",
            first_target,
            "binding-rev-" + first_target,
            "published",
            "activation-winner",
            initial_catalog[0] + (first_target != "2"),
        )
    ] + (
        [
            (
                "model_binding.activate",
                "tts-quality",
                second_target,
                "binding-rev-" + second_target,
                "published",
                "activation-loser",
                initial_catalog[0] + catalog_delta,
            )
        ]
        if event_count == 2
        else []
    )
    assert (
        admin.execute(
            "SELECT * FROM speech_publications ORDER BY kind, object_id, revision"
        ).fetchall()
        == initial_publications
    )
    assert admin.execute("SELECT * FROM speech_publication_retirements").fetchall() == (
        initial_retirements
    )
