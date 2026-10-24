"""HTTP behaviour of ``internal/runtime-control`` against the recorded intent facts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_DIGEST,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    SAMPLE_TEMPLATE_ID,
    InMemoryIntentStore,
    ScriptedNodeRuntime,
    build_request,
    build_template,
)
from tests.fake.readiness import StaticProbe

from speechnexus_node_controller.app import create_app
from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.intents import (
    RuntimeActionKind,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.payloads import request_payload
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.settings import Settings

_TOKEN = "component-token-0123456789"
_NODE = SAMPLE_NODE_ID
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_INTENTS_PATH = f"/internal/v1/nodes/{_NODE}/instance-intents"
_RECONCILE_PATH = f"/internal/v1/nodes/{_NODE}/reconcile"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    templates = tmp_path / "approved-templates.json"
    templates.write_text(
        json.dumps(
            {
                "templates": [
                    {
                        "template_id": SAMPLE_TEMPLATE_ID,
                        "image_reference": "local/synthetic-engine:0.1.0",
                        "image_digest": "sha256:" + "c" * 64,
                        "memory_architecture": "unified_memory",
                        "allowed_pool_ids": ["asr-batch"],
                        "max_peak_memory_bytes": 16 * 1024**3,
                        "container_memory_limit_bytes": 1024**3,
                        "readiness": {"kind": "process_running"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        service_name="speechnexus-node-controller-test",
        node_id=_NODE,
        component_token=SecretStr(_TOKEN),
        approved_templates_file=templates,
        intent_store_path=tmp_path / "runtime-intents.sqlite3",
        docker_socket_path=tmp_path / "missing-docker.sock",
    )


def _service(
    *, runtime: ScriptedNodeRuntime | None = None
) -> tuple[NodeRuntimeControlService, InMemoryIntentStore, ScriptedNodeRuntime]:
    resolved_runtime = runtime or ScriptedNodeRuntime()
    store = InMemoryIntentStore()
    service = NodeRuntimeControlService(
        node_id=_NODE,
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=store,
        runtime=resolved_runtime,
        now=lambda: FIXED_NOW,
    )
    return service, store, resolved_runtime


def _client(settings: Settings, service: NodeRuntimeControlService) -> AsyncClient:
    app = create_app(
        settings=settings,
        service=service,
        readiness=ReadinessService([StaticProbe("container_daemon")], timeout_seconds=1),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_submit_records_the_intent_and_replays_it_idempotently(
    settings: Settings,
) -> None:
    service, store, runtime = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))
    body = request_payload(build_request())

    async with _client(settings, service) as client:
        first = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)
        replay = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)

    assert first.status_code == 200
    assert first.json()["idempotent_replay"] is False
    assert first.json()["intent_state"] == "succeeded"
    assert first.json()["observed_instance_state"] == "ready"
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert len(runtime.prepared) == 1
    assert store.get("intent-1") is not None


async def test_changed_request_under_the_same_key_is_a_conflict(settings: Settings) -> None:
    service, _, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))

    async with _client(settings, service) as client:
        await client.post(_INTENTS_PATH, json=request_payload(build_request()), headers=_HEADERS)
        conflict = await client.post(
            _INTENTS_PATH,
            json=request_payload(build_request(fencing_token=2)),
            headers=_HEADERS,
        )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "intent_conflict"
    assert conflict.json()["error"]["retryable"] is False


@pytest.mark.parametrize(
    "mutate,expected_status,expected_code",
    [
        ({"target_node_id": "node-other"}, 409, "target_node_mismatch"),
        ({"fencing_token": 0}, 422, "invalid_runtime_request"),
        ({"config_digest": "sha256:not-a-digest"}, 422, "invalid_runtime_request"),
        ({"approved_template_id": "template-unapproved"}, 422, "unapproved_runtime_template"),
        ({"pool_id": "tts-batch"}, 422, "unapproved_runtime_template"),
    ],
)
async def test_rejections_carry_a_stable_contract_code(
    settings: Settings,
    mutate: dict[str, Any],
    expected_status: int,
    expected_code: str,
) -> None:
    service, store, _ = _service()
    body = {**request_payload(build_request()), **mutate}

    async with _client(settings, service) as client:
        response = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert store.list_for_instance(SAMPLE_INSTANCE_ID) == []


async def test_stale_generation_cannot_replace_a_serving_instance(settings: Settings) -> None:
    service, _, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))

    async with _client(settings, service) as client:
        await client.post(
            _INTENTS_PATH,
            json=request_payload(build_request(instance_generation=2, fencing_token=2)),
            headers=_HEADERS,
        )
        stale = await client.post(
            _INTENTS_PATH,
            json=request_payload(build_request(intent_id="intent-stale", instance_generation=1)),
            headers=_HEADERS,
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_fencing_token"


async def test_a_silent_node_runtime_reports_unavailable_and_records_no_stop(
    settings: Settings,
) -> None:
    runtime = ScriptedNodeRuntime()
    runtime.unavailable = True
    service, store, _ = _service(runtime=runtime)

    async with _client(settings, service) as client:
        response = await client.post(
            _INTENTS_PATH,
            json=request_payload(
                build_request(
                    intent_id="intent-stop-silent",
                    action=RuntimeActionKind.STOP,
                    fencing_token=2,
                )
            ),
            headers=_HEADERS,
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_controller_unavailable"
    assert response.json()["error"]["retryable"] is True
    recorded = store.get("intent-stop-silent")
    assert recorded is not None
    assert recorded.intent_state is RuntimeIntentState.RECONCILING
    assert recorded.error_code is RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE
    assert recorded.stop_evidence is None


async def test_get_returns_the_recorded_fact_without_a_replay_flag(settings: Settings) -> None:
    service, _, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))

    async with _client(settings, service) as client:
        await client.post(_INTENTS_PATH, json=request_payload(build_request()), headers=_HEADERS)
        found = await client.get(f"{_INTENTS_PATH}/intent-1", headers=_HEADERS)
        missing = await client.get(f"{_INTENTS_PATH}/intent-unknown", headers=_HEADERS)

    assert found.status_code == 200
    assert "idempotent_replay" not in found.json()
    assert found.json()["intent_id"] == "intent-1"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "runtime_intent_not_found"


async def test_a_foreign_node_path_is_refused_before_reading_anything(settings: Settings) -> None:
    service, _, _ = _service()

    async with _client(settings, service) as client:
        response = await client.get(
            "/internal/v1/nodes/node-other/instance-intents/intent-1", headers=_HEADERS
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "target_node_mismatch"


async def test_reconcile_folds_reported_facts_and_quarantines_untracked_instances(
    settings: Settings,
) -> None:
    service, _, _ = _service()
    async with _client(settings, service) as client:
        await client.post(
            _INTENTS_PATH,
            json=request_payload(build_request(instance_generation=2, fencing_token=2)),
            headers=_HEADERS,
        )
        accepted = await client.post(
            _RECONCILE_PATH,
            json=_snapshot(
                _fact(instance_generation=2, observed_instance_state="ready"),
                _fact(
                    instance_id="instance-untracked",
                    instance_generation=1,
                    observed_instance_state="loading",
                ),
            ),
            headers=_HEADERS,
        )

    assert accepted.status_code == 200
    assert accepted.json()["quarantined_instance_ids"] == ["instance-untracked"]
    assert accepted.json()["node_id"] == _NODE


@pytest.mark.parametrize(
    "snapshot,expected_status,expected_code",
    [
        ("controller", 409, "target_node_mismatch"),
        ("duplicate", 422, "invalid_runtime_request"),
    ],
)
async def test_reconcile_refuses_a_snapshot_it_cannot_apply(
    settings: Settings, snapshot: str, expected_status: int, expected_code: str
) -> None:
    service, _, _ = _service()
    body = (
        _snapshot(_fact(instance_generation=1), controller_id="controller-other")
        if snapshot == "controller"
        else _snapshot(_fact(instance_generation=1), _fact(instance_generation=1))
    )

    async with _client(settings, service) as client:
        response = await client.post(_RECONCILE_PATH, json=body, headers=_HEADERS)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.parametrize(
    "headers,status_code",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong-token-0123456789"}, 401),
        ({"Authorization": "Basic component-token-0123456789"}, 401),
    ],
)
async def test_every_route_requires_the_component_credential(
    settings: Settings, headers: dict[str, str], status_code: int
) -> None:
    service, _, _ = _service()

    async with _client(settings, service) as client:
        responses = [
            await client.get(f"{_INTENTS_PATH}/intent-1", headers=headers),
            await client.post(
                _INTENTS_PATH, json=request_payload(build_request()), headers=headers
            ),
            await client.post(_RECONCILE_PATH, json=_snapshot(), headers=headers),
        ]

    for response in responses:
        assert response.status_code == status_code
        assert response.json()["error"]["code"] == "component_credential_invalid"


async def test_a_body_that_is_not_the_frozen_request_shape_is_refused(
    settings: Settings,
) -> None:
    service, store, _ = _service()
    body = {**request_payload(build_request()), "unexpected": "field"}

    async with _client(settings, service) as client:
        unknown_field = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)
        missing_field = await client.post(
            _INTENTS_PATH, json={"intent_id": "intent-1"}, headers=_HEADERS
        )

    for response in (unknown_field, missing_field):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_runtime_request"
    assert store.get("intent-1") is None


async def test_request_ids_are_echoed_or_generated(settings: Settings) -> None:
    service, _, _ = _service()

    async with _client(settings, service) as client:
        echoed = await client.get("/healthz", headers={"X-Request-ID": "corr-1"})
        generated = await client.get("/healthz")
        rejected = await client.get("/healthz", headers={"X-Request-ID": "not a valid id"})

    assert echoed.headers["X-Request-ID"] == "corr-1"
    assert generated.headers["X-Request-ID"]
    assert rejected.headers["X-Request-ID"] != "not a valid id"


async def test_settings_driven_composition_fails_closed_without_a_daemon(
    settings: Settings,
) -> None:
    """A missing daemon socket leaves the process alive but not ready."""
    app = create_app(settings=settings)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        health = await client.get("/healthz")
        readiness = await client.get("/readyz")

    assert health.status_code == 200
    assert readiness.status_code == 503
    assert {check["name"] for check in readiness.json()["checks"]} == {
        "container_daemon",
        "runtime_intent_store",
    }


def _fact(
    *,
    instance_id: str = SAMPLE_INSTANCE_ID,
    instance_generation: int = 1,
    observed_instance_state: str = "loading",
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "instance_generation": instance_generation,
        "approved_template_id": SAMPLE_TEMPLATE_ID,
        "config_digest": SAMPLE_DIGEST,
        "observed_instance_state": observed_instance_state,
    }


def _snapshot(*facts: dict[str, Any], controller_id: str = _NODE) -> dict[str, Any]:
    return {
        "controller_id": controller_id,
        "fencing_token": 1,
        "reported_at": datetime(2026, 10, 24, 9, 5, tzinfo=UTC).isoformat(),
        "instances": list(facts),
    }
