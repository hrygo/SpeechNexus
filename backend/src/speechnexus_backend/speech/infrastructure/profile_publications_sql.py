"""One key then one family lock; replay precedes evidence and current-head CAS."""

from collections.abc import Callable
from typing import Any

from speechnexus_backend.speech.application.profile_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.application.profile_lifecycle import (
    ProfilePublicationVerifier,
    check_lifecycle_command,
    observe_publication_evidence,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft, ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfilePublicationIneligibleError,
)
from speechnexus_backend.speech.domain.profile_publications import (
    ProfileLifecycleResult,
    PublishedProfile,
)
from speechnexus_backend.speech.infrastructure.profile_drafts_sql import (
    ProfileCursor,
    _rows,
    _update,
    read_draft,
)
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intents_sql import record_intent

ERRORS = {
    "configuration_changed",
    "profile_not_found",
    "profile_not_published",
    "profile_not_eligible",
    "profile_already_published",
}


def _publication(row: dict[str, Any], draft: ProfileDraft) -> PublishedProfile:
    validation = row["validation_json"].encode()
    decode_document(validation)
    if document_digest(validation) != row["validation_digest"]:
        raise ProfileUnavailableError
    return PublishedProfile(
        draft,
        validation,
        row["published_at_us"],
        row["status"],
        row["retired_at_us"],
        row["disabled_reason"],
    )


def read_publication(
    cursor: ProfileCursor, profile_id: str, revision: str
) -> PublishedProfile | None:
    rows = _rows(
        cursor,
        "SELECT * FROM speech_profile_publications WHERE profile_id=%s AND revision=%s",
        (profile_id, revision),
    )
    if not rows:
        return None
    draft = read_draft(cursor, profile_id, revision)
    if draft is None:
        raise ProfileUnavailableError
    return _publication(rows[0], draft)


def _snapshot(publication: PublishedProfile) -> bytes:
    # Private immutable result stores the complete original facts, not a pointer.
    p = publication
    return encode_document(
        {
            "profile_id": p.draft.profile_id,
            "revision": p.draft.revision,
            "definition": decode_document(p.draft.definition),
            "created_at_us": p.draft.created_at_us,
            "validation": decode_document(p.validation),
            "published_at_us": p.published_at_us,
        }
    )


def _original(cursor: ProfileCursor, command: ProfileLifecycleCommand) -> ProfileLifecycleResult:
    rows = _rows(
        cursor,
        "SELECT * FROM speech_profile_lifecycle_results WHERE key_digest=%s",
        (command.key_digest,),
    )
    if not rows:
        return ProfileLifecycleResult("unknown")
    row = rows[0]
    if row["error_code"] is not None:
        if row["error_code"] not in ERRORS or row["result_json"] is not None:
            raise ProfileUnavailableError
        return ProfileLifecycleResult("rejected", error_code=row["error_code"])
    raw = row["result_json"].encode()
    value = decode_document(raw)
    if document_digest(raw) != row["result_digest"] or (value["profile_id"], value["revision"]) != (
        command.profile_id,
        command.revision,
    ):
        raise ProfileUnavailableError
    draft = ProfileDraft(
        value["profile_id"],
        value["revision"],
        encode_document(value["definition"]),
        value["created_at_us"],
    )
    return ProfileLifecycleResult(
        "replayed",
        PublishedProfile(draft, encode_document(value["validation"]), value["published_at_us"]),
    )


