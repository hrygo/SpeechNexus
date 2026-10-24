"""Recovery validates original relational facts; the fake cursor permits no writes."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from typing import Any

import pytest

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.infrastructure.admission_codec import prepare_admission
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import read_original
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from tests.fake.admission import ADMISSION, BUSINESS, COMMAND, STAGE
from tests.fake.runtime_intents import NOW, PLAN


class OriginalRows:
    """Scripted read seam, not a SQL/locking emulator or portability oracle."""

    def __init__(self) -> None:
        self.prepared = prepare_admission(COMMAND, max_command_bytes=1048576)
        self.data: dict[str, list[dict[str, Any]]] = {
            "admission_attempts": [
                {
                    "operation_id": ADMISSION.identity.operation_id,
                    "attempt_id": ADMISSION.identity.attempt_id,
                    "workspace_id": ADMISSION.workspace_id,
                    "binding_revision_id": ADMISSION.binding_revision_id,
                    "manifest_id": ADMISSION.manifest_id,
                    "recipe_digest": ADMISSION.recipe_digest,
                    "policy_revision_id": ADMISSION.policy_revision_id,
                    "service_class": ADMISSION.service_class,
                    "request_digest": self.prepared.request_digest,
                    "required_stage_ids": json.dumps([STAGE.stage_id]),
                    "quota_state": "held",
                    "version": 1,
                }
            ],
            "admission_requests": [
                {
                    "request_document": self.prepared.document,
                    "request_digest": self.prepared.request_digest,
                    "recorded_at_us": NOW,
                }
            ],
            "admission_operations": [
                {
                    "operation_id": ADMISSION.identity.operation_id,
                    "workspace_id": ADMISSION.workspace_id,
                    "request_digest": BUSINESS.request_digest,
                    "initial_attempt_id": ADMISSION.identity.attempt_id,
                    "current_attempt_id": ADMISSION.identity.attempt_id,
                    "recorded_at_us": NOW,
                }
            ],
            "admission_leases": [
                {
                    "lease_id": STAGE.lease_id,
                    "operation_id": ADMISSION.identity.operation_id,
                    "attempt_id": ADMISSION.identity.attempt_id,
                    "stage_id": STAGE.stage_id,
                    "pool_id": STAGE.pool_id,
                    "host_node_id": STAGE.host_node_id,
                    "gateway_id": STAGE.gateway_id,
                    "instance_id": STAGE.instance_id,
                    "instance_generation": STAGE.instance_generation,
                    "reserved_slots": STAGE.reserved_slots,
                    "fencing_token": 7,
                    "deadline_at_us": ADMISSION.deadline_at_us,
                    "version": 1,
                    "lease_state": "reserved",
                    "cleanup_state": "pending",
                    "quarantine_observed_at_us": None,
                    "quarantine_reason_code": None,
                    "budget_released_at_us": None,
                    **{
                        "reserved_" + name: value
                        for name, value in zip(DIMENSIONS, PLAN.per_slot.values, strict=True)
                    },
                }
            ],
            "coordination_jobs": [asdict(ADMISSION.job)],
            "coordination_outbox": [
                {"job_id": ADMISSION.job.job_id, "event_digest": ADMISSION.job.event_digest}
            ],
        }
        self.document: str | None = BUSINESS.document
        self.current: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    @property
    def rowcount(self) -> int:
        return len(self.current)

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        assert query.lstrip().startswith("SELECT"), "Recovery attempted a write"
        self.statements.append((query, parameters))
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT request_document FROM admission_operations"):
            self.current = [{"request_document": self.document}]
            return
        for table, values in self.data.items():
            if "FROM " + table + " " in normalized:
                self.current = copy.deepcopy(values)
                return
        raise AssertionError("Unexpected original-fact query")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.current


def test_original_recovery_is_complete_and_never_reads_current_runtime_or_policy() -> None:
    cursor = OriginalRows()
    result = read_original(
        cursor, ADMISSION.identity, max_command_bytes=1048576, prepared=cursor.prepared
    )
    assert result is not None and result.fact is not None
    assert result.fact.reservations[0].stage == STAGE
    assert result.fact.reservations[0].physical == PLAN.per_slot
    assert all(
        "resource_pools" not in sql and "engine_instances" not in sql
        for sql, _ in cursor.statements
    )


def test_removed_private_content_does_not_restore_a_body_or_drop_original_metadata() -> None:
    cursor = OriginalRows()
    cursor.document = None
    result = read_original(
        cursor, ADMISSION.identity, max_command_bytes=1048576, prepared=cursor.prepared
    )
    assert result is not None and result.outcome == "replayed"
    assert cursor.document is None


@pytest.mark.parametrize(
    "missing",
    [
        "admission_requests",
        "admission_operations",
        "admission_leases",
        "coordination_jobs",
        "coordination_outbox",
    ],
)
def test_partial_original_cannot_be_repaired_into_success(missing: str) -> None:
    cursor = OriginalRows()
    cursor.data[missing] = []
    with pytest.raises(AdmissionRejectedError, match="reconciliation_required"):
        read_original(cursor, ADMISSION.identity, max_command_bytes=1048576)


@pytest.mark.parametrize(
    "damage",
    [
        "digest",
        "workspace",
        "stages",
        "version",
        "lease-parent",
        "lease-fence",
        "lease-slots",
        "physical",
        "job-digest",
        "envelope-bytes",
        "record-time",
    ],
)
def test_inconsistent_original_rows_are_rejected_without_reinterpreting_defaults(
    damage: str,
) -> None:
    cursor = OriginalRows()
    if damage == "digest":
        cursor.data["admission_requests"][0]["request_digest"] = "sha256:" + "a" * 64
    elif damage == "workspace":
        cursor.data["admission_operations"][0]["workspace_id"] = "workspace-other"
    elif damage == "stages":
        cursor.data["admission_attempts"][0]["required_stage_ids"] = (
            '["stage-main","missing-stage"]'
        )
    elif damage == "version":
        cursor.data["admission_attempts"][0]["version"] = 0
    elif damage == "lease-parent":
        cursor.data["admission_leases"][0]["host_node_id"] = "host-other"
    elif damage == "lease-fence":
        cursor.data["admission_leases"][0]["fencing_token"] = 8
    elif damage == "lease-slots":
        cursor.data["admission_leases"][0]["reserved_slots"] = 2
    elif damage == "physical":
        cursor.data["admission_leases"][0]["reserved_host_pcm_buffer_bytes"] = -1
    elif damage == "job-digest":
        cursor.data["coordination_jobs"][0]["request_digest"] = "sha256:" + "a" * 64
    elif damage == "envelope-bytes":
        cursor.data["admission_requests"][0]["request_document"] = "{}"
    else:
        cursor.data["admission_operations"][0]["recorded_at_us"] = NOW + 1
    with pytest.raises(AdmissionRejectedError, match="reconciliation_required"):
        read_original(cursor, ADMISSION.identity, max_command_bytes=1048576)


def test_expected_original_digest_conflict_does_not_authorize_a_retry() -> None:
    cursor = OriginalRows()
    with pytest.raises(AdmissionRejectedError, match="idempotency_conflict"):
        read_original(
            cursor,
            ADMISSION.identity,
            max_command_bytes=1048576,
            request_digest=BUSINESS.request_digest,
        )
    cursor.data["admission_attempts"] = []
    assert read_original(cursor, ADMISSION.identity, max_command_bytes=1048576) is None
