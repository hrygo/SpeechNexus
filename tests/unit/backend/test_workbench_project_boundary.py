"""Generated request bodies reach the real workbench use cases without coercion."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import RevisionConflictError
from speechnexus_backend.workbench.interfaces.project_commands import ProjectCommandBoundary
from tests.fake.workbench_versions import MemoryVersionUnitOfWork


@pytest.fixture
def store() -> MemoryVersionUnitOfWork:
    return MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))


def boundary(store: MemoryVersionUnitOfWork) -> ProjectCommandBoundary:
    return ProjectCommandBoundary(
        WorkbenchVersions(store), default_workspace_id="workspace-default"
    )


async def test_create_preserves_content_and_uses_the_configured_default(
    store: MemoryVersionUnitOfWork,
) -> None:
    request = {"name": "  合成项目  ", "description": "说明"}
    before = copy.deepcopy(request)
    result = await boundary(store).create_project(request, idempotency_key="create-request")
    assert result.workspace_id == "workspace-default"
    assert result.name == "  合成项目  "
    assert result.description == "说明"
    assert result.metadata_revision == 1
    assert request == before
    assert await WorkbenchVersions(store).get_project(result.id) == result


async def test_explicit_workspace_has_its_own_idempotency_scope(
    store: MemoryVersionUnitOfWork,
) -> None:
    adapter = boundary(store)
    default = await adapter.create_project({"name": "合成项目"}, idempotency_key="same-key")
    explicit = await adapter.create_project(
        {"name": "合成项目", "workspace_id": "workspace-other", "description": None},
        idempotency_key="same-key",
    )
    assert default.id != explicit.id
    assert explicit.workspace_id == "workspace-other"
    assert explicit.description is None
    assert len(store.projects) == 2


async def test_patch_keeps_omitted_fields_and_clears_explicit_null(
    store: MemoryVersionUnitOfWork,
) -> None:
    adapter = boundary(store)
    created = await adapter.create_project({"name": "原名称", "description": "原说明"})
    changed = await adapter.update_project(
        created.id, {"expected_metadata_revision": 1, "name": "新名称"}
    )
    assert (changed.name, changed.description, changed.status) == ("新名称", "原说明", "active")
    request = {"expected_metadata_revision": 2, "description": None, "status": "archived"}
    cleared = await adapter.update_project(created.id, request)
    assert (cleared.name, cleared.description, cleared.status) == ("新名称", None, "archived")
    assert cleared.metadata_revision == 3
    assert request == {"expected_metadata_revision": 2, "description": None, "status": "archived"}


async def test_replay_returns_the_committed_snapshot_before_stale_revision_check(
    store: MemoryVersionUnitOfWork,
) -> None:
    adapter = boundary(store)
    created = await adapter.create_project({"name": "原名称"}, idempotency_key="create-key")
    request = {"expected_metadata_revision": 1, "name": "新名称"}
    changed = await adapter.update_project(created.id, request, idempotency_key="patch-key")
    await adapter.update_project(
        created.id, {"expected_metadata_revision": 2, "status": "archived"}
    )
    assert await adapter.create_project({"name": "原名称"}, idempotency_key="create-key") == created
    assert await adapter.update_project(created.id, request, idempotency_key="patch-key") == changed
    with pytest.raises(IdempotencyConflictError):
        await adapter.update_project(
            created.id,
            {"expected_metadata_revision": 1, "name": "不同意图"},
            idempotency_key="patch-key",
        )
    assert (await WorkbenchVersions(store).get_project(created.id)).metadata_revision == 3


async def test_parallel_validated_edits_expose_the_real_revision_conflict(
    store: MemoryVersionUnitOfWork,
) -> None:
    adapter = boundary(store)
    created = await adapter.create_project({"name": "原名称"})
    results = await asyncio.gather(
        *(
            adapter.update_project(created.id, {"expected_metadata_revision": 1, "name": name})
            for name in ("一", "二")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    assert (await WorkbenchVersions(store).get_project(created.id)).metadata_revision == 2


async def test_integral_json_number_uses_one_domain_intent_without_mutating_input(
    store: MemoryVersionUnitOfWork,
) -> None:
    adapter = boundary(store)
    created = await adapter.create_project({"name": "合成项目"})
    payload = {"expected_metadata_revision": 1.0, "description": None}
    first = await adapter.update_project(created.id, payload, idempotency_key="integral-number")
    replay = await adapter.update_project(
        created.id,
        {"expected_metadata_revision": 1, "description": None},
        idempotency_key="integral-number",
    )
    assert replay == first
    assert isinstance(payload["expected_metadata_revision"], float)
    assert len(store.receipts) == 1


async def test_missing_project_stays_a_not_found_outcome(store: MemoryVersionUnitOfWork) -> None:
    with pytest.raises(ResourceNotFoundError):
        await boundary(store).update_project(
            "missing-project", {"expected_metadata_revision": 1, "description": None}
        )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"name": 7},
        {"name": ""},
        {"name": "x" * 129},
        {"name": "合成", "description": "x" * 2001},
        {"name": "合成", "workspace_id": None},
        {"name": "合成", "workspace_id": "UPPERCASE"},
        {"name": "合成", "undeclared": "synthetic-sensitive-value"},
    ],
)
async def test_invalid_create_is_rejected_before_opening_a_transaction(
    store: MemoryVersionUnitOfWork, payload: Any
) -> None:
    adapter = boundary(store)
    store.fail_commit = True
    with pytest.raises(ContractValidationError, match=r"^Invalid ProjectCreateRequest$"):
        await adapter.create_project(payload, idempotency_key="reusable-key")
    assert not store.projects
    assert not store.receipts


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_metadata_revision": 1},
        {"expected_metadata_revision": "1", "name": "合成"},
        {"expected_metadata_revision": True, "name": "合成"},
        {"expected_metadata_revision": 0, "name": "合成"},
        {"expected_metadata_revision": 1, "name": None},
        {"expected_metadata_revision": 1, "name": ""},
        {"expected_metadata_revision": 1, "status": "deleted"},
        {"expected_metadata_revision": 1, "description": 12},
        {"expected_metadata_revision": 1, "description": None, "metadata_revision": 1},
    ],
)
async def test_invalid_patch_does_not_claim_a_key_or_change_content(
    store: MemoryVersionUnitOfWork, payload: Any
) -> None:
    adapter = boundary(store)
    created = await adapter.create_project({"name": "原名称", "description": "原说明"})
    before = copy.deepcopy(payload)
    with pytest.raises(ContractValidationError, match=r"^Invalid ProjectPatch$"):
        await adapter.update_project(created.id, payload, idempotency_key="reusable-key")
    assert payload == before
    assert await WorkbenchVersions(store).get_project(created.id) == created
    assert not store.receipts
    changed = await adapter.update_project(
        created.id,
        {"expected_metadata_revision": 1, "description": None},
        idempotency_key="reusable-key",
    )
    assert changed.description is None
    assert changed.metadata_revision == 2
