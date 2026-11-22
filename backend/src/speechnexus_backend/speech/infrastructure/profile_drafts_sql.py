"""Explicit profile statements; one key, then one family, in a short transaction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.application.profile_documents import check_command, decode_document
from speechnexus_backend.speech.domain.profile_drafts import (
    MAX_REVISION,
    CreateProfileDraft,
    ProfileDraft,
    ProfileDraftResult,
    ProfileUnavailableError,
    document_digest,
    revision_number,
)
from speechnexus_portable_sql.contract import insert_if_absent


class ProfileCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _rows(cursor: ProfileCursor, sql: str, args: tuple[object, ...]) -> list[dict[str, Any]]:
    cursor.execute(sql, args)
    return cursor.fetchall()


def read_draft(cursor: ProfileCursor, profile_id: str, revision: str) -> ProfileDraft | None:
    rows = _rows(
        cursor,
        "SELECT * FROM speech_profile_revisions WHERE profile_id=? AND revision=?",
        (profile_id, revision),
    )
    if not rows:
        return None
    row = rows[0]
    raw = row["definition_json"].encode("utf-8")
    decode_document(raw)
    if len(rows) != 1 or document_digest(raw) != row["definition_digest"]:
        raise ProfileUnavailableError
    return ProfileDraft(row["profile_id"], row["revision"], raw, row["created_at_us"])


def _result(
    cursor: ProfileCursor, command: CreateProfileDraft, row: dict[str, Any]
) -> ProfileDraftResult:
    original = row["intent_json"].encode("utf-8")
    decoded = decode_document(original)
    if (
        document_digest(original) != row["request_digest"]
        or decoded.get("profile_id") != row["profile_id"]
    ):
        raise ProfileUnavailableError
    if original != command.intent:
        return ProfileDraftResult("conflict", error_code="idempotency_conflict")
    if row["command_state"] == "pending":
        return ProfileDraftResult("unknown")
    if row["command_state"] == "rejected":
        if row["revision"] is not None or row["error_code"] != "configuration_changed":
            raise ProfileUnavailableError
        return ProfileDraftResult("rejected", error_code="configuration_changed")
    if row["command_state"] != "created" or row["error_code"] is not None:
        raise ProfileUnavailableError
    draft = read_draft(cursor, row["profile_id"], row["revision"])
    if draft is None or draft.definition != command.definition:
        raise ProfileUnavailableError
    return ProfileDraftResult("replayed", draft)


def recover_draft(cursor: ProfileCursor, command: CreateProfileDraft) -> ProfileDraftResult:
    check_command(command)
    rows = _rows(
        cursor, "SELECT * FROM speech_profile_commands WHERE key_digest=?", (command.key_digest,)
    )
    return _result(cursor, command, rows[0]) if rows else ProfileDraftResult("unknown")


def _update(cursor: ProfileCursor, sql: str, args: tuple[object, ...]) -> None:
    cursor.execute(sql, args)
    if cursor.rowcount != 1:
        raise ProfileUnavailableError


def create_draft(
    cursor: ProfileCursor, command: CreateProfileDraft, clock: Callable[[], int]
) -> ProfileDraftResult:
    check_command(command)
    # 幂等创建:同 key 的第二个写者不插入。竞争下较晚的一方会撞唯一索引,由
    # `insert_if_absent` 读成「未插入」——与 `ON CONFLICT DO NOTHING` 的语义一致。
    inserted = insert_if_absent(
        cursor,
        "INSERT INTO speech_profile_commands "
        "(key_digest,profile_id,intent_json,request_digest,command_state) "
        "SELECT ?,?,?,?,'pending' WHERE NOT EXISTS "
        "(SELECT 1 FROM speech_profile_commands WHERE key_digest = ?)",
        (
            command.key_digest,
            command.profile_id,
            command.intent.decode(),
            command.request_digest,
            command.key_digest,
        ),
    )
    rows = _rows(
        cursor,
        "SELECT * FROM speech_profile_commands WHERE key_digest=? FOR UPDATE",
        (command.key_digest,),
    )
    if len(rows) != 1:
        raise ProfileUnavailableError
    if not inserted:
        return _result(cursor, command, rows[0])
    # A null allocation head is only an internal lock anchor, never a revision
    # or the default/current published profile pointer.
    # 命令行已经选出唯一赢家,只有赢家走到这里;`WHERE NOT EXISTS` 替代 `ON CONFLICT`。
    cursor.execute(
        "INSERT INTO speech_profile_families (profile_id) "
        "SELECT ? WHERE NOT EXISTS ("
        "SELECT 1 FROM speech_profile_families f WHERE f.profile_id = ?)",
        (command.profile_id, command.profile_id),
    )
    families = _rows(
        cursor,
        "SELECT latest_revision FROM speech_profile_families WHERE profile_id=? FOR UPDATE",
        (command.profile_id,),
    )
    if len(families) != 1:
        raise ProfileUnavailableError
    latest = families[0]["latest_revision"]
    if latest is not None:
        revision_number(latest)
    if latest != command.expected_latest_revision:
        _update(
            cursor,
            "UPDATE speech_profile_commands SET command_state='rejected', "
            "error_code='configuration_changed' WHERE key_digest=? AND command_state='pending'",
            (command.key_digest,),
        )
        return ProfileDraftResult("rejected", error_code="configuration_changed")
    number = 0 if latest is None else revision_number(latest)
    if number == MAX_REVISION:
        raise ProfileUnavailableError
    revision, observed_at = str(number + 1), clock()
    draft = ProfileDraft(command.profile_id, revision, command.definition, observed_at)
    cursor.execute(
        "INSERT INTO speech_profile_revisions "
        "(profile_id,revision,definition_json,definition_digest,request_digest,created_at_us) "
        "VALUES (?,?,?,?,?,?)",
        (
            draft.profile_id,
            draft.revision,
            draft.definition.decode(),
            document_digest(draft.definition),
            command.request_digest,
            observed_at,
        ),
    )
    _update(
        cursor,
        "UPDATE speech_profile_families SET latest_revision=? WHERE profile_id=?",
        (revision, command.profile_id),
    )
    _update(
        cursor,
        "UPDATE speech_profile_commands SET command_state='created',revision=? "
        "WHERE key_digest=? AND command_state='pending'",
        (revision, command.key_digest),
    )
    return ProfileDraftResult("created", draft)
