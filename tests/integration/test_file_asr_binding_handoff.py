"""The B offline create input is consumed by A's actual PG HTTP producer."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from speechnexus_contracts.binding_defaults_http import validate_response
from tests.integration.test_binding_defaults_http import (
    DefaultsHTTPDatabase,
)
from tests.integration.test_binding_defaults_http import (
    database as database,
)
from tests.integration.test_binding_defaults_http import (
    http_db as http_db,
)
from tests.integration.test_binding_defaults_http import (
    producer_db as producer_db,
)
from tests.unit.test_file_asr_artifacts import (
    REGISTRATION,
    cli,
)
from tests.unit.test_file_asr_artifacts import (
    inputs as inputs,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


def test_emitted_binding_and_defaults_have_one_original_identity_across_replay_and_restart(
    producer_db: DefaultsHTTPDatabase, inputs: dict[str, Any]
) -> None:
    emitted = cli(inputs, *REGISTRATION)
    assert emitted.returncode == 0, emitted.stderr
    request = json.loads(emitted.stdout)
    path = "/v1/nexus/admin/model-bindings"
    headers = {"Idempotency-Key": "original-file-asr-handoff"}
    with TestClient(producer_db.app()) as client:
        response = client.post(path, json=request, headers=headers)
        assert response.status_code == 201, response.text
        original = response.json()
        validate_response("createModelBindingDraft", 201, {"body": original})
        assert original["publication"]["status"] == "draft"
        assert original["publication"]["capacity_manifest_id"] is None
        assert original["publication"]["qualification_report_id"] is None
        assert original["engine"] == request["engine"]
        assert original["artifact_set"] == request["artifact_set"]
        snapshot_path = (
            path + f"/{original['binding_id']}/revisions/{original['revision']}/parameter-defaults"
        )
        defaults = client.get(snapshot_path)
        assert defaults.status_code == 200
        snapshot = defaults.json()
        validate_response("getModelBindingParameterDefaults", 200, {"body": snapshot})
        assert snapshot["binding_revision_id"] == original["revision_id"]
        assert snapshot["parameters"] == request["parameter_defaults"]
        before = producer_db.snapshot()
        assert client.post(path, json=request, headers=headers).json() == original
        changed = json.loads(emitted.stdout)
        changed["parameter_defaults"]["asr"]["final_deadline_ms"] = 9000
        conflict = client.post(path, json=changed, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert producer_db.snapshot() == before
    with TestClient(producer_db.app()) as client:
        replay = client.post(path, json=request, headers=headers)
        assert replay.status_code == 201 and replay.json() == original
        assert client.get(snapshot_path).json() == snapshot
    assert producer_db.snapshot() == before
    assert producer_db.db.counts() == (0, 0)
