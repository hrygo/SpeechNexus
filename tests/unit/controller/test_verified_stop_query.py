"""Only a released local checkpoint can authenticate an instance stop query."""

from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from speechnexus_contracts.runtime_http import validate_response
from tests.fake.controller import (
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    ScriptedNodeRuntime,
    build_request,
    build_template,
)
from tests.fake.docker_api import FakeDockerEngine
from tests.unit.controller.test_http_runtime_control import _client
from tests.unit.controller.test_http_runtime_control import settings as settings

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeIntentNotFoundError,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore
from speechnexus_node_controller.settings import Settings

PREPARE = build_request()
STOP = replace(PREPARE, intent_id="original-stop", action=RuntimeActionKind.STOP, fencing_token=2)
HEADERS = {"Authorization": "Bearer component-token-0123456789", "X-Request-ID": "proof-query"}
PATH = (
    f"/internal/v1/nodes/{SAMPLE_NODE_ID}/instances/{SAMPLE_INSTANCE_ID}"
    "/generations/1/verified-stop"
)


@pytest.fixture
def native(
    tmp_path: Path,
) -> Iterator[tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine]]:
    engine = FakeDockerEngine(images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST})
    with closing(SqliteIntentStore(tmp_path / "proofs.sqlite3")) as store:
        yield (
            NodeRuntimeControlService(
                node_id=SAMPLE_NODE_ID,
                templates=ApprovedTemplateRegistry((build_template(),)),
                store=store,
                stop_checkpoints=store,
                runtime=DockerNodeRuntime(
                    api=engine, node_id=SAMPLE_NODE_ID, stop_checkpoints=store
                ),
            ),
            store,
            engine,
        )


