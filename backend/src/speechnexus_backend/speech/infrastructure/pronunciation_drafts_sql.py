"""Original command first, one family CAS, immutable draft and result atomically."""

from __future__ import annotations

import uuid
from collections.abc import Callable
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
    revision_number,
)


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


def _result(
    cursor: PronunciationCursor, command: CreatePronunciationDraft, row: dict[str, Any]
) -> PronunciationDraftResult:
    original = row["intent_json"].encode("utf-8")
    source_id, expected, _ = draft_parts(decode_document(original))
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
    return PronunciationDraftResult("replayed", draft)


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
    cursor.execute(
        "INSERT INTO speech_pronunciation_revisions "
        "(pronunciation_id,revision,definition_json,definition_digest,"
        "key_digest,request_digest,created_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            pronunciation_id,
            revision,
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
    _update(
        cursor,
        "UPDATE speech_pronunciation_commands SET command_state='created',pronunciation_id=%s,"
        "revision=%s WHERE key_digest=%s AND command_state='pending'",
        (pronunciation_id, revision, command.key_digest),
    )
    return PronunciationDraftResult("created", draft)
