"""Future producer boundary; not wired into the operational legacy HTTP surface."""

from datetime import UTC, datetime
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.binding_defaults import parse as parse_snapshot
from speechnexus_contracts.binding_defaults_producer import parse as parse_request

from speechnexus_backend.speech.application.binding_documents import (
    check_command,
    command_defaults,
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.binding_defaults import BindingDefaultsSnapshot
from speechnexus_backend.speech.domain.binding_drafts import CreateBindingDraft
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft


def prepare_binding_defaults_draft(
    request: Any, idempotency_key: str | None = None
) -> CreateBindingDraft:
    try:
        parse_request("BindingDefaultsCreateRequest", request)
        intent = encode_document(request)
        body = decode_document(intent)
        parameters = command_defaults(body)
        body.pop("parameter_defaults", None)
        legacy = prepare_binding_draft(body, idempotency_key)
        command = CreateBindingDraft(
            legacy.binding_id,
            legacy.key_digest,
            intent,
            legacy.definition,
            legacy.document_version,
            parameters,
        )
        check_command(command)
        return command
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid binding default intent") from None


def defaults_to_wire(snapshot: BindingDefaultsSnapshot) -> dict[str, Any]:
    seconds, micros = divmod(snapshot.created_at_us, 1000000)
    created = datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
    result = {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "binding_id": snapshot.binding_id,
        "binding_revision": snapshot.revision,
        "binding_revision_id": snapshot.revision_id,
        "parameters": decode_document(snapshot.parameters),
        "created_at": created.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    parse_snapshot("BindingParameterDefaultsSnapshot", result)
    return result
