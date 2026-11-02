"""Deterministic application-layer replay windows; no database concurrency is implied."""

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    IdempotencyConflictError,
    JobOutcomeUnknownError,
    ModelBindingError,
    ModelNotPublishedError,
    PublicJobs,
    UnknownWorkspaceError,
    VoiceNotPublishedError,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJob, PublicJobUnavailableError
from tests.unit.backend.test_public_jobs import (
    BINDING,
    NOW,
    Facts,
    Store,
    _body,
    _pin_facts,
    _pinned_body,
)

KEY = "idem-race-0001"


class InterleavedStore(Store):
    """Run A after B's first lookup has observed absence, before it returns."""

    after_empty: Callable[[], None] | None = None

    def find_by_key(self, workspace_id: str, key_digest: str, request_digest: str) -> Any:
        result = super().find_by_key(workspace_id, key_digest, request_digest)
        callback = self.after_empty
        if result is None and callback is not None:
            self.after_empty = None
            callback()
        return result


@pytest.mark.parametrize("change", ["retired", "new_version"])
def test_same_intent_returns_original_after_binding_changes(change: str) -> None:
    facts, store = Facts(), InterleavedStore()
    _pin_facts(facts)
    service = PublicJobs(facts, store, now_us=lambda: NOW)
    command = _pinned_body()
    committed: list[PublicJob] = []

    def commit_then_change() -> None:
        committed.append(service.submit(command, request_id="a", idempotency_key=KEY))
        facts.binding = (
            replace(facts.binding, retired=True)
            if change == "retired"
            else replace(facts.binding, current=False)
        )

    store.after_empty = commit_then_change
    replay = service.submit(command, request_id="b", idempotency_key=KEY)
    assert replay.job_id == committed[0].job_id
    assert replay.submission.effective_configuration_json == (
        committed[0].submission.effective_configuration_json
    )
    assert json.loads(replay.submission.effective_configuration_json)["models"]["tts"] == (
        BINDING.reference
    )
    assert len(store.jobs) == 1


@pytest.mark.parametrize("change", ["pin", "remove_pin", "text"])
def test_changed_intent_is_not_masked_by_early_rejection(change: str) -> None:
    facts, store = Facts(), InterleavedStore()
    _pin_facts(facts)
    service = PublicJobs(facts, store)
    command = _pinned_body()
    changed = {**command}
    if change == "pin":
        changed["model_binding"] = {**BINDING.reference, "revision": "3"}
    elif change == "remove_pin":
        del changed["model_binding"]
    else:
        changed["input"] = {"text": "different synthetic intent"}

    def commit_then_retire() -> None:
        service.submit(command, request_id="a", idempotency_key=KEY)
        facts.binding = replace(facts.binding, retired=True)

    store.after_empty = commit_then_retire
    with pytest.raises(IdempotencyConflictError):
        service.submit(changed, request_id="b", idempotency_key=KEY)
    assert len(store.jobs) == 1 and len(store.requests) == 1


