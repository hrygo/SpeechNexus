"""Intent and read-only provider boundary; synthetic providers prove no qualification."""

import copy
import json
import traceback
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.profile_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.application.profile_lifecycle import (
    UnavailableProfilePublicationVerifier,
    check_lifecycle_command,
    observe_publication_evidence,
)
from speechnexus_backend.speech.domain.configuration import document_digest
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft, ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfilePublicationIneligibleError,
    ProfilePublicationObservation,
)
from speechnexus_backend.speech.interfaces.profile_lifecycle import (
    prepare_profile_activation,
    prepare_profile_publication,
)
from tests.support.profile_examples import profile_request

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/profile-lifecycle-http.json").read_text())[
    "examples"
]


def request(prefix: str = "publish") -> dict[str, Any]:
    return copy.deepcopy(next(e["value"] for e in EXAMPLES if e["id"] == prefix + "-original"))


def prepare(
    value: dict[str, Any] | None = None, key: Any = "original-profile-key"
) -> ProfileLifecycleCommand:
    value = request() if value is None else value
    return prepare_profile_publication(
        value["path"]["profile_id"], value["path"]["revision"], value["body"], key
    )


def target(command: ProfileLifecycleCommand) -> ProfileDraft:
    definition = profile_request()
    definition.pop("profile_id")
    definition.pop("expected_latest_revision")
    return ProfileDraft(command.profile_id, command.revision, encode_document(definition), 0)


def observation(
    command: ProfileLifecycleCommand, draft: ProfileDraft
) -> ProfilePublicationObservation:
    return ProfilePublicationObservation(
        command.profile_id,
        command.revision,
        document_digest(draft.definition),
        command.request_digest,
        "sha256:" + "a" * 64,
    )


class RecordingVerifier:
    """Test double, never installed in a product composition or writer."""

    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[ProfileLifecycleCommand, ProfileDraft]] = []
        self.result = result

    def verify(
        self, command: ProfileLifecycleCommand, draft: ProfileDraft
    ) -> ProfilePublicationObservation:
        self.calls.append((command, draft))
        if isinstance(self.result, Exception):
            raise self.result
        # Deliberately malformed return values exercise the runtime boundary.
        return cast(ProfilePublicationObservation, self.result)


def test_complete_target_body_and_key_are_fixed_without_mutating_or_logging_the_input() -> None:
    value = request()
    value["body"]["validation"]["notes"] = "SYNTHETIC_PRIVATE_NOTES"
    before = copy.deepcopy(value)
    first = prepare(value)
    reordered = dict(reversed(list(value["body"].items())))
    again = prepare_profile_publication(
        value["path"]["profile_id"], value["path"]["revision"], reordered, "original-profile-key"
    )
    assert first == again
    assert value == before
    assert check_lifecycle_command(first) == value["body"]
    assert decode_document(first.intent) == {
        "operation": "publish",
        **value["path"],
        "body": value["body"],
    }
    assert "original-profile-key" not in repr(first)
    assert "SYNTHETIC_PRIVATE_NOTES" not in repr(first)
    value["body"]["validation"]["notes"] = "changed after preparation"
    assert check_lifecycle_command(first)["validation"]["notes"] == "SYNTHETIC_PRIVATE_NOTES"
    assert before["body"]["expected_current_revision"] is None
    with pytest.raises(FrozenInstanceError):
        first.revision = "2"  # type: ignore[misc]


@pytest.mark.parametrize("change", ["current", "evidence", "notes", "target"])
def test_the_same_key_does_not_erase_differences_in_complete_intent(change: str) -> None:
    original = prepare()
    value = request()
    if change == "current":
        value["body"]["expected_current_revision"] = "2"
    elif change == "evidence":
        value["body"]["validation"]["evidence_digest"] = "sha256:" + "b" * 64
    elif change == "notes":
        value["body"]["validation"]["notes"] = "explicit synthetic note"
    else:
        value["path"]["profile_id"] = "other-profile"
        value["body"]["validation"]["profile_id"] = "other-profile"
    changed = prepare(value)
    assert changed.key_digest == original.key_digest
    assert changed.request_digest != original.request_digest


def test_activation_preserves_explicit_null_and_has_an_independent_intent_kind() -> None:
    value = request("activate")
    command = prepare_profile_activation(
        **value["path"], request=value["body"], idempotency_key="original-profile-key"
    )
    assert command.operation == "activate"
    assert check_lifecycle_command(command) == {"expected_current_revision": None}
    assert command.request_digest != prepare().request_digest
    assert command.key_digest == prepare().key_digest


def test_full_revision_range_is_preserved_in_paths_cas_and_validation() -> None:
    value = request()
    maximum = "9999999999999999999"
    value["path"]["revision"] = maximum
    value["body"]["expected_revision"] = maximum
    value["body"]["expected_current_revision"] = maximum
    value["body"]["validation"]["revision"] = maximum
    command = prepare(value)
    assert command.revision == maximum
    assert check_lifecycle_command(command) == value["body"]


def test_keyless_preparation_does_not_promise_a_replayable_new_request() -> None:
    first, second = prepare(key=None), prepare(key=None)
    assert first.request_digest == second.request_digest
    assert first.key_digest != second.key_digest


