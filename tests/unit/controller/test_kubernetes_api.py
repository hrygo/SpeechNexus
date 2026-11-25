"""Exercise the actual Kubernetes JSON boundary, not just an in-memory double.

The client is the only place the controller's cluster credential is read and the
only place the documented namespace appears in a URL. These tests drive it with
``httpx.MockTransport``: no cluster, no kubeconfig, no network.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApi,
    KubernetesApiError,
    KubernetesApiSettings,
    KubernetesUnavailableError,
)

NAMESPACE = "speechnexus"
SERVICE_ACCOUNT = "speechnexus-node-controller"
API_URL = "https://kubernetes.test"


def _token(subject: str) -> str:
    """A bound-token shaped credential: header.payload.signature, base64url parts."""

    def encode(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (
            encode({"alg": "RS256"}),
            encode({"sub": subject, "iss": "kubernetes/serviceaccount"}),
            "sig",
        )
    )


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport must not be used: {request.method} {request.url}")


def _respond(payload: dict[str, Any]) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def _settings(
    tmp_path: Path, *, subject: str | None = None, token: str | None = None
) -> KubernetesApiSettings:
    path = tmp_path / "token"
    resolved = (
        token
        if token is not None
        else _token(subject or f"system:serviceaccount:{NAMESPACE}:{SERVICE_ACCOUNT}")
    )
    path.write_text(resolved, encoding="ascii")
    return KubernetesApiSettings(
        namespace=NAMESPACE,
        service_account_name=SERVICE_ACCOUNT,
        api_server_url=API_URL,
        token_path=path,
    )


def _pod_payload(*, name: str = "engine-instance-asr-1-1") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "uid": "pod-uid-1",
            "labels": {"speechnexus.io/instance-id": "instance-asr-1"},
            "annotations": {"speechnexus.io/config-digest": "sha256:" + "a" * 64},
        },
        "spec": {"containers": [{"name": "engine", "image": "local/engine@sha256:" + "c" * 64}]},
        "status": {
            "phase": "Running",
            "podIP": "10.0.0.7",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def test_the_client_creates_a_pod_in_its_own_namespace_with_the_node_credential(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json=_pod_payload())

    api = KubernetesApi(settings=_settings(tmp_path), transport=httpx.MockTransport(server))
    with api:
        pod = api.create_pod(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "engine-instance-asr-1-1", "namespace": NAMESPACE},
                "spec": {"containers": [{"name": "engine", "image": "synthetic"}]},
            }
        )

    assert pod.name == "engine-instance-asr-1-1"
    assert pod.uid == "pod-uid-1"
    assert pod.ready is True
    assert pod.pod_ip == "10.0.0.7"
    assert pod.annotations["speechnexus.io/config-digest"] == "sha256:" + "a" * 64
    sent = requests[0]
    assert sent.method == "POST"
    assert sent.url.path == f"/api/v1/namespaces/{NAMESPACE}/pods"
    assert sent.headers["Authorization"].startswith("Bearer ")


def test_a_missing_pod_is_absent_and_a_refusal_keeps_its_status(tmp_path: Path) -> None:
    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/not-there"):
            return httpx.Response(404, json={"message": 'pods "not-there" not found'})
        return httpx.Response(403, json={"message": "pods is forbidden"})

    api = KubernetesApi(settings=_settings(tmp_path), transport=httpx.MockTransport(server))
    with api:
        assert api.get_pod("not-there") is None
        with pytest.raises(KubernetesApiError) as caught:
            api.get_pod("forbidden")
    assert caught.value.status_code == 403


def test_the_client_pings_the_api_server_it_would_write_to(tmp_path: Path) -> None:
    seen: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"gitVersion": "v1.35.5"})

    api = KubernetesApi(settings=_settings(tmp_path), transport=httpx.MockTransport(server))
    with api:
        api.ping()

    assert seen == ["/version"]


def test_a_pod_without_a_uid_is_refused_instead_of_observed(tmp_path: Path) -> None:
    payload = _pod_payload()
    del payload["metadata"]["uid"]

    api = KubernetesApi(
        settings=_settings(tmp_path),
        transport=httpx.MockTransport(_respond(payload)),
    )
    with api, pytest.raises(KubernetesApiError, match="no uid"):
        api.get_pod("engine-instance-asr-1-1")


def test_a_credential_for_another_service_account_is_refused_at_construction(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, subject="system:serviceaccount:other:someone")

    with pytest.raises(KubernetesUnavailableError, match="belongs to"):
        KubernetesApi(settings=settings, transport=httpx.MockTransport(_unreachable))


def test_an_absent_service_account_mount_is_refused_at_construction(tmp_path: Path) -> None:
    settings = KubernetesApiSettings(
        namespace=NAMESPACE,
        service_account_name=SERVICE_ACCOUNT,
        api_server_url=API_URL,
        token_path=tmp_path / "missing-token",
    )

    with pytest.raises(KubernetesUnavailableError, match="mounted ServiceAccount token"):
        KubernetesApi(settings=settings, transport=httpx.MockTransport(_unreachable))


def test_an_unreadable_credential_is_refused_instead_of_trusted(tmp_path: Path) -> None:
    settings = _settings(tmp_path, token="not-a-jwt")

    with pytest.raises(KubernetesUnavailableError, match="belongs to"):
        KubernetesApi(settings=settings, transport=httpx.MockTransport(_unreachable))


def test_the_in_cluster_service_address_is_the_only_default_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    settings = KubernetesApiSettings(
        namespace=NAMESPACE,
        service_account_name=SERVICE_ACCOUNT,
        token_path=_settings(tmp_path).token_path,
    )

    with pytest.raises(KubernetesUnavailableError, match="KUBERNETES_SERVICE_HOST"):
        # No injected transport: the in-cluster address is what a real deployment
        # would use, and its absence must refuse rather than guess a host.
        KubernetesApi(settings=settings)