def mutate_profile(
    cursor: ProfileCursor,
    command: ProfileLifecycleCommand,
    verifier: ProfilePublicationVerifier,
    clock: Callable[[], int],
) -> ProfileLifecycleResult:
    body = check_lifecycle_command(command)
    recorded = record_intent(cursor, command, clock)
    if recorded.outcome == "conflict":
        return ProfileLifecycleResult("conflict", error_code="idempotency_conflict")
    _rows(
        cursor,
        "SELECT key_digest FROM speech_profile_lifecycle_intents WHERE key_digest=%s FOR UPDATE",
        (command.key_digest,),
    )
    original = _original(cursor, command)
    if original.outcome != "unknown":
        return original
    # The intent journal may predate this writer. Its immutable identity is reused.
    families = _rows(
        cursor,
        "SELECT current_revision FROM speech_profile_families WHERE profile_id=%s FOR UPDATE",
        (command.profile_id,),
    )

    def reject(code: str) -> ProfileLifecycleResult:
        cursor.execute(
            "INSERT INTO speech_profile_lifecycle_results (key_digest,error_code) VALUES (%s,%s)",
            (command.key_digest, code),
        )
        return ProfileLifecycleResult("rejected", error_code=code)

    draft = read_draft(cursor, command.profile_id, command.revision)
    if not families or draft is None:
        return reject("profile_not_found")
    current = families[0]["current_revision"]
    if current != body["expected_current_revision"]:
        return reject("configuration_changed")
    publication = read_publication(cursor, command.profile_id, command.revision)
    if command.operation == "publish":
        if publication is not None:
            return reject("profile_already_published")
        try:
            # Bounded local/read-only authority only. No HTTP, inference or object writes.
            observation = observe_publication_evidence(command, draft, verifier)
        except ProfilePublicationIneligibleError:
            return reject("profile_not_eligible")
        validation, now = encode_document(body["validation"]), clock()
        cursor.execute(
            "INSERT INTO speech_profile_publications "
            "(profile_id,revision,validation_json,validation_digest,authority_digest,"
            "published_at_us,status) "
            "VALUES (%s,%s,%s,%s,%s,%s,'published')",
            (
                command.profile_id,
                command.revision,
                validation.decode(),
                document_digest(validation),
                observation.authority_digest,
                now,
            ),
        )
        publication = PublishedProfile(draft, validation, now)
    elif publication is None or publication.status != "published":
        return reject("profile_not_published")
    _update(
        cursor,
        "UPDATE speech_profile_families SET current_revision=%s WHERE profile_id=%s",
        (command.revision, command.profile_id),
    )
    assert publication is not None
    snapshot = _snapshot(publication)
    cursor.execute(
        "INSERT INTO speech_profile_lifecycle_results (key_digest,result_json,result_digest) "
        "VALUES (%s,%s,%s)",
        (command.key_digest, snapshot.decode(), document_digest(snapshot)),
    )
    return ProfileLifecycleResult("completed", publication)


def list_publications(
    cursor: ProfileCursor, *, limit: int, after: tuple[str, str] | None = None
) -> tuple[list[tuple[PublishedProfile, str | None]], bool]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid profile page size")
    where = ""
    args: tuple[object, ...] = ()
    if after is not None:
        from speechnexus_backend.speech.domain.assets import identifier
        from speechnexus_backend.speech.domain.configuration import revision_number

        identity, revision = after
        identifier(identity)
        revision_number(revision)
        where = (
            " AND (p.profile_id > %s OR (p.profile_id=%s AND "
            "(length(p.revision)>%s OR (length(p.revision)=%s AND p.revision>%s))))"
        )
        args = (identity, identity, len(revision), len(revision), revision)
    rows = _rows(
        cursor,
        "SELECT p.*,r.definition_json,r.definition_digest,r.created_at_us, "
        "active.revision AS current_revision "
        "FROM speech_profile_publications p JOIN speech_profile_revisions r "
        "ON r.profile_id=p.profile_id AND r.revision=p.revision "
        "JOIN speech_profile_families f ON f.profile_id=p.profile_id "
        "LEFT JOIN speech_profile_publications active ON active.profile_id=f.profile_id "
        "AND active.revision=f.current_revision AND active.status='published' "
        "WHERE p.status='published'"
        + where
        + " ORDER BY p.profile_id,length(p.revision),p.revision LIMIT "
        + str(limit + 1),
        args,
    )
    result = []
    for row in rows[:limit]:
        raw = row["definition_json"].encode()
        if document_digest(raw) != row["definition_digest"]:
            raise ProfileUnavailableError
        decode_document(raw)
        draft = ProfileDraft(row["profile_id"], row["revision"], raw, row["created_at_us"])
        result.append((_publication(row, draft), row["current_revision"]))
    return result, len(rows) > limit
