"""Original command first, one family CAS, revision and publication atomically.

``publish: true`` appends ``speech_publications`` and the selected head inside the
same transaction as the immutable revision. A failure anywhere rolls the whole
command back: the deployment can never observe a publication without its
revision, or a published-status revision with no publication ledger row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from speechnexus_backend.speech.application.pronunciation_documents import (
    check_command,
    decode_document,
    draft_parts,
)
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    MAX_REVISION,
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
    PronunciationUnavailableError,
    document_digest,
    revision_id_for,
    revision_number,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication


class PronunciationCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _rows(cursor: PronunciationCursor, sql: str, args: tuple[object, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, args)
    return cursor.fetchall()


def read_draft(
    cursor: PronunciationCursor,
    pronunciation_id: str,
    revision: str,
    *,
    expected_key: str | None = None,
) -> PronunciationDraft | None:
    rows = _rows(
        cursor,
        "SELECT * FROM speech_pronunciation_revisions WHERE pronunciation_id=%s AND revision=%s",
        (pronunciation_id, revision),
    )
    if not rows:
        return None
    row = rows[0]
    if expected_key is not None and row["key_digest"] != expected_key:
        raise PronunciationUnavailableError
    raw = row["definition_json"].encode("utf-8")
    definition = decode_document(raw)
    if (
        len(rows) != 1
        or set(definition) != {"encoding", "entries"}
        or document_digest(raw) != row["definition_digest"]
    ):
        raise PronunciationUnavailableError
    draft_parts({**definition, "publish": False})
    return PronunciationDraft(row["pronunciation_id"], row["revision"], raw, row["created_at_us"])


def read_published_draft(
    cursor: PronunciationCursor, pronunciation_id: str, revision: str
) -> PronunciationDraft | None:
    """Read one fixed revision together with its own publication fact.

    Only the read identity holds SELECT on ``speech_publications``; the draft
    writer does not need it because its command ledger already carries the
    publication intent for a revision it created or replayed.
    """
    draft = read_draft(cursor, pronunciation_id, revision)
    if draft is None:
        return None
    rows = _rows(
        cursor,
        "SELECT 1 AS published FROM speech_publications "
        "WHERE kind='pronunciation' AND object_id=%s AND revision=%s",
        (pronunciation_id, revision),
    )
    if len(rows) > 1:
        raise PronunciationUnavailableError
    return replace(draft, published=bool(rows))


def _result(
    cursor: PronunciationCursor, command: CreatePronunciationDraft, row: dict[str, Any]
) -> PronunciationDraftResult:
    original = row["intent_json"].encode("utf-8")
    source_id, expected, _, _ = draft_parts(decode_document(original))
    if document_digest(original) != row["request_digest"]:
        raise PronunciationUnavailableError
    if original != command.intent:
        return PronunciationDraftResult("conflict", error_code="idempotency_conflict")
    state = row["command_state"]
    if state == "pending":
        return PronunciationDraftResult("unknown")
    if state == "rejected":
        if (
            row["pronunciation_id"] is not None
            or row["revision"] is not None
            or row["error_code"] != "configuration_changed"
        ):
            raise PronunciationUnavailableError
        return PronunciationDraftResult("rejected", error_code="configuration_changed")
    if state != "created" or row["error_code"] is not None:
        raise PronunciationUnavailableError
    if source_id is not None and source_id != row["pronunciation_id"]:
        raise PronunciationUnavailableError
    expected_number = 0 if expected is None else revision_number(expected)
    if revision_number(row["revision"]) != expected_number + 1:
        raise PronunciationUnavailableError
    draft = read_draft(
        cursor, row["pronunciation_id"], row["revision"], expected_key=command.key_digest
    )
    if draft is None or draft.definition != command.definition:
        raise PronunciationUnavailableError
    # Publication is written in the same transaction as the revision, so the
    # stored command's publication intent is the revision's publication state.
    return PronunciationDraftResult("replayed", replace(draft, published=command.publish))


def list_summaries(
    cursor: PronunciationCursor, *, limit: int, after: str | None
) -> list[dict[str, Any]]:
    """One keyset page over family heads, one extra row proving continuation.

    The head revision is joined rather than dereferenced per row: a summary is
    only listable when the allocation head points at an immutable revision whose
    derived encoding and entry count were fixed by migration 0045.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid pronunciation page bound")
    if after is not None:
        identifier(after)
    return _rows(
        cursor,
        "SELECT f.pronunciation_id AS pronunciation_id, "
        "f.latest_revision AS head_revision, r.encoding AS encoding, "
        "r.entry_count AS entry_count, "
        "EXISTS (SELECT 1 FROM speech_publications p "
        "WHERE p.kind = 'pronunciation' AND p.object_id = f.pronunciation_id "
        "AND p.revision = f.latest_revision) AS published "
        "FROM speech_pronunciation_families f "
        "JOIN speech_pronunciation_revisions r "
        "ON r.pronunciation_id = f.pronunciation_id AND r.revision = f.latest_revision "
        "WHERE f.pronunciation_id > %s ORDER BY f.pronunciation_id LIMIT %s",
        (after or "", limit + 1),
    )


