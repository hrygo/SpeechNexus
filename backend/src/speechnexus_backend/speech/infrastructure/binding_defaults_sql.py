"""Original intent, parent and explicit snapshot in the same bounded transaction."""

from collections.abc import Callable

from speechnexus_backend.speech.application.binding_documents import (
    check_command,
    command_defaults,
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.binding_defaults import (
    BindingDefaultsDraftResult,
    BindingDefaultsSnapshot,
)
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingUnavailableError,
    CreateBindingDraft,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.infrastructure import binding_drafts_sql as bindings
from speechnexus_backend.speech.infrastructure.binding_drafts_sql import BindingCursor


def _record_digest(snapshot: BindingDefaultsSnapshot, key_digest: str, request_digest: str) -> str:
    return document_digest(
        encode_document(
            {
                "snapshot_id": snapshot.snapshot_id,
                "binding_id": snapshot.binding_id,
                "revision": snapshot.revision,
                "revision_id": snapshot.revision_id,
                "created_at_us": snapshot.created_at_us,
                "document_version": snapshot.document_version,
                "schema_version": snapshot.schema_version,
                "parameters_digest": document_digest(snapshot.parameters),
                "key_digest": key_digest,
                "request_digest": request_digest,
            }
        )
    )


def read_default_snapshot(
    cursor: BindingCursor, binding_id: str, revision: str
) -> BindingDefaultsSnapshot | None:
    draft = bindings.read_draft(cursor, binding_id, revision)
    if draft is None:
        return None
    cursor.execute(
        "SELECT c.* FROM speech_binding_commands c JOIN speech_binding_revisions r "
        "ON c.key_digest=r.key_digest WHERE r.binding_id=%s AND r.revision=%s",
        (binding_id, revision),
    )
    originals = cursor.fetchall()
    if len(originals) != 1 or "defaults_declared" not in originals[0]:
        raise BindingUnavailableError
    original = originals[0]
    parameters = command_defaults(decode_document(original["intent_json"].encode()))
    marker = original["defaults_declared"]
    if (marker is None and parameters is not None) or (
        marker is not None
        and (
            type(marker) not in (bool, int)
            or marker not in (0, 1)
            or bool(marker) != (parameters is not None)
        )
    ):
        raise BindingUnavailableError
    cursor.execute(
        "SELECT * FROM speech_binding_defaults WHERE binding_id=%s AND revision=%s",
        (binding_id, revision),
    )
    rows = cursor.fetchall()
    if parameters is None:
        if rows:
            raise BindingUnavailableError
        return None
    if len(rows) != 1:
        raise BindingUnavailableError
    row = rows[0]
    snapshot = BindingDefaultsSnapshot(
        row["snapshot_id"],
        row["binding_id"],
        row["revision"],
        row["revision_id"],
        row["parameters_json"].encode(),
        row["created_at_us"],
        row["document_version"],
        row["schema_version"],
    )
    if (
        snapshot.binding_id != draft.binding_id
        or snapshot.revision != draft.revision
        or snapshot.revision_id != draft.revision_id
        or snapshot.created_at_us != draft.created_at_us
        or snapshot.document_version != draft.document_version
        or snapshot.parameters != parameters
        or row["parameters_digest"] != document_digest(parameters)
        or row["key_digest"] != original["key_digest"]
        or row["request_digest"] != original["request_digest"]
        or row["record_digest"]
        != _record_digest(snapshot, original["key_digest"], original["request_digest"])
    ):
        raise BindingUnavailableError
    return snapshot


def create_default_draft(
    cursor: BindingCursor,
    command: CreateBindingDraft,
    clock: Callable[[], int],
    new_identity: Callable[[], str],
    new_snapshot_identity: Callable[[], str],
) -> BindingDefaultsDraftResult:
    check_command(command)
    result = bindings.create_draft(cursor, command, clock, new_identity, include_defaults=True)
    if result.outcome == "created" and command.parameter_defaults is not None:
        draft = result.draft
        if draft is None:
            raise BindingUnavailableError
        snapshot = BindingDefaultsSnapshot(
            new_snapshot_identity(),
            draft.binding_id,
            draft.revision,
            draft.revision_id,
            command.parameter_defaults,
            draft.created_at_us,
        )
        cursor.execute(
            "INSERT INTO speech_binding_defaults "
            "(snapshot_id,binding_id,revision,revision_id,key_digest,request_digest,"
            "document_version,schema_version,parameters_json,parameters_digest,"
            "record_digest,created_at_us) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                snapshot.snapshot_id,
                snapshot.binding_id,
                snapshot.revision,
                snapshot.revision_id,
                command.key_digest,
                command.request_digest,
                snapshot.document_version,
                snapshot.schema_version,
                snapshot.parameters.decode(),
                document_digest(snapshot.parameters),
                _record_digest(snapshot, command.key_digest, command.request_digest),
                snapshot.created_at_us,
            ),
        )
        if cursor.rowcount != 1:
            raise BindingUnavailableError
    stored_snapshot = (
        read_default_snapshot(cursor, result.draft.binding_id, result.draft.revision)
        if result.draft is not None
        else None
    )
    if result.draft is not None and (stored_snapshot is not None) != (
        command.parameter_defaults is not None
    ):
        raise BindingUnavailableError
    return BindingDefaultsDraftResult(result, stored_snapshot)


def recover_default_draft(
    cursor: BindingCursor, command: CreateBindingDraft
) -> BindingDefaultsDraftResult:
    check_command(command)
    result = bindings.recover_draft(cursor, command)
    snapshot = (
        read_default_snapshot(cursor, result.draft.binding_id, result.draft.revision)
        if result.draft is not None
        else None
    )
    if result.draft is not None and (snapshot is not None) != (
        command.parameter_defaults is not None
    ):
        raise BindingUnavailableError
    return BindingDefaultsDraftResult(result, snapshot)