@pytest.mark.parametrize("key", [True, 1, "", "short", "x" * 129])
def test_source_invalid_key_is_rejected_without_exposing_it(key: Any) -> None:
    with pytest.raises(ContractValidationError, match=r"^Invalid profile lifecycle intent$"):
        prepare(key=key)


@pytest.mark.parametrize("field", ["profile_id", "revision", "expected_revision"])
def test_publication_cannot_apply_validation_to_another_fixed_target(field: str) -> None:
    value = request()
    if field == "expected_revision":
        value["body"][field] = "2"
    else:
        value["body"]["validation"][field] = "other-profile" if field == "profile_id" else "2"
    with pytest.raises(ContractValidationError):
        prepare(value)


@pytest.mark.parametrize("revision", [True, 1, "0", "latest", "10000000000000000000"])
def test_revision_never_narrows_or_resolves_a_current_alias(revision: Any) -> None:
    value = request()
    value["path"]["revision"] = revision
    with pytest.raises(ContractValidationError):
        prepare(value)


@pytest.mark.parametrize("change", ["operation", "profile_id", "revision", "body", "canonical"])
def test_private_command_tampering_is_rejected(change: str) -> None:
    command = prepare()
    intent = decode_document(command.intent)
    if change == "canonical":
        raw = json.dumps(intent).encode()
    else:
        if change == "body":
            intent["body"]["validation"]["revision"] = "2"
        else:
            intent[change] = "activate" if change == "operation" else "other-profile"
        raw = encode_document(intent)
    altered = replace(command, intent=raw)
    with pytest.raises(ValueError):
        check_lifecycle_command(altered)


def test_a_source_valid_caller_report_does_not_create_authority() -> None:
    command = prepare()
    with pytest.raises(
        ProfileUnavailableError, match=r"^Profile publication evidence unavailable$"
    ):
        observe_publication_evidence(
            command, target(command), UnavailableProfilePublicationVerifier()
        )


@pytest.mark.parametrize("result", [None, True, False, {}, "approved"])
def test_empty_or_truthy_provider_results_never_become_an_observation(result: Any) -> None:
    command = prepare()
    verifier = RecordingVerifier(result)
    with pytest.raises(ProfileUnavailableError):
        observe_publication_evidence(command, target(command), verifier)
    assert len(verifier.calls) == 1


@pytest.mark.parametrize("field", ["profile_id", "revision", "definition_digest", "intent_digest"])
def test_provider_observation_is_bound_to_the_target_definition_and_complete_intent(
    field: str,
) -> None:
    command = prepare()
    draft = target(command)
    value = (
        "other-profile"
        if field == "profile_id"
        else "2"
        if field == "revision"
        else "sha256:" + "b" * 64
    )
    verifier = RecordingVerifier(replace(observation(command, draft), **{field: value}))
    with pytest.raises(ProfileUnavailableError):
        observe_publication_evidence(command, draft, verifier)


def test_explicit_denial_and_unreadable_facts_are_distinct_and_have_no_private_error_text() -> None:
    command = prepare()
    for error in (
        ProfilePublicationIneligibleError("SYNTHETIC_PRIVATE"),
        RuntimeError("SYNTHETIC_PRIVATE"),
        ProfileUnavailableError("SYNTHETIC_PRIVATE"),
    ):
        verifier = RecordingVerifier(error)
        expected = (
            ProfilePublicationIneligibleError
            if isinstance(error, ProfilePublicationIneligibleError)
            else ProfileUnavailableError
        )
        with pytest.raises(expected) as raised:
            observe_publication_evidence(command, target(command), verifier)
        assert "SYNTHETIC_PRIVATE" not in "".join(traceback.format_exception(raised.value))
        assert len(verifier.calls) == 1


def test_wrong_target_corrupt_document_and_activation_never_query_a_draft_verifier() -> None:
    command = prepare()
    draft = target(command)
    verifier = RecordingVerifier(observation(command, draft))
    for wrong in (
        replace(draft, profile_id="other-profile"),
        replace(draft, definition=b"not-json"),
    ):
        with pytest.raises(ProfileUnavailableError):
            observe_publication_evidence(command, wrong, verifier)
    activation = prepare_profile_activation(
        command.profile_id, command.revision, {"expected_current_revision": None}
    )
    with pytest.raises(ProfileUnavailableError):
        observe_publication_evidence(activation, draft, verifier)
    assert not verifier.calls


def test_observation_never_publishes_or_freezes_future_eligibility() -> None:
    command = prepare()
    draft = target(command)
    expected = observation(command, draft)
    verifier = RecordingVerifier(expected)
    assert observe_publication_evidence(command, draft, verifier) is expected
    assert verifier.calls == [(command, draft)]
    assert target(command) == draft and prepare() == command
    # A later unavailable authority is still unavailable; the earlier snapshot
    # is not a commit permission, stored result or independent quality evidence.
    with pytest.raises(ProfileUnavailableError):
        observe_publication_evidence(command, draft, UnavailableProfilePublicationVerifier())
