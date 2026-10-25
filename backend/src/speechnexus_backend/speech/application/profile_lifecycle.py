"""Read-only publication evidence boundary; no writer or enabled authority producer."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.application.profile_documents import decode_document
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft, ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfilePublicationIneligibleError,
    ProfilePublicationObservation,
)


def check_lifecycle_command(command: ProfileLifecycleCommand) -> dict[str, Any]:
    """Check the private canonical envelope and cross-field identity bindings."""
    intent = decode_document(command.intent)
    if (
        set(intent) != {"operation", "profile_id", "revision", "body"}
        or intent["operation"] != command.operation
        or intent["profile_id"] != command.profile_id
        or intent["revision"] != command.revision
        or not isinstance(intent["body"], dict)
    ):
        raise ValueError("Inconsistent profile lifecycle command")
    body = intent["body"]
    expected_keys = {"expected_current_revision"}
    if command.operation == "publish":
        expected_keys |= {"expected_revision", "validation"}
    if set(body) != expected_keys:
        raise ValueError("Inconsistent profile lifecycle body")
    if body["expected_current_revision"] is not None:
        revision_number(body["expected_current_revision"])
    if command.operation == "publish" and (
        body["expected_revision"] != command.revision
        or not isinstance(body["validation"], dict)
        or body["validation"].get("profile_id") != command.profile_id
        or body["validation"].get("revision") != command.revision
    ):
        raise ValueError("Profile validation belongs to another target")
    return body


class ProfilePublicationVerifier(Protocol):
    def verify(
        self, command: ProfileLifecycleCommand, target: ProfileDraft
    ) -> ProfilePublicationObservation:
        """Resolve trusted current facts for the exact target/definition/intent.

        The implementation must check full source/producer semantics, capability
        composition and applicable tier/quality evidence. Caller validation or
        DeploymentQualificationReport alone is not Profile-specific authority.
        Explicit ineligibility raises ProfilePublicationIneligibleError; unreadable
        facts raise ProfileUnavailableError. No implementation may publish here.
        """
        ...


class UnavailableProfilePublicationVerifier:
    def verify(
        self, command: ProfileLifecycleCommand, target: ProfileDraft
    ) -> ProfilePublicationObservation:
        raise ProfileUnavailableError("Profile publication evidence unavailable")


def observe_publication_evidence(
    command: ProfileLifecycleCommand,
    target: ProfileDraft,
    verifier: ProfilePublicationVerifier,
) -> ProfilePublicationObservation:
    """Obtain a read-only observation, never mutate or make it committable.

    Original-key result lookup and atomic rechecking belong to the later writer.
    Activation requires published lifecycle facts and cannot use this draft port.
    """
    try:
        check_lifecycle_command(command)
        if command.operation != "publish":
            # A draft verifier cannot establish published activation facts.
            raise ProfileUnavailableError
        if (target.profile_id, target.revision) != (command.profile_id, command.revision):
            raise ProfileUnavailableError
        decode_document(target.definition)
        observed = verifier.verify(command, target)
        if not isinstance(observed, ProfilePublicationObservation) or (
            observed.profile_id,
            observed.revision,
            observed.definition_digest,
            observed.intent_digest,
        ) != (
            command.profile_id,
            command.revision,
            document_digest(target.definition),
            command.request_digest,
        ):
            raise ProfileUnavailableError
        return observed
    except ProfilePublicationIneligibleError:
        raise ProfilePublicationIneligibleError("Profile is not eligible for publication") from None
    except Exception:
        # Inability to read/verify never becomes an authoritative rejection.
        raise ProfileUnavailableError("Profile publication evidence unavailable") from None
