"""The speech deletion projection is the frozen shared schema, not a hand copy."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import rfc8785

from speechnexus_backend.speech.domain.deletion import (
    DeletionPlan,
    DeletionTarget,
    Dependency,
    dependency_snapshot,
    plan_digest,
    plan_payload,
)
from tests.contract.test_architecture_repairs import _deletion_validator

NOW = datetime(2026, 11, 19, 3, 0, 0, 123456, tzinfo=UTC)


def target(kind: str, identifier: str, revision: int) -> DeletionTarget:
    return DeletionTarget("speech", kind, identifier, revision)


def plan(mode: str = "cascade") -> DeletionPlan:
    dependencies = (
        Dependency(target("voice_revision", "voice-b", 2), "persistent_reference", "blocks"),
        Dependency(target("operation_content", "job-a", 5), "owned_child", "cascade"),
    )
    return DeletionPlan.create(
        plan_id="dp-0123456789abcdef",
        workspace_id="workspace-default",
        target=target("asset", "asset-a", 3),
        mode=mode,
        dependencies=dependencies,
        now=NOW,
    )


def test_full_plan_matches_the_shared_schema_and_rfc8785() -> None:
    candidate = plan()
    payload = plan_payload(candidate)
    _deletion_validator("DeletionPlan").validate(payload)
    digest = payload.pop("plan_digest")
    wire_value = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    assert digest == "sha256:" + hashlib.sha256(rfc8785.dumps(wire_value)).hexdigest()
    assert digest == plan_digest(candidate)


def test_plan_pins_its_revision_window_and_scope() -> None:
    candidate = plan()
    payload = plan_payload(candidate)
    assert payload["plan_revision"] == "1"
    assert payload["created_at"] == "2026-11-19T03:00:00.123456Z"
    assert payload["expires_at"] == "2026-11-19T03:05:00.123456Z"
    assert payload["eligible"] is False
    dependencies = cast(list[dict[str, Any]], payload["dependencies"])
    assert [item["target"]["resource"]["id"] for item in dependencies] == [
        "job-a",
        "voice-b",
    ]


def test_ordinary_mode_cannot_carry_a_cascade() -> None:
    ordinary = plan("ordinary")
    assert [(item.relation, item.disposition) for item in ordinary.dependencies] == [
        ("owned_child", "blocks"),
        ("persistent_reference", "blocks"),
    ]
    _deletion_validator("DeletionPlan").validate(plan_payload(ordinary))


def test_one_relation_cannot_carry_two_dispositions() -> None:
    item = Dependency(target("voice_revision", "voice-a", 1), "persistent_reference", "blocks")
    retained = Dependency(target("voice_revision", "voice-a", 1), "persistent_reference", "retain")
    with pytest.raises(ValueError):
        dependency_snapshot((item, retained), mode="cascade")


def test_an_independent_copy_is_never_deleted() -> None:
    with pytest.raises(ValueError):
        Dependency(target("voice_revision", "voice-a", 1), "independent_copy", "cascade")
    retained = Dependency(target("voice_revision", "voice-a", 1), "independent_copy", "retain")
    assert dependency_snapshot((retained,), mode="cascade") == (retained,)
