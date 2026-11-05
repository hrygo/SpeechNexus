"""Actual application/router and generated contracts, using synthetic SQL/qualification."""

from fastapi.testclient import TestClient
from speechnexus_contracts.profile_public_http import validate_response

from speechnexus_backend.app import create_app
from tests.integration.test_profile_publications import Sandbox
from tests.support.profile_examples import profile_request
from tests.unit.backend.test_health import _settings
from tests.unit.test_profile_publications import sandbox as sandbox

PUBLIC = "/v1/nexus/profiles"


def test_actual_http_lists_reads_publishes_activates_and_replays(sandbox: Sandbox) -> None:
    s = sandbox
    identity = profile_request()["profile_id"]
    admin = f"/v1/nexus/admin/profiles/{identity}/revisions/1"
    fixed = f"{PUBLIC}/{identity}/revisions/1"
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("testserver",)}),
        profiles=s.port,
    )
    # TestClient runs the real route in a thread, so the synthetic SQL fixture
    # explicitly permits threads; there is no PG lock qualification here.
    with TestClient(app) as http:
        s.draft()
        assert http.get(PUBLIC).json()["data"] == []
        assert http.get(fixed).status_code == 409
        assert http.get(f"{PUBLIC}/{identity}/revisions/99").status_code == 404
        command = s.publish()
        import json

        body = json.loads(command.intent)["body"]
        headers = {"Idempotency-Key": "publication-key"}
        response = http.post(admin + "/publications", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["publication"]["status"] == "published"
        assert http.get(fixed).json() == response.json()
        assert http.get(admin).json() == response.json()
        assert (
            http.post(admin + "/publications", json=body, headers=headers).json() == response.json()
        )
        listing = http.get(PUBLIC)
        validate_response("listProfiles", 200, {"body": listing.json()})
        assert [(p["revision"], p["current_revision"]) for p in listing.json()["data"]] == [
            ("1", "1")
        ]
        assert "key_digest" not in listing.text and "validation_report_id" not in listing.text
        assert http.get(PUBLIC + "?limit=bad").status_code == 400
        assert http.get(PUBLIC + "?cursor=bad").status_code == 400
        assert http.post(PUBLIC).status_code == 405
        assert http.get(PUBLIC + "?unknown=1").status_code == 400
        assert (
            http.post(admin + "/activations", json={"expected_current_revision": "1"}).status_code
            == 200
        )


def test_unconfigured_new_paths_fail_closed_and_enforce_ingress() -> None:
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("testserver",)})
    )
    with TestClient(app) as http:
        for path in (PUBLIC, PUBLIC + "/synthetic/revisions/1"):
            assert http.get(path).status_code == 503
        path = "/v1/nexus/admin/profiles/synthetic/revisions/1/publications"
        response = http.post(path, json={})
        assert response.status_code == 503 and response.json()["error"]["retryable"] is False
        assert http.get(path).headers["Allow"] == "POST"


def test_public_pages_and_historical_original_result_survive_default_changes(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    for n in range(2, 12):
        revision = str(n)
        s.draft(revision)
        assert (
            s.port.mutate(s.publish(revision, str(n - 1), "publish-key-" + revision)).outcome
            == "completed"
        )
    app = create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("testserver",)}),
        profiles=s.port,
    )
    with TestClient(app) as http:
        revisions: list[str] = []
        path = PUBLIC + "?limit=3"
        while True:
            response = http.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            revisions.extend(p["revision"] for p in data["data"])
            assert all(p["current_revision"] == "11" for p in data["data"])
            if not data["has_more"]:
                assert data["next_cursor"] is None
                break
            path = PUBLIC + "?limit=3&cursor=" + data["next_cursor"]
        assert revisions == [str(n) for n in range(1, 12)]
        for query in (
            "limit=0",
            "limit=101",
            "limit=1&limit=2",
            "cursor=W10",
            "cursor=WyJib2d1cyIsImxhdGVzdCJd",
        ):
            assert http.get(PUBLIC + "?" + query).status_code == 400