async def test_verified_query_is_read_only_and_returns_the_original_proof(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
    settings: Settings,
) -> None:
    service, store, engine = native
    service.submit_intent(PREPARE)
    stopped = service.submit_intent(STOP).intent
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None and checkpoint.stage == "released"
    calls = (len(engine.stopped), len(engine.removed))
    async with _client(settings, service) as client:
        first = await client.get(PATH, headers=HEADERS)
        again = await client.get(PATH, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json() == again.json()
    validate_response(
        "getVerifiedInstanceStop",
        first.status_code,
        {"headers": dict(first.headers), "body": first.json()},
    )
    assert first.json()["intent_id"] == STOP.intent_id
    assert first.json()["stop_evidence"]["runtime_container_id"] == checkpoint.container_id
    assert "idempotent_replay" not in first.json()
    assert store.get(STOP.intent_id) == stopped
    assert store.get_stop_checkpoint(STOP) == checkpoint
    assert (len(engine.stopped), len(engine.removed)) == calls


@pytest.mark.parametrize("phase", ["missing", "ready", "exit-only", "legacy-success"])
async def test_missing_incomplete_and_legacy_proofs_return_no_stop_fact(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
    settings: Settings,
    phase: str,
) -> None:
    service, store, engine = native
    if phase != "missing":
        service.submit_intent(PREPARE)
    if phase == "exit-only":
        engine.fail_remove_and_keep = True
        assert service.submit_intent(STOP).intent.intent_state is RuntimeIntentState.QUARANTINED
    if phase == "legacy-success":
        legacy = NodeRuntimeControlService(
            node_id=SAMPLE_NODE_ID,
            templates=ApprovedTemplateRegistry((build_template(),)),
            store=store,
            stop_checkpoints=store,
            runtime=ScriptedNodeRuntime(),
        )
        assert legacy.submit_intent(STOP).intent.intent_state is RuntimeIntentState.SUCCEEDED
        assert store.get_stop_checkpoint(STOP) is None
    before = store.list_for_instance(SAMPLE_INSTANCE_ID)
    calls = (len(engine.stopped), len(engine.removed))
    async with _client(settings, service) as client:
        response = await client.get(PATH, headers=HEADERS)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "runtime_intent_not_found"
    assert store.list_for_instance(SAMPLE_INSTANCE_ID) == before
    assert (len(engine.stopped), len(engine.removed)) == calls


@pytest.mark.parametrize("corrupt", ["container", "exit", "time"])
async def test_a_receipt_that_disagrees_with_its_checkpoint_is_unavailable(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
    settings: Settings,
    corrupt: str,
) -> None:
    service, store, _ = native
    service.submit_intent(PREPARE)
    stopped = service.submit_intent(STOP).intent
    assert stopped.stop_evidence is not None
    if corrupt == "container":
        stopped.stop_evidence = replace(
            stopped.stop_evidence, runtime_container_id="another-container"
        )
    elif corrupt == "exit":
        stopped.stop_evidence = replace(stopped.stop_evidence, exit_code=42)
    else:
        stopped.stop_evidence = replace(
            stopped.stop_evidence,
            verified_at=stopped.stop_evidence.verified_at + timedelta(seconds=1),
        )
    version = stopped.version
    stopped.version += 1
    store.save(stopped, expected_version=version)
    async with _client(settings, service) as client:
        response = await client.get(PATH, headers=HEADERS)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "runtime_controller_unavailable"
    assert store.get(STOP.intent_id) == stopped


def test_ambiguous_released_sources_are_not_resolved_by_choosing_the_latest(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
) -> None:
    service, store, _ = native
    service.submit_intent(PREPARE)
    original = service.submit_intent(STOP).intent
    checkpoint = store.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    another = replace(STOP, intent_id="another-stop", fencing_token=3)
    receipt = RuntimeInstanceIntent(
        request=another, created_at=original.created_at, updated_at=original.created_at
    )
    store.add(receipt)
    duplicate = replace(checkpoint, request=another)
    store.save_stop_checkpoint(StopCheckpoint(another, checkpoint.container_id))
    store.save_stop_checkpoint(replace(duplicate, released_at=None))
    store.save_stop_checkpoint(duplicate)
    observation = original.stop_evidence
    assert observation is not None
    receipt.intent_state = original.intent_state
    receipt.observed_instance_state = original.observed_instance_state
    receipt.memory_hold = original.memory_hold
    receipt.stop_evidence = observation
    receipt.updated_at = original.updated_at
    receipt.version += 1
    store.save(receipt, expected_version=1)
    with pytest.raises(RuntimeControllerUnavailableError, match="ambiguous"):
        service.get_verified_stop(SAMPLE_INSTANCE_ID, 1)


@pytest.mark.parametrize(
    "path,headers,status",
    [
        (PATH, {}, 401),
        (PATH.replace(SAMPLE_NODE_ID, "node-other"), HEADERS, 409),
        (PATH.replace("/1/verified-stop", "/2/verified-stop"), HEADERS, 404),
        (PATH.replace("/1/verified-stop", "/0/verified-stop"), HEADERS, 422),
        (PATH.replace("/1/verified-stop", "/true/verified-stop"), HEADERS, 422),
        (PATH.replace("/1/verified-stop", "/9007199254740992/verified-stop"), HEADERS, 422),
    ],
)
async def test_verified_query_keeps_credential_node_and_generation_boundaries(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
    settings: Settings,
    path: str,
    headers: dict[str, str],
    status: int,
) -> None:
    service, _, _ = native
    async with _client(settings, service) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == status, response.text


def test_stop_proof_is_not_implicitly_available_without_configured_checkpoints(
    native: tuple[NodeRuntimeControlService, SqliteIntentStore, FakeDockerEngine],
    tmp_path: Path,
) -> None:
    _, store, _ = native
    service = NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=store,
        runtime=ScriptedNodeRuntime(),
    )
    with pytest.raises(RuntimeControllerUnavailableError):
        service.get_verified_stop(SAMPLE_INSTANCE_ID, 1)
    store.close()
    with pytest.raises(RuntimeControllerUnavailableError):
        native[0].get_verified_stop(SAMPLE_INSTANCE_ID, 1)
    # A reopened empty store is a missing proof, not physical absence.
    with (
        pytest.raises(RuntimeIntentNotFoundError),
        closing(SqliteIntentStore(tmp_path / "proofs.sqlite3")) as reopened,
    ):
        reopened_service = NodeRuntimeControlService(
            node_id=SAMPLE_NODE_ID,
            templates=ApprovedTemplateRegistry((build_template(),)),
            store=reopened,
            stop_checkpoints=reopened,
            runtime=ScriptedNodeRuntime(),
        )
        reopened_service.get_verified_stop(SAMPLE_INSTANCE_ID, 1)
