"""Actual application/router and generated contracts, using synthetic SQL/qualification."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from speechnexus_contracts.profile_lifecycle_http import (
    validate_response as lifecycle_response,
)
from speechnexus_contracts.profile_public_http import validate_response

from speechnexus_backend.app import create_app
from tests.integration.test_profile_publications import Sandbox
from tests.support.profile_examples import profile_request
from tests.unit.backend.test_health import _settings
from tests.unit.test_profile_publications import sandbox as sandbox

PUBLIC = "/v1/nexus/profiles"
PUBLIC_V2 = "/v2/nexus/profiles"


def _app(s: Sandbox | None = None) -> FastAPI:
    return create_app(
        settings=_settings().model_copy(update={"asset_allowed_hosts": ("testserver",)}),
        profiles=None if s is None else s.port,
    )


def test_actual_http_lists_reads_publishes_activates_and_replays(sandbox: Sandbox) -> None:
    s = sandbox
    identity = profile_request()["profile_id"]
    admin = f"/v1/nexus/admin/profiles/{identity}/revisions/1"
    fixed = f"{PUBLIC}/{identity}/revisions/1"
    app = _app(s)
    # TestClient runs the real route in a thread, so the synthetic SQL fixture
    # explicitly permits threads; there is no PG lock qualification here.
    with TestClient(app) as http:
        s.draft()
        assert http.get(PUBLIC).json()["data"] == []
        assert http.get(PUBLIC_V2).json()["data"] == []
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
        assert [p["revision"] for p in listing.json()["data"]] == ["1"]
        assert all("current_revision" not in p for p in listing.json()["data"])
        published = http.get(PUBLIC_V2)
        validate_response("listPublishedProfiles", 200, {"body": published.json()})
        assert [(p["revision"], p["current_revision"]) for p in published.json()["data"]] == [
            ("1", "1")
        ]
        assert "key_digest" not in published.text and "validation_report_id" not in published.text
        assert http.get(PUBLIC + "?limit=bad").status_code == 400
        assert http.get(PUBLIC + "?cursor=bad").status_code == 400
        assert http.post(PUBLIC).status_code == 405
        assert http.get(PUBLIC + "?unknown=1").status_code == 400
        assert (
            http.post(admin + "/activations", json={"expected_current_revision": "1"}).status_code
            == 200
        )


def test_unconfigured_new_paths_fail_closed_and_enforce_ingress() -> None:
    app = _app()
    with TestClient(app) as http:
        for path in (PUBLIC, PUBLIC_V2, PUBLIC + "/synthetic/revisions/1"):
            assert http.get(path).status_code == 503
        unavailable = http.get(PUBLIC_V2)
        validate_response("listPublishedProfiles", 503, {"body": unavailable.json()})
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
    app = _app(s)
    with TestClient(app) as http:
        revisions: list[str] = []
        path = PUBLIC + "?limit=3"
        while True:
            response = http.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            revisions.extend(p["revision"] for p in data["data"])
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


def test_v2_reports_no_default_pointer_without_fabricating_one(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    s.admin.execute("UPDATE speech_profile_families SET current_revision=NULL")
    with TestClient(_app(s)) as http:
        response = http.get(PUBLIC_V2)
        assert response.status_code == 200, response.text
        validate_response("listPublishedProfiles", 200, {"body": response.json()})
        assert [(p["revision"], p["current_revision"]) for p in response.json()["data"]] == [
            ("1", None)
        ]
        legacy = http.get(PUBLIC).json()
        assert "current_revision" not in legacy["data"][0]


def test_v2_excludes_drafts_and_pages_from_the_last_item(sandbox: Sandbox) -> None:
    s = sandbox
    s.draft()
    s.port.mutate(s.publish())
    for n in range(2, 8):
        revision = str(n)
        s.draft(revision)
        assert (
            s.port.mutate(s.publish(revision, str(n - 1), "publish-key-" + revision)).outcome
            == "completed"
        )
    s.draft("8")
    with TestClient(_app(s)) as http:
        revisions: list[str] = []
        path = PUBLIC_V2 + "?limit=3"
        while True:
            response = http.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            validate_response("listPublishedProfiles", 200, {"body": data})
            assert all(p["current_revision"] == "7" for p in data["data"])
            revisions.extend(p["revision"] for p in data["data"])
            if not data["has_more"]:
                assert data["next_cursor"] is None
                break
            path = PUBLIC_V2 + "?limit=3&cursor=" + data["next_cursor"]
        assert revisions == [str(n) for n in range(1, 8)]


def test_v2_errors_are_source_declared(sandbox: Sandbox) -> None:
    with TestClient(_app(sandbox)) as http:
        for query in (
            "limit=0",
            "limit=101",
            "limit=1&limit=2",
            "cursor=bad",
            "cursor=W10",
            "unknown=1",
        ):
            response = http.get(PUBLIC_V2 + "?" + query)
            assert response.status_code == 400, response.text
            validate_response(
                "listPublishedProfiles",
                400,
                {"body": response.json(), "headers": dict(response.headers)},
            )
        rejected = http.get(PUBLIC_V2, headers={"Host": "foreign.test"})
        assert rejected.status_code == 403
        validate_response(
            "listPublishedProfiles",
            403,
            {"body": rejected.json(), "headers": dict(rejected.headers)},
        )
        method = http.post(PUBLIC_V2)
        assert method.status_code == 405 and method.headers["Allow"] == "GET"
        validate_response(
            "listPublishedProfiles",
            405,
            {"body": method.json(), "headers": dict(method.headers)},
        )
    with TestClient(_app()) as http:
        unavailable = http.get(PUBLIC_V2)
        assert unavailable.status_code == 503
        validate_response(
            "listPublishedProfiles",
            503,
            {"body": unavailable.json(), "headers": dict(unavailable.headers)},
        )


def test_published_summary_projection_has_a_fixed_key_set(sandbox: Sandbox) -> None:
    """The wire projection is asserted independently of response validation."""
    from speechnexus_backend.speech.interfaces.http.profile_publications import _summary

    s = sandbox
    s.draft()
    result = s.port.mutate(s.publish())
    assert result.publication is not None
    v1 = _summary(result.publication, "1", include_current=False)
    v2 = _summary(result.publication, "1", include_current=True)
    assert set(v1) == {
        "id",
        "revision",
        "display_name",
        "kind",
        "release_tier",
        "execution_classes",
    }
    assert set(v2) == set(v1) | {"current_revision"}
    assert v1["id"] == result.publication.draft.profile_id
    assert v1["revision"] == "1"


def test_actual_http_retirement_replays_conflicts_and_clears_the_pointer(sandbox: Sandbox) -> None:
    s = sandbox
    identity = profile_request()["profile_id"]
    revision = f"/v1/nexus/admin/profiles/{identity}/revisions/1"
    fixed = f"{PUBLIC}/{identity}/revisions/1"
    app = _app(s)
    with TestClient(app) as http:
        s.draft()
        assert s.port.mutate(s.publish()).outcome == "completed"
        retired = http.post(
            revision + "/retirements",
            json={"mode": "retire"},
            headers={"Idempotency-Key": "http-retire-key"},
        )
        assert retired.status_code == 200, retired.text
        body = retired.json()
        assert body["publication"]["status"] == "retired"
        assert body["publication"]["retired_at"] is not None
        assert body["publication"]["disabled_reason"] is None
        assert http.get(fixed).json() == body
        assert http.get(PUBLIC).json()["data"] == []
        assert http.get(PUBLIC_V2).json()["data"] == []
        assert (
            s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0][
                "current_revision"
            ]
            is None
        )
        replay = http.post(
            revision + "/retirements",
            json={"mode": "retire"},
            headers={"Idempotency-Key": "http-retire-key"},
        )
        assert replay.status_code == 200 and replay.json() == body
        conflict = http.post(
            revision + "/retirements",
            json={"mode": "disable", "reason": "changed intent"},
            headers={"Idempotency-Key": "http-retire-key"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        for invalid in (
            {"mode": "remove"},
            {"mode": "disable"},
            {"mode": "disable", "reason": ""},
            {"mode": "retire", "unknown": True},
        ):
            invalid_response = http.post(
                revision + "/retirements",
                json=invalid,
                headers={"Idempotency-Key": "http-invalid-retirement"},
            )
            assert invalid_response.status_code == 400, invalid_response.text
        terminal = http.post(
            revision + "/retirements",
            json={"mode": "retire"},
            headers={"Idempotency-Key": "http-terminal-key"},
        )
        assert terminal.status_code == 409
        assert terminal.json()["error"]["code"] == "profile_not_published"
        missing = http.post(
            f"/v1/nexus/admin/profiles/{identity}/revisions/99/retirements",
            json={"mode": "retire"},
            headers={"Idempotency-Key": "http-missing-key"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "profile_not_found"


def test_actual_http_disable_records_the_reason_and_never_selects_a_replacement(
    sandbox: Sandbox,
) -> None:
    s = sandbox
    identity = profile_request()["profile_id"]
    with TestClient(_app(s)) as http:
        s.draft()
        assert s.port.mutate(s.publish()).outcome == "completed"
        s.draft("2")
        assert s.port.mutate(s.publish("2", "1", "http-publish-2")).outcome == "completed"
        disabled = http.post(
            f"/v1/nexus/admin/profiles/{identity}/revisions/2/retirements",
            json={"mode": "disable", "reason": "synthetic operator disable"},
            headers={"Idempotency-Key": "http-disable-key"},
        )
        assert disabled.status_code == 200, disabled.text
        body = disabled.json()
        assert body["publication"]["status"] == "disabled"
        assert body["publication"]["retired_at"] is None
        assert body["publication"]["disabled_reason"] == "synthetic operator disable"
        assert (
            s.admin.execute("SELECT current_revision FROM speech_profile_families").fetchall()[0][
                "current_revision"
            ]
            is None
        )
        # Disabling the current revision clears the default pointer without
        # silently selecting revision 1. Both revisions keep their published
        # history: revision 1 was never retired and revision 2 stays readable.
        assert [item["revision"] for item in http.get(PUBLIC).json()["data"]] == ["1"]
        assert [
            (item["revision"], item["current_revision"])
            for item in http.get(PUBLIC_V2).json()["data"]
        ] == [("1", None)]
        assert http.get(f"{PUBLIC}/{identity}/revisions/2").json()["publication"] == {
            "status": "disabled",
            "published_at": body["publication"]["published_at"],
            "retired_at": None,
            "disabled_reason": "synthetic operator disable",
        }
        activation = http.post(
            f"/v1/nexus/admin/profiles/{identity}/revisions/2/activations",
            json={"expected_current_revision": None},
            headers={"Idempotency-Key": "http-activate-disabled"},
        )
        assert activation.status_code == 409
        assert activation.json()["error"]["code"] == "profile_not_published"


def test_retirement_path_produces_its_declared_405_and_lifecycle_unknown(
    sandbox: Sandbox,
) -> None:
    """The declared 405/503 are produced by the real exit, not a framework default."""
    identity = profile_request()["profile_id"]
    path = f"/v1/nexus/admin/profiles/{identity}/revisions/1/retirements"
    with TestClient(_app(sandbox)) as http:
        method = http.get(path)
        assert method.status_code == 405, method.text
        assert method.headers["Allow"] == "POST"
        assert method.json()["error"]["code"] == "profile_method_not_allowed"
        assert method.json()["error"]["request_id"] == method.headers["X-Request-ID"]
        lifecycle_response(
            "retireProfileRevision",
            405,
            {"body": method.json(), "headers": dict(method.headers)},
        )
    # No provider: the shared mutation ingress answers the declared 503 before
    # any body parse, so the operation cannot fail as an unvalidated 500.
    with TestClient(_app()) as http:
        unavailable = http.post(
            path, json={"mode": "retire"}, headers={"Idempotency-Key": "unconfigured-retire"}
        )
        assert unavailable.status_code == 503, unavailable.text
        body = unavailable.json()
        assert body["error"]["code"] == "profile_result_unknown"
        assert body["error"]["retryable"] is False
        lifecycle_response(
            "retireProfileRevision",
            503,
            {"body": body, "headers": dict(unavailable.headers)},
        )
