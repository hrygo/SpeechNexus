"""Publication store decisions proven on synthetic rows; PostgreSQL is qualified separately."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.binding_documents import encode_document
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingLifecycleConflictError,
    BindingLifecycleNotFoundError,
    BindingPublicationCommand,
    BindingPublicationEvidenceError,
    BindingPublicationHeadConflictError,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
)
from tests.support.binding_examples import binding_request

DIGEST = "sha256:" + "a" * 64
NOW = 1_700_000_000_000_003


def stored_definition(capacity_manifest_id: str = "cap-asr-quality-7") -> str:
    body = binding_request()
    body.pop("binding_id")
    body["execution_requirement"] = {}
    body["execution_recipe"] = {
        **body["execution_recipe"],
        "capacity_manifest_id": capacity_manifest_id,
    }
    return encode_document(body).decode("utf-8")


def command(**overrides: Any) -> BindingPublicationCommand:
    fields: dict[str, Any] = {
        "binding_id": "asr-quality",
        "revision": "1",
        "request_id": "publish-request-1",
        "actor_type": "anonymous",
        "credential_label": None,
        "key_digest": DIGEST,
        "intent_digest": DIGEST,
        "expected_current_revision": None,
        "capacity_manifest_id": "cap-asr-quality-7",
        "qualification_report_id": "qrep-asr-quality-1",
    }
    fields.update(overrides)
    return BindingPublicationCommand(**fields)


class Cursor:
    """Dispatch on the statements the publication port is allowed to issue."""

    def __init__(
        self,
        *,
        published: bool = False,
        retired: bool = False,
        draft: bool = True,
        definition: str | None = None,
        head: str | None = None,
        replay: dict[str, Any] | None = None,
    ) -> None:
        self.published, self.retired, self.draft = published, retired, draft
        self.definition = definition or stored_definition()
        self.head = head
        self.replay = replay
        self.rowcount = 0
        self.writes: list[tuple[str, tuple[object, ...]]] = []
        self.statements: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.statements.append(query)
        self.query, self.parameters, self.rowcount = query, parameters, 0
        if "pg_advisory_xact_lock" in query:
            self.rows: list[dict[str, Any]] = []
        elif "FROM speech_publication_lifecycle_events" in query:
            self.rows = [] if self.replay is None else [self.replay]
        elif "FROM speech_publications AS p" in query:
            self.rows = [self._state()] if self.published else []
        elif "FROM speech_binding_revisions" in query:
            self.rows = (
                [
                    {
                        "revision_id": "binding-rev-1",
                        "definition_json": self.definition,
                        "created_at_us": NOW - 3,
                    }
                ]
                if self.draft
                else []
            )
        elif "SELECT revision, revision_id FROM speech_publication_heads" in query:
            self.rows = [] if self.head is None else [{"revision": self.head, "revision_id": "r"}]
        elif "FROM speech_publication_heads WHERE kind = %s" in query:
            self.rows = [] if self.head is None else [{"revision": self.head}]
        elif "SELECT CAST(EXTRACT" in query:
            self.rows = [{"t": NOW}]
        elif "FROM speech_catalog_state WHERE singleton IS TRUE" in query:
            self.rows = [{"revision": 7}]
        elif query.startswith("SELECT kind, object_id, revision, revision_id, published_at_us"):
            self.rows = []
        elif query.startswith("INSERT INTO speech_publications "):
            self.writes.append((query, parameters))
            self.published, self.rowcount = True, 1
        elif query.startswith(
            (
                "INSERT INTO speech_publication_heads",
                "INSERT INTO speech_binding_publication_evidence",
                "INSERT INTO speech_publication_lifecycle_events",
            )
        ):
            self.writes.append((query, parameters))
            self.rowcount = 1
        else:  # pragma: no cover - an unexpected statement is a port regression
            raise AssertionError(f"unexpected publication statement: {query}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def _state(self) -> dict[str, Any]:
        return {
            "kind": "binding",
            "binding_id": "asr-quality",
            "revision": "1",
            "revision_id": "binding-rev-1",
            "published_at_us": NOW,
            "definition_json": self.definition,
            "created_at_us": NOW - 3,
            "capacity_manifest_id": "cap-asr-quality-7",
            "qualification_report_id": "qrep-asr-quality-1",
            "retired_at_us": NOW if self.retired else None,
            "mode": "retire" if self.retired else None,
            "reason": None,
        }


def adapter() -> PostgresBindingLifecycle:
    return PostgresBindingLifecycle(cast(Any, None))


def test_replay_returns_the_original_result_and_never_writes() -> None:
    cursor = Cursor(
        published=True,
        replay={
            "intent_digest": DIGEST,
            "action": "model_binding.publish",
            "binding_id": "asr-quality",
            "revision": "1",
            "revision_id": "binding-rev-1",
            "result_status": "published",
            "retired_at_us": None,
            "disabled_reason": None,
        },
    )
    receipt = adapter()._publish(cast(Any, cursor), command())
    assert receipt.replayed is True
    assert receipt.status == "published"
    assert receipt.revision_id == "binding-rev-1"
    assert cursor.writes == []


def test_a_published_target_is_not_a_draft_for_a_new_key() -> None:
    cursor = Cursor(published=True)
    with pytest.raises(BindingLifecycleConflictError) as rejected:
        adapter()._publish(cast(Any, cursor), command())
    assert rejected.value.code == "binding_not_draft"
    assert cursor.writes == []


def test_a_retired_target_cannot_be_republished() -> None:
    cursor = Cursor(published=True, retired=True)
    with pytest.raises(BindingLifecycleConflictError) as rejected:
        adapter()._publish(cast(Any, cursor), command())
    assert rejected.value.code == "binding_not_draft"
    assert cursor.writes == []


def test_publishing_an_absent_revision_is_not_found() -> None:
    with pytest.raises(BindingLifecycleNotFoundError):
        adapter()._publish(cast(Any, Cursor(draft=False)), command())


def test_the_capacity_manifest_must_describe_the_stored_draft() -> None:
    cursor = Cursor(definition=stored_definition("cap-other"))
    with pytest.raises(BindingPublicationEvidenceError):
        adapter()._publish(cast(Any, cursor), command())
    assert cursor.writes == []


def test_the_observed_head_must_still_be_current() -> None:
    cursor = Cursor(head="2")
    with pytest.raises(BindingPublicationHeadConflictError) as rejected:
        adapter()._publish(cast(Any, cursor), command())
    assert rejected.value.current_revision == "2"
    assert rejected.value.code == "configuration_changed"
    assert cursor.writes == []


def test_an_expected_head_must_match_the_stored_pointer() -> None:
    cursor = Cursor()
    with pytest.raises(BindingPublicationHeadConflictError) as rejected:
        adapter()._publish(cast(Any, cursor), command(expected_current_revision="2"))
    assert rejected.value.current_revision is None
    assert cursor.writes == []


def test_publish_appends_one_publication_evidence_and_journal_row() -> None:
    cursor = Cursor()
    receipt = adapter()._publish(cast(Any, cursor), command())
    assert receipt.replayed is False
    assert receipt.status == "published"
    assert receipt.published_at_us == NOW
    assert [query.split("(")[0] for query, _ in cursor.writes] == [
        "INSERT INTO speech_publications ",
        "INSERT INTO speech_publication_heads ",
        "INSERT INTO speech_binding_publication_evidence ",
        "INSERT INTO speech_publication_lifecycle_events ",
    ]
    publication = cursor.writes[0][1]
    assert publication[:4] == ("binding", "asr-quality", "1", "binding-rev-1")
    evidence = cursor.writes[2][1]
    assert evidence[0] == "binding" and evidence[6] == "publish_http"
    event = cursor.writes[3][1]
    assert event[1] == "model_binding.publish" and event[5] == "published"
    # The catalogue version is read after the append advanced it, so the journal
    # records the revision its own fact produced.
    catalog_read = next(
        index
        for index, statement in enumerate(cursor.statements)
        if "FROM speech_catalog_state WHERE singleton IS TRUE" in statement
    )
    publication_insert = next(
        index
        for index, statement in enumerate(cursor.statements)
        if statement.startswith("INSERT INTO speech_publications ")
    )
    assert publication_insert < catalog_read


def test_the_appended_evidence_keeps_the_observation_identity() -> None:
    cursor = Cursor()
    adapter()._publish(cast(Any, cursor), command())
    _, publication_parameters = cursor.writes[0]
    _, evidence_parameters = cursor.writes[2]
    assert publication_parameters[4] == evidence_parameters[7] == NOW
    assert evidence_parameters[4:6] == ("cap-asr-quality-7", "qrep-asr-quality-1")
    assert evidence_parameters[8:] == (None, None)


def test_the_store_never_rewrites_a_definition() -> None:
    cursor = Cursor()
    adapter()._publish(cast(Any, cursor), command())
    assert not any(
        statement.startswith(("UPDATE speech_binding_revisions", "DELETE"))
        for statement in cursor.statements
    )
    written = copy.deepcopy(cursor.writes)
    assert written == cursor.writes
