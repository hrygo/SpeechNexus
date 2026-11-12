"""Recorded instance reads over HTTP: keyset pages, two-layer 404 and no invented facts.

The listing projects only facts the instance store records, so a response can
never contain a loaded artifact digest, a process start time, a heartbeat time or
an instance stop evidence document. A missing instance and a missing generation
answer different ``code`` values on the same 404 so a caller can tell "never
existed" from "this generation is gone".
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.model_instances import ModelInstanceService
from speechnexus_backend.speech.domain.model_instances import (
    InstancePosition,
    ModelInstance,
    ModelInstanceLookup,
    ModelInstancePage,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)
from speechnexus_backend.speech.interfaces.http.model_instances import (
    cursor_for,
    cursor_position,
)
from tests.unit.backend.test_health import _settings

URL = "/v1/nexus/admin/model-instances"
SINGLE = URL + "/inst-asr-1/generations/2"
DIGEST = "sha256:" + "a" * 64


def instance(**overrides: Any) -> ModelInstance:
    values: dict[str, Any] = {
        "instance_id": "inst-asr-1",
        "host_node_id": "node-a",
        "gateway_id": "gateway-1",
        "binding_id": "asr-quality",
        "binding_revision": "7",
        "binding_revision_id": "binding-revision-1",
        "pool_id": "asr-realtime",
        "generation": 1,
        "config_digest": DIGEST,
        "state": "ready",
        "ready_at_us": 1_700_000_000_000_001,
        "stopped_at_us": None,
        "quarantine_reason": None,
    }
    values.update(overrides)
    return ModelInstance(**values)


class Stub:
    def __init__(
        self,
        page: ModelInstancePage | None = None,
        lookup: ModelInstanceLookup | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.page = page if page is not None else ModelInstancePage((), None)
        self.lookup = lookup if lookup is not None else ModelInstanceLookup(False, None)
        self.failure = failure
        self.queries: list[ModelInstanceQuery] = []
        self.lookups: list[tuple[str, int]] = []

    def list(self, query: ModelInstanceQuery) -> ModelInstancePage:
        self.queries.append(query)
        if self.failure is not None:
            raise self.failure
        return self.page

    def get(self, instance_id: str, generation: int) -> ModelInstanceLookup:
        self.lookups.append((instance_id, generation))
        if self.failure is not None:
            raise self.failure
        return self.lookup


def settings() -> Settings:
    return Settings(
        **{
            **_settings().model_dump(),
            "asset_allowed_hosts": ("testserver",),
        }
    )


def client(stub: Stub | None = None, *, service: Any = None) -> TestClient:
    if service is None and stub is not None:
        service = cast(ModelInstanceService, stub)
    return TestClient(create_app(settings=settings(), model_instances=service))


def test_an_empty_recorded_set_is_an_empty_page_without_a_cursor() -> None:
    response = client(Stub()).get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "object": "nexus.list",
        "data": [],
        "has_more": False,
        "next_cursor": None,
    }


def test_the_row_carries_only_recorded_facts() -> None:
    stub = Stub(ModelInstancePage((instance(),), InstancePosition("inst-asr-1", 1)))
    response = client(stub).get(URL, headers={"X-Request-ID": "req:model-instance"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == [
        {
            "schema_version": "1.0.0",
            "instance_id": "inst-asr-1",
            "host_node_id": "node-a",
            "gateway_id": "gateway-1",
            "binding": {"id": "asr-quality", "revision": "7"},
            "binding_revision_id": "binding-revision-1",
            "pool_id": "asr-realtime",
            "generation": 1,
            "config_digest": DIGEST,
            "state": "ready",
            "readiness": {
                "accepting_new_work": True,
                "ready_at": "2023-11-14T22:13:20.000001Z",
            },
            "observed": {"stopped_at": None},
            "quarantine_reason": None,
        }
    ]
    assert body["has_more"] is True
    assert body["next_cursor"] == cursor_for(InstancePosition("inst-asr-1", 1), None, None)


def test_a_response_never_carries_facts_the_store_does_not_record() -> None:
    """Reverse assertion: an injected digest or timestamp would fail this case."""
    stub = Stub(ModelInstancePage((instance(),), None))
    response = client(stub).get(URL)
    assert response.status_code == 200
    body = response.json()
    for absent in (
        "loaded_artifact_digests",
        "warmed_capabilities",
        "started_at",
        "last_heartbeat_at",
        "stop_evidence",
        "exit_code",
        "runtime_container_id",
    ):
        assert absent not in body["data"][0]
        assert absent not in response.text
    assert set(body["data"][0]["readiness"]) == {"accepting_new_work", "ready_at"}
    assert set(body["data"][0]["observed"]) == {"stopped_at"}


def test_a_stopped_instance_reports_its_recorded_stop_time_only() -> None:
    stopped = instance(
        instance_id="inst-tts-2",
        pool_id="tts-realtime",
        generation=7,
        state="stopped",
        ready_at_us=1_700_000_000_000_001,
        stopped_at_us=1_700_000_000_000_009,
    )
    stub = Stub(ModelInstancePage((stopped,), None))
    response = client(stub).get(URL)
    row = response.json()["data"][0]
    assert row["state"] == "stopped"
    assert row["readiness"] == {
        "accepting_new_work": False,
        "ready_at": "2023-11-14T22:13:20.000001Z",
    }
    assert row["observed"] == {"stopped_at": "2023-11-14T22:13:20.000009Z"}


def test_tied_readiness_times_still_page_in_key_order() -> None:
    first = instance(instance_id="inst-asr-1", generation=1)
    second = instance(instance_id="inst-asr-2", generation=1, ready_at_us=first.ready_at_us)
    stub = Stub(ModelInstancePage((first,), InstancePosition("inst-asr-1", 1)))
    response = client(stub).get(URL)
    assert response.status_code == 200
    assert [row["instance_id"] for row in response.json()["data"]] == ["inst-asr-1"]
    # The frontier names the same instance at its generation, not the shared
    # timestamp, so the next page resumes after the last emitted key.
    assert cursor_position(response.json()["next_cursor"], None, None) == InstancePosition(
        "inst-asr-1", 1
    )
    assert second.position > first.position


def test_a_page_resumes_after_the_cursor_it_was_given() -> None:
    cursor = cursor_for(InstancePosition("inst-asr-1", 2), None, None)
    stub = Stub(ModelInstancePage((instance(instance_id="inst-asr-1", generation=3),), None))
    response = client(stub).get(URL, params={"cursor": cursor, "limit": "1"})
    assert response.status_code == 200
    assert len(stub.queries) == 1
    assert stub.queries[0] == ModelInstanceQuery(1, InstancePosition("inst-asr-1", 2))
    assert response.json()["has_more"] is False


def test_filters_are_bound_into_the_cursor_and_the_query() -> None:
    stub = Stub(ModelInstancePage((), None))
    response = client(stub).get(URL, params={"host_node_id": "node-a", "state": "stopped"})
    assert response.status_code == 200
    assert stub.queries[0] == ModelInstanceQuery(50, None, "node-a", "stopped")
    cursor = cursor_for(InstancePosition("inst-asr-1", 1), "node-a", "stopped")
    response = client(stub).get(
        URL, params={"cursor": cursor, "host_node_id": "node-a", "state": "stopped"}
    )
    assert response.status_code == 200
    assert stub.queries[1].after == InstancePosition("inst-asr-1", 1)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "abc"},
        {"limit": "1.5"},
        {"host_node_id": "Node-A"},
        {"state": "paused"},
        {"unknown": "1"},
    ],
)
def test_a_request_outside_the_declared_query_is_rejected(params: dict[str, str]) -> None:
    stub = Stub()
    response = client(stub).get(URL, params=params)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_model_instance_request"
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert stub.queries == []


def test_a_cursor_from_another_filter_is_rejected() -> None:
    cursor = cursor_for(InstancePosition("inst-asr-1", 1), "node-a", "ready")
    for params in (
        {"cursor": cursor},
        {"cursor": cursor, "host_node_id": "node-a", "state": "stopped"},
        {"cursor": "not-a-cursor"},
    ):
        stub = Stub()
        response = client(stub).get(URL, params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_model_instance_cursor"
        assert stub.queries == []


@pytest.mark.parametrize("cursor", ["", "é", "x" * 513, "not-a-cursor"])
def test_an_unreadable_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(ValueError):
        cursor_position(cursor, None, None)


def test_a_body_or_repeated_parameter_is_rejected() -> None:
    stub = Stub()
    response = client(stub).request(
        "GET", URL, content=b"{}", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_model_instance_request"
    response = client(stub).get(URL + "?limit=1&limit=2")
    assert response.status_code == 400
    assert stub.queries == []


def test_one_recorded_generation_is_returned_by_its_identity() -> None:
    stub = Stub(lookup=ModelInstanceLookup(True, instance(generation=2)))
    response = client(stub).get(SINGLE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert stub.lookups == [("inst-asr-1", 2)]
    assert response.json()["generation"] == 2
    assert response.json()["instance_id"] == "inst-asr-1"


def test_a_missing_instance_and_a_missing_generation_answer_different_codes() -> None:
    unknown = client(Stub(lookup=ModelInstanceLookup(False, None))).get(SINGLE)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_instance_unknown"
    assert unknown.json()["error"]["type"] == "not_found_error"

    generation_unknown = client(Stub(lookup=ModelInstanceLookup(True, None))).get(SINGLE)
    assert generation_unknown.status_code == 404
    assert generation_unknown.json()["error"]["code"] == "model_instance_generation_unknown"
    assert generation_unknown.json()["error"]["type"] == "not_found_error"
    assert unknown.json()["error"]["code"] != generation_unknown.json()["error"]["code"]


@pytest.mark.parametrize(
    "path",
    [
        URL + "/inst-asr-1/generations/0",
        URL + "/inst-asr-1/generations/01",
        URL + "/inst-asr-1/generations/abc",
        URL + "/Inst-ASR/generations/1",
        URL + "/inst-asr-1/generations/9223372036854775808",
    ],
)
def test_a_malformed_identity_is_a_bad_request(path: str) -> None:
    stub = Stub(lookup=ModelInstanceLookup(True, instance()))
    response = client(stub).get(path)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_model_instance_request"
    assert stub.lookups == []


def test_a_query_parameter_on_the_single_read_is_rejected() -> None:
    stub = Stub(lookup=ModelInstanceLookup(True, instance(generation=2)))
    response = client(stub).get(SINGLE, params={"limit": "1"})
    assert response.status_code == 400
    assert stub.lookups == []


@pytest.mark.parametrize("failure", [ModelInstanceUnavailableError(), ValueError("bad row")])
def test_an_unreadable_store_answers_one_service_unavailable(failure: Exception) -> None:
    response = client(Stub(failure=failure)).get(URL)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_instance_service_unavailable"
    assert response.json()["error"]["type"] == "service_unavailable_error"
    response = client(Stub(failure=failure)).get(SINGLE)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_instance_service_unavailable"


def test_an_unconfigured_reader_answers_the_same_service_unavailable() -> None:
    response = client(service=None).get(URL)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_instance_service_unavailable"


def test_an_error_carries_the_request_identity_and_never_retries() -> None:
    response = client(Stub()).get(URL, params={"limit": "0"}, headers={"X-Request-ID": "req:7"})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-should-retry"] == "false"
    assert response.json()["error"]["request_id"] == "req:7"
    assert response.json()["error"]["retryable"] is False
