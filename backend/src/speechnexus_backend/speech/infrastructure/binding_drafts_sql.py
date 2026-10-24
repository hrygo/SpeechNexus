"""One original command then one family lock; all new facts commit together."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.application.binding_documents import check_command, decode_document
from speechnexus_backend.speech.domain.binding_drafts import (
    DOCUMENT_VERSION,
    BindingDraft,
    BindingDraftResult,
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.domain.configuration import (
    MAX_REVISION,
    document_digest,
    revision_number,
)


class BindingCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _rows(cursor: BindingCursor, sql: str, args: tuple[object, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, args)
    return cursor.fetchall()


def _version(row: dict[str, Any]) -> None:
    if type(row["document_version"]) is not int or row["document_version"] != DOCUMENT_VERSION:
        raise BindingUnavailableError


def read_draft(cursor: BindingCursor, binding_id: str, revision: str) -> BindingDraft | None:
    rows = _rows(
        cursor,
        "SELECT * FROM speech_binding_revisions WHERE binding_id=%s AND revision=%s",
        (binding_id, revision),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise BindingUnavailableError
    row = rows[0]
    _version(row)
    raw = row["definition_json"].encode("utf-8")
    decode_document(raw)
    if (
        document_digest(raw) != row["definition_digest"]
        or row["binding_id"] != binding_id
        or row["revision"] != revision
    ):
        raise BindingUnavailableError
    commands = _rows(
        cursor, "SELECT * FROM speech_binding_commands WHERE key_digest=%s", (row["key_digest"],)
    )
    if len(commands) != 1:
        raise BindingUnavailableError
    original = commands[0]
    _version(original)
    prepared = CreateBindingDraft(
        binding_id,
        row["key_digest"],
        original["intent_json"].encode("utf-8"),
        raw,
        row["document_version"],
    )
    check_command(prepared)
    if (
        original["binding_id"] != binding_id
        or original["request_digest"] != row["request_digest"]
        or prepared.request_digest != row["request_digest"]
    ):
        raise BindingUnavailableError
    return BindingDraft(
        binding_id, revision, row["revision_id"], raw, row["created_at_us"], row["document_version"]
    )


def _result(
    cursor: BindingCursor, command: CreateBindingDraft, row: dict[str, Any]
) -> BindingDraftResult:
    _version(row)
    original = row["intent_json"].encode("utf-8")
    decoded = decode_document(original)
    if (
        document_digest(original) != row["request_digest"]
        or decoded.get("binding_id") != row["binding_id"]
        or row["key_digest"] != command.key_digest
    ):
        raise BindingUnavailableError
    if original != command.intent:
        return BindingDraftResult("conflict", error_code="idempotency_conflict")
    revisions = _rows(
        cursor, "SELECT * FROM speech_binding_revisions WHERE key_digest=%s", (command.key_digest,)
    )
    if not revisions:
        # Only a bypassed/corrupt journal can commit without its revision. It is
        # query-only unknown, never permission to allocate under the original key.
        return BindingDraftResult("unknown")
    if len(revisions) != 1:
        raise BindingUnavailableError
    revision = revisions[0]
    if (
        revision["binding_id"] != command.binding_id
        or revision["request_digest"] != command.request_digest
    ):
        raise BindingUnavailableError
    draft = read_draft(cursor, revision["binding_id"], revision["revision"])
    if (
        draft is None
        or draft.definition != command.definition
        or draft.revision_id != revision["revision_id"]
    ):
        raise BindingUnavailableError
    return BindingDraftResult("replayed", draft)


def recover_draft(cursor: BindingCursor, command: CreateBindingDraft) -> BindingDraftResult:
    check_command(command)
    rows = _rows(
        cursor, "SELECT * FROM speech_binding_commands WHERE key_digest=%s", (command.key_digest,)
    )
    if len(rows) > 1:
        raise BindingUnavailableError
    return _result(cursor, command, rows[0]) if rows else BindingDraftResult("unknown")


def _update(cursor: BindingCursor, sql: str, args: tuple[object, ...]) -> None:
    cursor.execute(sql, args)
    if cursor.rowcount != 1:
        raise BindingUnavailableError


def create_draft(
    cursor: BindingCursor,
    command: CreateBindingDraft,
    clock: Callable[[], int],
    new_identity: Callable[[], str],
) -> BindingDraftResult:
    check_command(command)
    cursor.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (key_digest) DO NOTHING",
        (
            command.key_digest,
            command.binding_id,
            command.document_version,
            command.intent.decode(),
            command.request_digest,
        ),
    )
    # The unique-key INSERT waits for the original transaction. Journal and
    # result rows are immutable, so no UPDATE privilege/explicit row lock is
    # needed after that wait. Only the family allocation head is mutable.
    inserted = cursor.rowcount == 1
    rows = _rows(
        cursor,
        "SELECT * FROM speech_binding_commands WHERE key_digest=%s",
        (command.key_digest,),
    )
    if len(rows) != 1:
        raise BindingUnavailableError
    if not inserted:
        return _result(cursor, command, rows[0])
    cursor.execute(
        "INSERT INTO speech_binding_families (binding_id) VALUES (%s) "
        "ON CONFLICT (binding_id) DO NOTHING",
        (command.binding_id,),
    )
    families = _rows(
        cursor,
        "SELECT latest_revision FROM speech_binding_families WHERE binding_id=%s FOR UPDATE",
        (command.binding_id,),
    )
    if len(families) != 1:
        raise BindingUnavailableError
    latest = families[0]["latest_revision"]
    number = 0 if latest is None else revision_number(latest)
    if number == MAX_REVISION:
        raise BindingUnavailableError
    draft = BindingDraft(
        command.binding_id,
        str(number + 1),
        new_identity(),
        command.definition,
        clock(),
        command.document_version,
    )
    cursor.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
        "definition_digest,request_digest,created_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            draft.binding_id,
            draft.revision,
            draft.revision_id,
            command.key_digest,
            draft.document_version,
            draft.definition.decode(),
            document_digest(draft.definition),
            command.request_digest,
            draft.created_at_us,
        ),
    )
    _update(
        cursor,
        "UPDATE speech_binding_families SET latest_revision=%s WHERE binding_id=%s",
        (draft.revision, command.binding_id),
    )
    return BindingDraftResult("created", draft)
