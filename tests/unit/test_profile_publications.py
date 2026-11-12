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


def test_unregistered_source_rolls_back_new_intent_and_pointer(sandbox: Sandbox) -> None:
    from speechnexus_backend.speech.application.profile_lifecycle import (
        UnavailableProfilePublicationVerifier,
    )

    s = sandbox
    s.draft()
    before = s.snapshot()
    # The synthetic transaction rolls back; the product boundary preserves unknown.
    s.port._verifier = UnavailableProfilePublicationVerifier()
    assert s.port.mutate(s.publish()).outcome == "unknown"
    assert s.snapshot() == before


def test_result_replay_preserves_original_snapshot_after_retirement(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    first = s.port.mutate(s.publish())
    s.admin.execute(
        "UPDATE speech_profile_publications SET status='retired',retired_at_us=published_at_us"
    )
    before = s.snapshot()
    replay = s.port.mutate(s.publish())
    assert replay.outcome == "replayed" and replay.publication == first.publication
    assert replay.publication is not None and replay.publication.status == "published"
    assert s.port.list_publications() == ([], False)
    assert s.snapshot() == before


def test_failed_result_write_rolls_back_publication_and_head(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    before = s.snapshot()
    s.admin.execute(
        "CREATE TRIGGER reject_synthetic_result BEFORE INSERT ON "
        "speech_profile_lifecycle_results BEGIN "
        "SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        s.port.mutate(s.publish())
    assert s.snapshot() == before


def test_retirement_clears_the_current_pointer_and_replays_the_original_result(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    identity = "caption-draft"
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    retired = s.port.mutate(s.retirement({"mode": "retire"}, "retire-key"))
    assert retired.outcome == "completed" and retired.publication is not None
    publication = retired.publication
    assert publication.status == "retired"
    assert publication.retired_at_us is not None
    assert publication.retired_at_us >= publication.published_at_us
    assert publication.disabled_reason is None
    assert (
        s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0][
            "current_revision"
        ]
        is None
    )
    # The revision leaves new-reference lists but keeps its fixed history read.
    assert s.port.list_publications(limit=5) == ([], False)
    fixed = s.port.get_publication(identity, "1")
    assert fixed is not None and fixed.status == "retired"
    after = s.snapshot()
    replay = s.port.mutate(s.retirement({"mode": "retire"}, "retire-key"))
    assert replay.outcome == "replayed" and replay.publication == publication
    assert s.snapshot() == after
    # The same key cannot carry a changed mode or reason.
    conflict = s.port.mutate(
        s.retirement({"mode": "disable", "reason": "changed intent"}, "retire-key")
    )
    assert conflict.outcome == "conflict" and conflict.error_code == "idempotency_conflict"
    assert s.snapshot() == after
    # A new key on a terminal revision is a stable rejection, not a second write.
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "terminal-key")).error_code
        == "profile_not_published"
    )
    rejected = s.snapshot()
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "terminal-key")).error_code
        == "profile_not_published"
    )
    assert s.snapshot() == rejected


def test_disable_records_its_reason_while_retirement_records_only_the_time(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    disabled = s.port.mutate(
        s.retirement({"mode": "disable", "reason": "synthetic operator disable"}, "disable-key")
    )
    assert disabled.outcome == "completed" and disabled.publication is not None
    publication = disabled.publication
    assert publication.status == "disabled"
    assert publication.disabled_reason == "synthetic operator disable"
    assert publication.retired_at_us is None
    row = s.admin.execute(
        "SELECT status,retired_at_us,disabled_reason FROM speech_profile_publications"
    ).fetchall()[0]
    assert row == {
        "status": "disabled",
        "retired_at_us": None,
        "disabled_reason": "synthetic operator disable",
    }
    # Both terminal modes remove the revision from new-reference selection.
    assert s.port.list_publications(limit=5) == ([], False)
    assert s.port.mutate(s.activate("1", None, "activate-disabled")).error_code == (
        "profile_not_published"
    )


def test_retiring_a_non_current_revision_preserves_the_current_pointer(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    s.draft("2")
    assert s.port.mutate(s.publish("2", "1", "publish-second")).outcome == "completed"
    assert s.port.mutate(s.retirement({"mode": "retire"}, "retire-old")).outcome == "completed"
    current = s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0]
    assert current["current_revision"] == "2"
    assert [item[0].draft.revision for item in s.port.list_publications(limit=5)[0]] == ["2"]
    assert s.port.mutate(s.retirement({"mode": "retire"}, "retire-current", "2")).outcome == (
        "completed"
    )
    current = s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0]
    assert current["current_revision"] is None


def test_retirement_requires_a_published_target(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "draft-key")).error_code
        == "profile_not_published"
    )
    assert (
        s.port.mutate(s.retirement({"mode": "disable", "reason": "x"}, "draft-disable")).error_code
        == "profile_not_published"
    )
    assert (
        s.port.mutate(s.retirement({"mode": "retire"}, "missing-key", "99")).error_code
        == "profile_not_found"
    )
    assert (
        s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0][
            "current_revision"
        ]
        is None
    )


@pytest.mark.parametrize("reason", ["", "x" * 2001])
def test_the_stored_disable_state_requires_a_bounded_reason(sandbox: Sandbox, reason: str) -> None:
    s = sandbox
    s.draft()
    assert s.port.mutate(s.publish()).outcome == "completed"
    # The command boundary rejects these; the publication CHECK is the last line
    # of defense for any other writer of the same table.
    with pytest.raises(sqlite3.IntegrityError):
        s.admin.execute(
            "UPDATE speech_profile_publications SET status='disabled',"
            "retired_at_us=NULL,disabled_reason=%s WHERE revision='1'",
            (reason,),
        )
    assert s.admin.execute("SELECT status FROM speech_profile_publications").fetchall() == [
        {"status": "published"}
    ]