def recover_draft(
    cursor: PronunciationCursor, command: CreatePronunciationDraft
) -> PronunciationDraftResult:
    check_command(command)
    rows = _rows(
        cursor,
        "SELECT * FROM speech_pronunciation_commands WHERE key_digest=%s",
        (command.key_digest,),
    )
    return _result(cursor, command, rows[0]) if rows else PronunciationDraftResult("unknown")


def _update(cursor: PronunciationCursor, sql: str, args: tuple[object, ...]) -> None:
    cursor.execute(sql, args)
    if cursor.rowcount != 1:
        raise PronunciationUnavailableError


def new_pronunciation_id() -> str:
    return "pron-" + uuid.uuid4().hex


def create_draft(
    cursor: PronunciationCursor,
    command: CreatePronunciationDraft,
    clock: Callable[[], int],
    allocate_id: Callable[[], str] = new_pronunciation_id,
) -> PronunciationDraftResult:
    check_command(command)
    cursor.execute(
        "INSERT INTO speech_pronunciation_commands "
        "(key_digest,intent_json,request_digest,command_state) "
        "VALUES (%s,%s,%s,'pending') ON CONFLICT (key_digest) DO NOTHING",
        (command.key_digest, command.intent.decode(), command.request_digest),
    )
    inserted = cursor.rowcount == 1
    rows = _rows(
        cursor,
        "SELECT * FROM speech_pronunciation_commands WHERE key_digest=%s FOR UPDATE",
        (command.key_digest,),
    )
    if len(rows) != 1:
        raise PronunciationUnavailableError
    if not inserted:
        return _result(cursor, command, rows[0])
    pronunciation_id = command.pronunciation_id or allocate_id()
    identifier(pronunciation_id)
    cursor.execute(
        "INSERT INTO speech_pronunciation_families (pronunciation_id) VALUES (%s) "
        "ON CONFLICT (pronunciation_id) DO NOTHING",
        (pronunciation_id,),
    )
    families = _rows(
        cursor,
        "SELECT latest_revision FROM speech_pronunciation_families "
        "WHERE pronunciation_id=%s FOR UPDATE",
        (pronunciation_id,),
    )
    if len(families) != 1:
        raise PronunciationUnavailableError
    latest = families[0]["latest_revision"]
    if latest is not None:
        revision_number(latest)
    if command.pronunciation_id is None and latest is not None:
        # An allocator collision never turns a new-family request into an append.
        raise PronunciationUnavailableError
    if latest != command.expected_revision:
        _update(
            cursor,
            "UPDATE speech_pronunciation_commands SET command_state='rejected',"
            "error_code='configuration_changed' WHERE key_digest=%s AND command_state='pending'",
            (command.key_digest,),
        )
        return PronunciationDraftResult("rejected", error_code="configuration_changed")
    if latest is not None and read_draft(cursor, pronunciation_id, latest) is None:
        raise PronunciationUnavailableError
    number = 0 if latest is None else revision_number(latest)
    if number == MAX_REVISION:
        raise PronunciationUnavailableError
    revision = str(number + 1)
    draft = PronunciationDraft(pronunciation_id, revision, command.definition, clock())
    document = decode_document(command.definition)
    # Write the derived summary in the same immutable row: the list never parses
    # definition_json, and migration 0045 makes these columns NOT NULL.
    cursor.execute(
        "INSERT INTO speech_pronunciation_revisions "
        "(pronunciation_id,revision,encoding,entry_count,definition_json,"
        "definition_digest,key_digest,request_digest,created_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            pronunciation_id,
            revision,
            document["encoding"],
            len(document["entries"]),
            command.definition.decode(),
            document_digest(command.definition),
            command.key_digest,
            command.request_digest,
            draft.created_at_us,
        ),
    )
    suffix, args = (
        ("latest_revision IS NULL", ()) if latest is None else ("latest_revision=%s", (latest,))
    )
    _update(
        cursor,
        "UPDATE speech_pronunciation_families SET latest_revision=%s WHERE pronunciation_id=%s AND "
        + suffix,
        (revision, pronunciation_id, *args),
    )
    if command.publish:
        # The publication ledger row, the selected head and the catalogue version
        # (advanced by the insert trigger) commit or roll back with the revision.
        published_at_us = clock()
        publication = Publication(
            kind="pronunciation",
            object_id=pronunciation_id,
            revision=number + 1,
            revision_id=revision_id_for(pronunciation_id, revision),
            published_at_us=published_at_us,
        )
        if not append_publication(cursor, publication):
            raise PronunciationUnavailableError
        draft = replace(draft, published=True)
    _update(
        cursor,
        "UPDATE speech_pronunciation_commands SET command_state='created',pronunciation_id=%s,"
        "revision=%s WHERE key_digest=%s AND command_state='pending'",
        (pronunciation_id, revision, command.key_digest),
    )
    return PronunciationDraftResult("created", draft)
