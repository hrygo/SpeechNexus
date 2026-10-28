"""Real PostgreSQL proof for the supported binding publication command.

The revision is produced by the product draft transaction; the command then
promotes that stored revision and the public read path resolves it. Synthetic
identities prove the chain only; they do not qualify production admission.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.binding_examples import binding_request

from speechnexus_backend.publish_binding import (
    EXIT_CONFLICT,
    EXIT_NOT_STORED,
    main,
    publish_binding,
)
from speechnexus_backend.speech.application.binding_documents import decode_document
from speechnexus_backend.speech.application.binding_publication import (
    MISSING_HEAD,
    BindingHeadPreconditionError,
    BindingIntentConflictError,
    BindingPublicationConflictError,
    BindingPublicationResult,
    BindingPublisher,
    BindingPublishIntent,
    BindingRevisionNotFoundError,
)
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingCapacityManifestMismatchError,
    BindingEvidenceConflictError,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import PostgresBindingDrafts
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    publication_evidence_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publication_schema import publication_grants
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.publications_sql import retire_publication
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_binding_publication"
REPLACEMENT_REVISION_ID = "mbrev-conflict-0001"
CAPACITY = "cap-asr-quality-7"
REPORT = "qrep-asr-quality-7"


@dataclass
class Sandbox:
    pool: SpeechPool
    admin: psycopg.Connection[tuple[object, ...]]
    schema: str
    role: str
    writer_url: str
    observations: dict[tuple[object, ...], dict[str, Any]] = field(default_factory=dict)


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    base = os.environ.get(VARIABLE)
    if not base:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_pubcli_" + suffix, "sn_pubcli_role_" + suffix
    engine = create_engine(
        base.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(base, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in (
        *publication_grants(schema, role),
        *binding_draft_grants(schema, role),
        *publication_evidence_grants(schema, role),
    ):
        admin.execute(grant)
    writer_url = make_conninfo(base, user=role, password=PASSWORD)
    pool = speech_pool(writer_url, schema=schema, max_size=2, name="pub-cli-test")
    pool.open()
    try:
        yield Sandbox(pool, admin, schema, role, writer_url)
    finally:
        pool.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def create_draft(sandbox: Sandbox, key: str = "publish-cli-original-key") -> BindingDraft:
    result = PostgresBindingDrafts(sandbox.pool).create(
        prepare_binding_draft(binding_request(), key)
    )
    assert result.outcome == "created", result
    assert result.draft is not None
    return result.draft


def counts(sandbox: Sandbox) -> tuple[int, int, int]:
    row = sandbox.admin.execute(
        f'SELECT (SELECT count(*) FROM "{sandbox.schema}".speech_publications), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_revisions), '
        f'(SELECT count(*) FROM "{sandbox.schema}".speech_binding_publication_evidence)'
    ).fetchone()
    assert row is not None
    publications, revisions, evidence = row
    assert isinstance(publications, int) and isinstance(revisions, int)
    assert isinstance(evidence, int)
    return publications, revisions, evidence


def publish(
    sandbox: Sandbox,
    draft: BindingDraft,
    *,
    revision_id: str | None = None,
    revision: int | None = None,
    capacity_manifest_id: str = CAPACITY,
    qualification_report_id: str = REPORT,
    expected_current_revision: object = None,
) -> BindingPublicationResult:
    identity = (
        draft.binding_id,
        revision_id,
        revision,
        capacity_manifest_id,
        qualification_report_id,
        expected_current_revision,
    )
    observation = sandbox.observations.get(identity)
    if observation is None:
        observation = PostgresPublications(sandbox.pool).observe_binding_head(draft.binding_id)
        sandbox.observations[identity] = observation
    intent = None
    if expected_current_revision is not MISSING_HEAD:
        assert expected_current_revision is None or isinstance(expected_current_revision, str)
        intent = BindingPublishIntent.from_request(
            installation_id=str(observation["installation_id"]),
            database=str(observation["database_name"]),
            schema=sandbox.schema,
            expected_current_revision=expected_current_revision,
            expected_catalog_revision=int(observation["catalog_revision"]),
            idempotency_key="cli-test-" + str(len(sandbox.observations)),
        )
    return publish_binding(
        database_url=sandbox.writer_url,
        schema=sandbox.schema,
        binding_id=draft.binding_id,
        revision=int(draft.revision) if revision is None else revision,
        revision_id=draft.revision_id if revision_id is None else revision_id,
        capacity_manifest_id=capacity_manifest_id,
        qualification_report_id=qualification_report_id,
        expected_current_revision=expected_current_revision,
        intent=intent,
    )


def test_publish_before_resolution_is_absent_and_after_resolves(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    jobs = PostgresPublicJobs(sandbox.pool)
    assert jobs.latest_binding(draft.binding_id) is None, "an unpublished draft must not resolve"
    assert jobs.catalog_revision() == 1
    assert counts(sandbox) == (0, 1, 0)

    result = publish(sandbox, draft)

    assert result.created is True
    assert counts(sandbox) == (1, 1, 1)
    assert jobs.catalog_revision() == 2
    fact = jobs.latest_binding(draft.binding_id)
    assert fact is not None
    assert fact.publication.kind == "binding"
    assert fact.publication.object_id == draft.binding_id
    assert str(fact.publication.revision) == draft.revision
    assert fact.publication.revision_id == draft.revision_id
    assert fact.definition == decode_document(draft.definition)


def test_repeated_publish_of_the_same_triple_keeps_the_row_count(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    first = publish(sandbox, draft)
    before = counts(sandbox)

    second = publish(sandbox, draft)
    after = counts(sandbox)

    assert first.created is True
    assert second.created is False
    assert second.publication == first.publication
    assert before == (1, 1, 1)
    assert after == (1, 1, 1)


@pytest.mark.parametrize("expected", [MISSING_HEAD, "9"])
def test_new_publication_rejects_missing_or_stale_observation(
    sandbox: Sandbox, expected: object
) -> None:
    draft = create_draft(sandbox)
    with pytest.raises(BindingHeadPreconditionError):
        publish(sandbox, draft, expected_current_revision=expected)
    assert counts(sandbox) == (0, 1, 0)
    assert PostgresPublicJobs(sandbox.pool).latest_binding(draft.binding_id) is None


def test_replay_after_retirement_returns_original_without_restoring_head(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    first = publish(sandbox, draft)
    store = PostgresPublications(sandbox.pool)
    assert store.retire(first.publication, retired_at_us=first.publication.published_at_us + 1)
    before = counts(sandbox)
    replay = publish(sandbox, draft, expected_current_revision=None)
    assert not replay.created
    assert replay.publication == first.publication
    assert replay.evidence == first.evidence
    assert counts(sandbox) == before
    assert PostgresPublicJobs(sandbox.pool).latest_binding(draft.binding_id) is None


def test_legacy_fact_is_distinct_from_a_verified_replay(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    sandbox.admin.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', %s, %s, %s, 1)",
        (draft.binding_id, draft.revision, draft.revision_id),
    )
    original = publish(sandbox, draft)
    assert original.outcome == "existing_fact" and not original.original_created
    assert not original.created
    replay = publish(sandbox, draft)
    assert replay.outcome == "replay" and not replay.original_created and not replay.created
    assert replay.publication == original.publication and replay.evidence == original.evidence
    assert PostgresPublicJobs(sandbox.pool).latest_binding(draft.binding_id) is None


def test_changed_database_target_rejects_before_writing(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    store = PostgresPublications(sandbox.pool)
    observed = store.observe_binding_head(draft.binding_id)
    intent = BindingPublishIntent.from_request(
        installation_id=str(observed["installation_id"]),
        database="wrong-database",
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(observed["catalog_revision"]),
        idempotency_key="target-key",
    )
    with pytest.raises(BindingHeadPreconditionError, match="publication_target_changed"):
        publish_binding(
            database_url=sandbox.writer_url,
            schema=sandbox.schema,
            binding_id=draft.binding_id,
            revision=int(draft.revision),
            revision_id=draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision=None,
            intent=intent,
        )
    assert counts(sandbox) == (0, 1, 0)


def test_runtime_role_cannot_rewrite_or_remove_intent_or_installation(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    publish(sandbox, draft)
    with psycopg.connect(sandbox.writer_url, autocommit=True) as connection:
        connection.execute(f'SET search_path TO "{sandbox.schema}", pg_catalog')
        for statement in (
            "UPDATE speech_binding_publish_intents SET intent='{}'::jsonb",
            "DELETE FROM speech_binding_publish_intents",
            "UPDATE speech_publication_installation SET installation_id=gen_random_uuid()",
            "DELETE FROM speech_publication_installation",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)


def test_committed_reply_loss_replays_original_and_rejects_changed_intent(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = create_draft(sandbox)
    store = PostgresPublications(sandbox.pool)
    observed = store.observe_binding_head(draft.binding_id)
    intent = BindingPublishIntent.from_request(
        installation_id=str(observed["installation_id"]),
        database=str(observed["database_name"]),
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(observed["catalog_revision"]),
        idempotency_key="lost-reply-key",
    )
    publisher = BindingPublisher(store, now_us=lambda: 100)
    real_run = store._run

    def discard_reply(*args: Any, **kwargs: Any) -> Any:
        real_run(*args, **kwargs)
        raise PublicationUnavailableError

    monkeypatch.setattr(store, "_run", discard_reply)
    with pytest.raises(PublicationUnavailableError):
        publisher.publish(
            draft.binding_id,
            int(draft.revision),
            draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision=None,
            intent=intent,
        )
    assert counts(sandbox) == (1, 1, 1)

    monkeypatch.setattr(store, "_run", real_run)
    replay = publisher.publish(
        draft.binding_id,
        int(draft.revision),
        draft.revision_id,
        capacity_manifest_id=CAPACITY,
        qualification_report_id=REPORT,
        expected_current_revision=None,
        intent=intent,
    )
    assert replay.outcome == "replay" and replay.original_created and not replay.created
    assert counts(sandbox) == (1, 1, 1)
    changed = BindingPublishIntent.from_request(
        installation_id=intent.installation_id,
        database=intent.database,
        schema=intent.schema,
        expected_current_revision="2",
        expected_catalog_revision=intent.expected_catalog_revision,
        idempotency_key="lost-reply-key",
    )
    with pytest.raises(BindingIntentConflictError):
        publisher.publish(
            draft.binding_id,
            int(draft.revision),
            draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision="2",
            intent=changed,
        )
    assert counts(sandbox) == (1, 1, 1)


def test_empty_head_aba_rejects_stale_catalogue_observation(sandbox: Sandbox) -> None:
    first_draft = create_draft(sandbox)
    second_result = PostgresBindingDrafts(sandbox.pool).create(
        prepare_binding_draft({**binding_request(), "supersedes_revision": "1"}, "aba-second-key")
    )
    assert second_result.draft is not None and second_result.draft.revision == "2"
    second_draft = second_result.draft
    store = PostgresPublications(sandbox.pool)
    before = store.observe_binding_head(first_draft.binding_id)
    assert before["current_revision"] is None
    first = publish(sandbox, first_draft)
    assert store.retire(first.publication, retired_at_us=first.publication.published_at_us + 1)
    after = store.observe_binding_head(first_draft.binding_id)
    assert after["current_revision"] is None
    assert after["catalog_revision"] != before["catalog_revision"]
    stale = BindingPublishIntent.from_request(
        installation_id=str(before["installation_id"]),
        database=str(before["database_name"]),
        schema=sandbox.schema,
        expected_current_revision=None,
        expected_catalog_revision=int(before["catalog_revision"]),
        idempotency_key="aba-new-key",
    )
    with pytest.raises(BindingHeadPreconditionError, match="configuration_changed"):
        publish_binding(
            database_url=sandbox.writer_url,
            schema=sandbox.schema,
            binding_id=second_draft.binding_id,
            revision=int(second_draft.revision),
            revision_id=second_draft.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision=None,
            intent=stale,
        )
    assert counts(sandbox) == (1, 2, 1)
    assert store.observe_binding_head(first_draft.binding_id)["current_revision"] is None


def test_publication_waits_for_retirement_and_rejects_old_observation(sandbox: Sandbox) -> None:
    first_draft = create_draft(sandbox)
    second_result = PostgresBindingDrafts(sandbox.pool).create(
        prepare_binding_draft({**binding_request(), "supersedes_revision": "1"}, "race-second-key")
    )
    assert second_result.draft is not None
    second = second_result.draft
    first = publish(sandbox, first_draft)
    store = PostgresPublications(sandbox.pool)
    observed = store.observe_binding_head(first_draft.binding_id)
    intent = BindingPublishIntent.from_request(
        installation_id=str(observed["installation_id"]),
        database=str(observed["database_name"]),
        schema=sandbox.schema,
        expected_current_revision="1",
        expected_catalog_revision=int(observed["catalog_revision"]),
        idempotency_key="race-key",
    )

    def submit() -> BindingPublicationResult:
        return publish_binding(
            database_url=sandbox.writer_url,
            schema=sandbox.schema,
            binding_id=second.binding_id,
            revision=int(second.revision),
            revision_id=second.revision_id,
            capacity_manifest_id=CAPACITY,
            qualification_report_id=REPORT,
            expected_current_revision="1",
            intent=intent,
        )

    blocker_pid = sandbox.admin.info.backend_pid
    with ThreadPoolExecutor(max_workers=1) as executor, sandbox.admin.transaction():
        sandbox.admin.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':binding:' || %s, 0))",
            (first_draft.binding_id,),
        )
        future = executor.submit(submit)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            sandbox.admin.execute("SELECT pg_stat_clear_snapshot()")
            if future.done():
                future.result()
                raise AssertionError("publication did not wait on the binding lock")
            row = sandbox.admin.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE %s = ANY(pg_blocking_pids(pid)))",
                (blocker_pid,),
            ).fetchone()
            assert row is not None
            if row[0]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("publication did not wait on the binding lock")
        with sandbox.admin.cursor(row_factory=dict_row) as cursor:
            assert retire_publication(
                cursor, first.publication, first.publication.published_at_us + 1
            )
    with pytest.raises(BindingHeadPreconditionError, match="configuration_changed"):
        future.result(timeout=3)
    assert counts(sandbox) == (1, 2, 1)
    assert store.observe_binding_head(first_draft.binding_id)["current_revision"] is None


def test_same_revision_with_another_revision_id_is_an_explicit_conflict(
    sandbox: Sandbox,
) -> None:
    draft = create_draft(sandbox)
    publish(sandbox, draft)

    with pytest.raises(BindingPublicationConflictError) as failure:
        publish(sandbox, draft, revision_id=REPLACEMENT_REVISION_ID)

    message = str(failure.value)
    assert f"already published as revision_id={draft.revision_id}" in message
    assert REPLACEMENT_REVISION_ID in message
    assert counts(sandbox) == (1, 1, 1), "a conflict must not write another row"


def test_unstored_or_mismatched_revision_is_rejected_before_insert(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    with pytest.raises(BindingRevisionNotFoundError) as missing:
        publish(sandbox, draft, revision=int(draft.revision) + 1)
    assert "no row" in str(missing.value)

    with pytest.raises(BindingRevisionNotFoundError) as mismatched:
        publish(sandbox, draft, revision_id=REPLACEMENT_REVISION_ID)
    assert f"stores revision_id={draft.revision_id}" in str(mismatched.value)
    assert REPLACEMENT_REVISION_ID in str(mismatched.value)
    assert counts(sandbox) == (0, 1, 0), "rejected validation must leave no row"


def test_command_line_reports_created_replayed_conflict_and_missing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = create_draft(sandbox)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.writer_url)
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    main(["observe", "--binding-id", draft.binding_id, "--schema", sandbox.schema])
    observed = json.loads(capsys.readouterr().out)
    assert observed["current_revision"] is None
    assert observed["schema_name"] == sandbox.schema
    arguments = [
        "--binding-id",
        draft.binding_id,
        "--revision",
        draft.revision,
        "--revision-id",
        draft.revision_id,
        "--capacity-manifest-id",
        CAPACITY,
        "--qualification-report-id",
        REPORT,
        "--expected-current-revision",
        "none",
        "--expected-catalog-revision",
        str(observed["catalog_revision"]),
        "--observed-installation-id",
        str(observed["installation_id"]),
        "--observed-database",
        str(observed["database_name"]),
        "--idempotency-key",
        "cli-test-command",
    ]

    main(arguments)
    assert "created=true" in capsys.readouterr().out

    main(arguments)
    assert "created=false" in capsys.readouterr().out
    assert counts(sandbox) == (1, 1, 1)

    with pytest.raises(SystemExit) as conflict:
        main([*arguments[:5], REPLACEMENT_REVISION_ID, *arguments[6:]])
    assert conflict.value.code == EXIT_CONFLICT
    assert capsys.readouterr().err.startswith("publication intent conflict:")

    with pytest.raises(SystemExit) as missing:
        main([*arguments[:3], str(int(draft.revision) + 1), *arguments[4:-1], "cli-test-missing"])
    assert missing.value.code == EXIT_NOT_STORED
    assert "no row" in capsys.readouterr().err

    assert PostgresPublicJobs(sandbox.pool).latest_binding(draft.binding_id) is not None
    assert counts(sandbox) == (1, 1, 1)


def test_publish_records_capacity_and_qualification_evidence(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    result = publish(sandbox, draft)

    row = sandbox.admin.execute(
        f"SELECT object_id, revision, revision_id, capacity_manifest_id, "
        f"qualification_report_id, evidence_source "
        f'FROM "{sandbox.schema}".speech_binding_publication_evidence'
    ).fetchone()
    assert row == (draft.binding_id, "1", draft.revision_id, CAPACITY, REPORT, "operator_cli")
    assert result.evidence.evidence_source == "operator_cli"
    assert result.evidence.capacity_manifest_id == CAPACITY
    assert result.evidence.qualification_report_id == REPORT


def test_capacity_manifest_mismatch_is_rejected_before_insert(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)

    with pytest.raises(BindingCapacityManifestMismatchError) as failure:
        publish(sandbox, draft, capacity_manifest_id="cap-other-0001")

    assert CAPACITY in str(failure.value)
    assert "cap-other-0001" in str(failure.value)
    assert counts(sandbox) == (0, 1, 0), "an evidence mismatch must not publish"


def test_replay_with_different_evidence_is_rejected(sandbox: Sandbox) -> None:
    draft = create_draft(sandbox)
    publish(sandbox, draft)

    with pytest.raises(BindingEvidenceConflictError):
        publish(sandbox, draft, qualification_report_id="qrep-other-0001")

    assert counts(sandbox) == (1, 1, 1), "different evidence must not overwrite the row"


def test_missing_evidence_arguments_write_nothing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = create_draft(sandbox)
    monkeypatch.setenv("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", sandbox.writer_url)
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", sandbox.schema)
    arguments = [
        "--binding-id",
        draft.binding_id,
        "--revision",
        draft.revision,
        "--revision-id",
        draft.revision_id,
        "--capacity-manifest-id",
        CAPACITY,
    ]

    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 2
    assert "--qualification-report-id" in capsys.readouterr().err
    assert counts(sandbox) == (0, 1, 0)


def test_observe_cli_reports_read_failures_without_tracebacks_or_secrets(sandbox: Sandbox) -> None:
    import subprocess
    import sys

    draft = create_draft(sandbox)
    base_env = {
        "SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL": sandbox.writer_url,
        "SPEECHNEXUS_DATABASE_SCHEMA": sandbox.schema,
        "SPEECHNEXUS_PUBLICATION_TRANSACTION_TIMEOUT_SECONDS": "0.5",
    }

    def observe(
        binding_id: str, env: dict[str, str], *args: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "speechnexus_backend.publish_binding",
                "observe",
                "--binding-id",
                binding_id,
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    missing_config = observe(draft.binding_id, {})
    bad_schema = observe(draft.binding_id, base_env, "--schema", "BAD SCHEMA")
    missing_family = observe("binding-missing", base_env)
    unavailable = observe(
        draft.binding_id,
        base_env
        | {
            "SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL": "postgresql://synthetic_user:synthetic_secret@127.0.0.1:1/synthetic"
        },
    )
    for response in (missing_config, bad_schema):
        assert response.returncode == 2
        assert response.stdout == ""
        assert "Traceback" not in response.stderr
    for response in (missing_family, unavailable):
        assert response.returncode == 5
        assert response.stdout == ""
        assert "observation unavailable" in response.stderr
        assert "Traceback" not in response.stderr
        assert "synthetic_secret" not in response.stderr
        assert sandbox.writer_url not in response.stderr
    success = observe(draft.binding_id, base_env)
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["current_revision"] is None
