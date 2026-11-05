"""Synthetic SQLite execution of product SQL; NO PostgreSQL/locking qualification."""

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_draft_schema import PROFILE_DRAFT_DDL
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intent_schema import (
    PROFILE_LIFECYCLE_INTENT_DDL,
)
from speechnexus_backend.speech.infrastructure.profile_publication_schema import (
    PROFILE_PUBLICATION_DDL,
)
from speechnexus_backend.speech.infrastructure.profile_publications_postgres import PostgresProfiles
from tests.integration.test_profile_publications import (
    Sandbox,
    SyntheticVerifier,
)
from tests.integration.test_profile_publications import (
    test_draft_retired_disabled_and_missing_cannot_be_activated as check_states,
)
from tests.integration.test_profile_publications import (
    test_publication_activation_replay_before_cas_and_same_key_conflict as check_replay,
)


class Cursor:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.raw: sqlite3.Cursor | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Cursor:
        self.raw = self.connection.execute(
            query.replace("%s", "?").replace(" FOR UPDATE", ""), parameters
        )
        return self

    @property
    def rowcount(self) -> int:
        assert self.raw is not None
        return self.raw.rowcount

    def fetchall(self) -> list[dict[str, Any]]:
        assert self.raw is not None
        return [dict(r) for r in self.raw.fetchall()]

    def clock(self) -> int:
        return 1_000_000


class SyntheticProfiles(PostgresProfiles):
    def __init__(
        self, connection: sqlite3.Connection, verifier: SyntheticVerifier | None = None
    ) -> None:
        super().__init__(cast(SpeechPool, None), verifier=verifier)
        self.connection = connection

    def _run[T](self, operation: Callable[[Any], T], *, read_only: bool = False) -> T:
        with self.connection:
            return operation(Cursor(self.connection))


@pytest.fixture
def sandbox() -> Iterator[Sandbox]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.create_function("OCTET_LENGTH", 1, lambda s: len(s.encode()))
    for ddl in (*PROFILE_DRAFT_DDL, *PROFILE_LIFECYCLE_INTENT_DDL, *PROFILE_PUBLICATION_DDL):
        if "ADD CONSTRAINT" in ddl:
            continue
        connection.execute(ddl.replace('COLLATE "C"', "COLLATE BINARY"))
    verifier = SyntheticVerifier()
    yield Sandbox(
        Cursor(connection),
        cast(SpeechPool, None),
        SyntheticProfiles(connection, verifier),
        verifier,
        "synthetic",
        "synthetic",
        "",
    )
    connection.close()


def test_original_replay_and_cas_rejections(sandbox: Sandbox) -> None:
    check_replay(sandbox)


def test_qualification_failure_is_a_stable_result(sandbox: Sandbox) -> None:
    sandbox.draft()
    sandbox.verifier.reject = True
    assert sandbox.port.mutate(sandbox.publish()).error_code == "profile_not_eligible"
    before = sandbox.snapshot()
    sandbox.verifier.reject = False
    assert sandbox.port.mutate(sandbox.publish()).error_code == "profile_not_eligible"
    assert sandbox.snapshot() == before


def test_unpublished_and_retired_targets_are_rejected(sandbox: Sandbox) -> None:
    check_states(sandbox)


def test_list_contains_only_published_revisions_and_current_pointer(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert s.port.list_publications(limit=2) == ([], False)
    s.port.mutate(s.publish())
    s.draft("2")
    page, more = s.port.list_publications(limit=2)
    assert len(page) == 1 and page[0][0].draft.revision == "1" and page[0][1] == "1"
    assert more is False
