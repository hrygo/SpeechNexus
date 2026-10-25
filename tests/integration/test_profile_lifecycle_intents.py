"""Private intent persistence, not publication or real Profile qualification."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, cast

import psycopg
import pymysql
import pytest
from alembic import command as migration
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_production_jobs import ROOT, FaultConnection
from tests.integration.test_production_jobs import database as database
from tests.integration.test_profile_drafts import Profiles
from tests.integration.test_profile_drafts import profiles as profiles
from tests.support.profile_database import MySQLProfileDrafts

from speechnexus_backend.speech.application.profile_lifecycle_intents import ProfileLifecycleIntents
from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_lifecycle_intents import ProfileLifecycleIntentResult
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intent_schema import (
    PROFILE_LIFECYCLE_INTENT_DDL,
    profile_lifecycle_intent_grants,
)
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intents_postgres import (
    PostgresProfileLifecycleIntents,
)
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intents_sql import (
    record_intent,
    recover_intent,
)
from speechnexus_backend.speech.interfaces.profile_lifecycle import (
    prepare_profile_activation,
    prepare_profile_publication,
)

pytestmark = pytest.mark.integration
TABLE = "speech_profile_lifecycle_intents"
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/profile-lifecycle-http.json").read_text())[
    "examples"
]


def prepared(
    operation: str = "publish", key: str | None = "original-profile-key"
) -> ProfileLifecycleCommand:
    value = copy.deepcopy(next(e["value"] for e in EXAMPLES if e["id"] == operation + "-original"))
    factory = prepare_profile_publication if operation == "publish" else prepare_profile_activation
    return factory(value["path"]["profile_id"], value["path"]["revision"], value["body"], key)


class MySQLIntentJournal:
    """Unchanged product statements through the existing InnoDB test driver."""

    def __init__(self, driver: MySQLProfileDrafts) -> None:
        self.driver = driver

    def record(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError

        try:
            return self.driver._run(lambda cursor: record_intent(cursor, command, cursor.clock))
        except ProfileUnavailableError:
            return ProfileLifecycleIntentResult("unknown")

    def recover(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError

        try:
            return self.driver._run(lambda cursor: recover_intent(cursor, command), read_only=True)
        except ProfileUnavailableError:
            return ProfileLifecycleIntentResult("unknown")


@dataclass
class Journal:
    profiles: Profiles
    port: ProfileLifecycleIntents

    def rows(self) -> list[dict[str, Any]]:
        return self.profiles.db.query(f"SELECT * FROM {TABLE} ORDER BY key_digest")


@pytest.fixture
def journal(profiles: Profiles) -> Journal:
    db = profiles.db
    if profiles.mysql is not None:
        for ddl in PROFILE_LIFECYCLE_INTENT_DDL:
            db.query(
                ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "MEDIUMTEXT")
            )
        db.query(f"GRANT SELECT, INSERT ON `{db.schema}`.{TABLE} TO '{db.role}'@'%'")
        return Journal(profiles, MySQLIntentJournal(profiles.mysql))
    for grant in profile_lifecycle_intent_grants(db.schema, db.role):
        db.query(grant)
    assert db.pool is not None
    return Journal(profiles, PostgresProfileLifecycleIntents(db.pool))


@pytest.mark.parametrize("operation", ["publish", "activate"])
def test_complete_intent_replays_after_rebuild_without_any_publication_effect(
    journal: Journal, operation: str
) -> None:
    command = prepared(operation)
    before = journal.profiles.snapshot()
    assert journal.port.recover(command).outcome == "unknown"
    assert journal.rows() == []
    first = journal.port.record(command)
    assert first.outcome == "recorded" and first.intent is not None
    assert first.intent.command == command and first.intent.recorded_at_us > 0
    rebuilt: ProfileLifecycleIntents = (
        MySQLIntentJournal(journal.profiles.mysql)
        if journal.profiles.mysql is not None
        else PostgresProfileLifecycleIntents(cast(SpeechPool, journal.profiles.db.pool))
    )
    assert rebuilt.record(command) == replace(first, outcome="replayed")
    assert rebuilt.recover(command).intent == first.intent
    assert journal.profiles.snapshot() == before
    assert journal.profiles.db.counts() == (0, 0)
    # Even a nonexistent target can be journaled: a journal receipt proves no eligibility.
    assert journal.profiles.port.get(command.profile_id, command.revision) is None
    assert command.intent.decode() not in repr(first)


@pytest.mark.parametrize("change", ["cas", "target", "operation", "validation"])
def test_private_key_cannot_be_reinterpreted_and_original_bytes_stay_fixed(
    journal: Journal, change: str
) -> None:
    from speechnexus_backend.speech.application.profile_documents import encode_document

    original = prepared()
    assert journal.port.record(original).outcome == "recorded"
    value = json.loads(original.intent)
    if change == "operation":
        changed = prepared("activate")
    else:
        if change == "cas":
            value["body"]["expected_current_revision"] = "2"
        elif change == "target":
            value["profile_id"] = "another-profile"
            value["body"]["validation"]["profile_id"] = "another-profile"
        else:
            value["body"]["validation"]["notes"] = "synthetic changed evidence metadata"
        changed = replace(original, profile_id=value["profile_id"], intent=encode_document(value))
    before = journal.rows()
    assert journal.port.record(changed).outcome == "conflict"
    assert journal.port.recover(changed).outcome == "conflict"
    assert journal.rows() == before
    assert journal.port.recover(original).outcome == "replayed"


@pytest.mark.parametrize("commit_first", [False, True])
@pytest.mark.parametrize("different", [False, True])
def test_real_unique_key_wait_resolves_commit_or_rollback_before_deciding(
    journal: Journal, commit_first: bool, different: bool
) -> None:
    first = prepared()
    second = prepared("activate") if different else first
    with journal.profiles.writer() as (connection, cursor):
        assert record_intent(cursor, first, cursor.clock).outcome == "recorded"
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(journal.port.record, second)
            try:
                journal.profiles.waiters(1)
                connection.commit() if commit_first else connection.rollback()
            finally:
                connection.rollback()
            result = future.result(timeout=5)
    expected = "recorded" if not commit_first else "conflict" if different else "replayed"
    assert result.outcome == expected
    assert len(journal.rows()) == 1
    recovered = journal.port.recover(first if commit_first else second)
    assert recovered.outcome == "replayed"
    assert journal.profiles.db.counts() == (0, 0)


def test_original_lookup_precedes_new_write_permission_and_missing_recovery_never_inserts(
    journal: Journal,
) -> None:
    command = prepared()
    first = journal.port.record(command)
    assert first.outcome == "recorded"
    db = journal.profiles.db
    target = f'"{db.schema}".{TABLE}' if db.dialect == "postgresql" else f"`{db.schema}`.{TABLE}"
    role = f'"{db.role}"' if db.dialect == "postgresql" else f"'{db.role}'@'%'"
    db.query(f"REVOKE INSERT ON {target} FROM {role}")
    before = journal.rows()
    assert journal.port.record(command).intent == first.intent
    assert journal.port.recover(prepared(key="missing-original-key")).outcome == "unknown"
    assert journal.port.record(prepared(key="new-private-intent")).outcome == "unknown"
    assert journal.rows() == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("request_digest", "sha256:" + "0" * 64),
        ("operation", "activate"),
        ("intent_json", "[" * 10000 + "]" * 10000),
    ],
)
def test_corrupt_history_is_unknown_without_repair_or_provider_calls(
    journal: Journal, column: str, value: str
) -> None:
    command = prepared()
    assert journal.port.record(command).outcome == "recorded"
    journal.profiles.db.query(f"UPDATE {TABLE} SET {column}=%s", (value,))
    before = journal.rows()
    assert journal.port.recover(command).outcome == "unknown"
    assert journal.port.record(command).outcome == "unknown"
    assert journal.rows() == before


def test_runtime_cannot_update_delete_or_truncate_original_intents(journal: Journal) -> None:
    command = prepared()
    journal.port.record(command)
    before = journal.rows()
    for sql in (
        f"UPDATE {TABLE} SET operation='activate'",
        f"DELETE FROM {TABLE}",
        f"TRUNCATE TABLE {TABLE}",
    ):
        with (
            pytest.raises((psycopg.errors.InsufficientPrivilege, pymysql.MySQLError)),
            journal.profiles.writer() as (_, cursor),
        ):
            cursor.execute(sql)
    assert journal.rows() == before


def test_committed_reply_loss_is_unknown_until_read_only_original_recovery(
    journal: Journal,
) -> None:
    command = prepared()
    if journal.profiles.mysql is not None:
        journal.profiles.mysql.lose_reply = True
        result = journal.port.record(command)
    else:
        db = journal.profiles.db

        def configure(connection: FaultConnection) -> None:
            connection.execute(f'SET search_path TO "{db.schema}",pg_catalog')
            connection.commit()

        with ConnectionPool[FaultConnection](
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=1,
            max_size=1,
        ) as pool:
            with pool.connection() as connection:
                armed = connection
            armed.fault = "after"
            result = PostgresProfileLifecycleIntents(cast(SpeechPool, pool)).record(command)
    assert result.outcome == "unknown"
    before = journal.rows()
    assert len(before) == 1
    recovered = journal.port.recover(command)
    assert recovered.outcome == "replayed" and recovered.intent is not None
    assert recovered.intent.command == command
    assert journal.rows() == before


def test_keyless_preparations_are_distinct_private_intents_with_no_public_recovery(
    journal: Journal,
) -> None:
    first, second = prepared(key=None), prepared(key=None)
    assert first.key_digest != second.key_digest and first.intent == second.intent
    assert journal.port.record(first).outcome == "recorded"
    assert journal.port.record(second).outcome == "recorded"
    assert len(journal.rows()) == 2


def test_unreadable_original_stays_unknown_and_does_not_change_the_command(
    journal: Journal,
) -> None:
    command = prepared()
    journal.port.record(command)
    before = journal.rows()
    db = journal.profiles.db
    target = f'"{db.schema}".{TABLE}' if db.dialect == "postgresql" else f"`{db.schema}`.{TABLE}"
    role = f'"{db.role}"' if db.dialect == "postgresql" else f"'{db.role}'@'%'"
    db.query(f"REVOKE SELECT ON {target} FROM {role}")
    try:
        assert journal.port.recover(command).outcome == "unknown"
        assert journal.port.record(command).outcome == "unknown"
        assert journal.port.record(prepared(key="another-private-key")).outcome == "unknown"
        assert journal.rows() == before
    finally:
        db.query(f"GRANT SELECT ON {target} TO {role}")
    assert journal.port.recover(command).outcome == "replayed"


def migrate(journal: Journal, operation: str, revision: str) -> None:
    db = journal.profiles.db
    engine = create_engine(
        db.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = db.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            if operation == "upgrade":
                migration.upgrade(config, revision)
            else:
                migration.downgrade(config, revision)
    finally:
        engine.dispose()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_additive_upgrade_and_empty_only_downgrade_preserve_all_other_history(
    journal: Journal,
) -> None:
    profile = journal.profiles
    profile.port.create(profile.prepare())
    before = profile.snapshot()
    migrate(journal, "downgrade", "0012_execution_results")
    assert profile.db.query(
        "SELECT to_regclass(%s) AS name", (profile.db.schema + "." + TABLE,)
    ) == [{"name": None}]
    migrate(journal, "upgrade", "head")
    migrate(journal, "upgrade", "head")
    for grant in profile_lifecycle_intent_grants(profile.db.schema, profile.db.role):
        profile.db.query(grant)
    assert journal.rows() == [] and profile.snapshot() == before
    assert journal.port.record(prepared()).outcome == "recorded"
    original = journal.rows()
    with pytest.raises(RuntimeError, match="history requires preservation"):
        migrate(journal, "downgrade", "0012_execution_results")
    assert journal.rows() == original and profile.snapshot() == before
    assert profile.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0013_profile_lifecycle_intents"}
    ]


def wait_for_migration(journal: Journal, minimum: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = journal.profiles.db.query(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() "
            "AND cardinality(pg_blocking_pids(pid))>0 AND query LIKE %s",
            ("%speech_profile_lifecycle_intents%",),
        )
        if rows[0]["n"] >= minimum:
            return
        time.sleep(0.01)
    pytest.fail("Expected real journal writer/DDL lock waits were not observed")


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_downgrade_waits_for_original_writer_then_refuses_to_drop_its_history(
    journal: Journal,
) -> None:
    command = prepared()
    with journal.profiles.writer() as (connection, cursor):
        assert record_intent(cursor, command, cursor.clock).outcome == "recorded"
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(migrate, journal, "downgrade", "0012_execution_results")
            try:
                wait_for_migration(journal, 1)
                connection.commit()
            finally:
                connection.rollback()
            with pytest.raises(RuntimeError, match="history requires preservation"):
                future.result(timeout=5)
    assert journal.port.recover(command).outcome == "replayed"
    assert len(journal.rows()) == 1


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_downgrade_first_never_silently_erases_a_successful_writer(journal: Journal) -> None:
    # Queue the migration's ACCESS EXCLUSIVE lock before the writer's SELECT.
    blocker = journal.profiles.db.blocker()
    try:
        blocker.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=2) as workers:
            ddl = workers.submit(migrate, journal, "downgrade", "0012_execution_results")
            try:
                wait_for_migration(journal, 1)
                writer = workers.submit(journal.port.record, prepared())
                wait_for_migration(journal, 2)
                blocker.commit()
            finally:
                blocker.rollback()
            ddl.result(timeout=5)
            assert writer.result(timeout=5).outcome == "unknown"
    finally:
        blocker.rollback()
        blocker.close()
    assert journal.profiles.db.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0012_execution_results"}
    ]
