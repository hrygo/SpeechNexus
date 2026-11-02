"""Synthetic SQL selection checks; PostgreSQL permissions require integration tests."""

import sqlite3
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.speech_facts_postgres import PostgresSpeechFacts


class SqliteFacts(PostgresSpeechFacts):
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE speech_publications
                (kind TEXT, object_id TEXT, revision TEXT, revision_id TEXT, published_at_us INT);
            CREATE TABLE speech_publication_heads
                (kind TEXT, object_id TEXT, revision TEXT, revision_id TEXT);
            CREATE TABLE speech_publication_retirements
                (kind TEXT, object_id TEXT, revision TEXT, revision_id TEXT);
            CREATE TABLE speech_published_binding_revisions
                (binding_id TEXT, revision TEXT, revision_id TEXT, definition_json TEXT);
            INSERT INTO speech_publications VALUES
                ('binding','model','1','binding-one',1), ('binding','model','2','binding-two',2),
                ('voice','voice','1','voice-one',1), ('voice','voice','2','voice-two',2);
            INSERT INTO speech_published_binding_revisions VALUES
                ('model','1','binding-one','{"version": 1}'),
                ('model','2','binding-two','{"version": 2}');
            INSERT INTO speech_publication_heads VALUES
                ('binding','model','2','binding-two'), ('voice','voice','2','voice-two');
        """)

    def _read(self, query: str, parameters: tuple[object, ...]) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(query.replace("%s", "?"), parameters)]


def test_selected_heads_rollback_clear_retirement_and_exact_history() -> None:
    facts = SqliteFacts()
    assert cast(Any, facts.latest_binding("model")).publication.revision == 2
    assert cast(Any, facts.latest_publication("voice", "voice")).revision == 2
    facts.db.execute("UPDATE speech_publication_heads SET revision='1', revision_id=kind || '-one'")
    assert cast(Any, facts.latest_binding("model")).definition == {"version": 1}
    assert cast(Any, facts.latest_publication("voice", "voice")).revision == 1
    facts.db.execute("UPDATE speech_publication_heads SET revision_id='wrong' WHERE kind='binding'")
    assert facts.latest_binding("model") is None
    facts.db.execute(
        "UPDATE speech_publication_heads SET revision_id='binding-one' WHERE kind='binding'"
    )
    facts.db.execute(
        "INSERT INTO speech_publication_retirements VALUES ('binding','model','1','binding-one')"
    )
    facts.db.execute(
        "INSERT INTO speech_publication_retirements VALUES ('voice','voice','1','voice-one')"
    )
    assert facts.latest_binding("model") is None
    assert facts.latest_publication("voice", "voice") is None
    assert cast(Any, facts.publication("binding", "model", 1)).revision_id == "binding-one"
    assert cast(Any, facts.publication("voice", "voice", 1)).revision_id == "voice-one"
    facts.db.execute("DELETE FROM speech_publication_heads")
    assert facts.latest_binding("model") is None
    assert facts.latest_publication("voice", "voice") is None


def test_retired_voice_remains_readable_but_cannot_start_a_request() -> None:
    facts = SqliteFacts()
    facts.db.execute(
        "INSERT INTO speech_publication_retirements VALUES ('voice','voice','1','voice-one')"
    )
    assert facts.publication("voice", "voice", 1) is not None
    assert facts.voice_for_request("voice", 1) is None
    assert facts.voice_for_request("voice", 2) is not None
    facts.db.execute("DELETE FROM speech_publication_heads WHERE kind='voice'")
    assert facts.voice_for_request("voice") is None
    assert facts.voice_for_request("voice", 2) is not None


@pytest.mark.parametrize("document", [None, "[]", "null", "{", '{"wrong": 1}'])
def test_invalid_binding_definition_fails_closed(document: str | None) -> None:
    facts = SqliteFacts()
    facts.db.execute("UPDATE speech_published_binding_revisions SET definition_json=?", (document,))
    if document == '{"wrong": 1}':
        # Reading is not eligibility verification; it must not invent qualification.
        assert facts.latest_binding("model") is not None
    else:
        with pytest.raises(PublicationUnavailableError):
            facts.latest_binding("model")