@pytest.mark.parametrize(
    ("fact", "error"),
    [
        ("workspace", UnknownWorkspaceError),
        ("latest_binding", ModelNotPublishedError),
        ("publication", VoiceNotPublishedError),
        ("asset", AssetNotReadyError),
    ],
)
def test_rejection_is_preserved_when_no_same_scope_result_exists(
    fact: str, error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, store = Facts(), InterleavedStore()
    service = PublicJobs(facts, store)
    command = _body()
    if fact == "asset":
        command["input"] = {"text_asset_id": "asset-synthetic"}
    monkeypatch.setattr(facts, fact, lambda *args: None)
    with pytest.raises(error):
        service.submit(command, request_id="b", idempotency_key=KEY)
    assert not store.jobs and not store.requests


def test_other_workspace_cannot_replay_same_key() -> None:
    facts, store = Facts(), InterleavedStore()
    service = PublicJobs(facts, store)
    command = _body()

    def commit_original() -> None:
        service.submit(command, request_id="a", idempotency_key=KEY)

    store.after_empty = commit_original
    with pytest.raises(UnknownWorkspaceError):
        service.submit(
            {**command, "workspace_id": "workspace-other"}, request_id="b", idempotency_key=KEY
        )
    assert len(store.jobs) == 1


def test_keyless_rejection_does_not_query_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    facts, store = Facts(), InterleavedStore()
    _pin_facts(facts)
    service = PublicJobs(facts, store)
    facts.binding = replace(facts.binding, retired=True)

    def unexpected_replay(*args: Any) -> None:
        pytest.fail("keyless request must not query keyed recovery")

    monkeypatch.setattr(store, "find_by_key", unexpected_replay)
    with pytest.raises(ModelBindingError, match="model_binding_retired"):
        service.submit(_pinned_body(), request_id="b", idempotency_key=None)
    assert not store.jobs


def test_unresolved_reconciliation_is_unavailable_not_a_selection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts, store = Facts(), InterleavedStore()
    _pin_facts(facts)
    service = PublicJobs(facts, store)
    facts.binding = replace(facts.binding, retired=True)
    lookups = 0

    def lookup(*args: Any) -> None:
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            raise PublicJobUnavailableError

    monkeypatch.setattr(store, "find_by_key", lookup)
    with pytest.raises(JobOutcomeUnknownError):
        service.submit(_pinned_body(), request_id="b", idempotency_key=KEY)
    assert lookups == 2 and not store.jobs


def test_different_intent_conflicts_even_after_binding_retired() -> None:
    facts, store = Facts(), InterleavedStore()
    _pin_facts(facts)
    service = PublicJobs(facts, store, now_us=lambda: NOW)
    command = _pinned_body()

    def commit_then_retire() -> None:
        service.submit(command, request_id="a", idempotency_key=KEY)
        facts.binding = replace(facts.binding, retired=True)

    store.after_empty = commit_then_retire
    with pytest.raises(IdempotencyConflictError):
        service.submit(
            {**command, "client_reference": "different"}, request_id="b", idempotency_key=KEY
        )
    assert len(store.jobs) == 1


@pytest.mark.parametrize(
    "early_exit",
    [
        "workspace",
        "workspace_missing",
        "unpinned_model",
        "voice",
        "profile",
        "pronunciation",
        "asset",
        "catalog",
    ],
)
def test_replay_survives_other_mutable_early_exits(
    early_exit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, store = Facts(), InterleavedStore()
    service = PublicJobs(facts, store, now_us=lambda: NOW)
    command = _body()
    if early_exit == "profile":
        command["profile"] = {"id": "profile-synthetic", "revision": "1"}
        facts.published[("profile", "profile-synthetic", 1)] = replace(
            BINDING, kind="profile", object_id="profile-synthetic", revision=1
        )
    elif early_exit == "pronunciation":
        command["pronunciation"] = {"id": "pronunciation-synthetic", "revision": "1"}
        monkeypatch.setattr(facts, "pronunciation_exists", lambda *args: True)
    elif early_exit == "asset":
        command["input"] = {"text_asset_id": "asset-synthetic"}
        monkeypatch.setattr(facts, "asset", lambda *args: ("ready", "text_input"))
    committed: list[PublicJob] = []

    def commit_then_change() -> None:
        committed.append(service.submit(command, request_id="a", idempotency_key=KEY))
        if early_exit == "workspace":
            monkeypatch.setattr(facts, "workspace", lambda _: WorkspaceFact(7, False))
        elif early_exit == "workspace_missing":
            monkeypatch.setattr(facts, "workspace", lambda _: None)
        elif early_exit == "unpinned_model":
            monkeypatch.setattr(facts, "latest_binding", lambda _: None)
        elif early_exit in ("voice", "profile"):
            facts.published.clear()
        elif early_exit == "pronunciation":
            monkeypatch.setattr(facts, "pronunciation_exists", lambda *args: False)
        elif early_exit == "asset":
            monkeypatch.setattr(facts, "asset", lambda *args: None)
        else:
            monkeypatch.setattr(facts, "catalog_revision", lambda: 0)

    store.after_empty = commit_then_change
    replay = service.submit(command, request_id="b", idempotency_key=KEY)
    assert replay.job_id == committed[0].job_id
    assert replay.submission.effective_configuration_json == (
        committed[0].submission.effective_configuration_json
    )
    assert len(store.jobs) == 1
