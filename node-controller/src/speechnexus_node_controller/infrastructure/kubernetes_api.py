"""Minimal Kubernetes pod API client for the restricted node controller.

Only the operations the controller needs are exposed, and every request is fixed
by the caller: this module never accepts an arbitrary namespace, an arbitrary
verb, or an unapproved pod manifest. The controller talks to the API server with
the ServiceAccount credential it runs under, because that credential is the
identity the documented Role grants pod permissions to; a kubeconfig mounted from
the host would be a second, unaccountable credential on disk and is not read.

The credential's own subject is checked against the configured namespace and
ServiceAccount during construction: launching as another identity would otherwise
start cleanly and fail every request later with ``403``.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

SERVICE_ACCOUNT_DIR: Final = Path("/var/run/secrets/kubernetes.io/serviceaccount")
SERVICE_ACCOUNT_TOKEN_PATH: Final = SERVICE_ACCOUNT_DIR / "token"
SERVICE_ACCOUNT_CA_BUNDLE_PATH: Final = SERVICE_ACCOUNT_DIR / "ca.crt"
DEFAULT_API_PORT: Final = 443


class KubernetesUnavailableError(Exception):
    """The API server could not be reached, or the node credential is unusable."""


class KubernetesApiError(Exception):
    """The API server answered with an error status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Kubernetes API answered {status_code}: {message}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PodState:
    """The subset of pod facts the controller reasons about.

    ``uid`` is the pod's immutable identity: a pod of the same name recreated
    after deletion receives a new one, which is what makes stop fencing possible.
    """

    name: str
    uid: str
    phase: str
    labels: dict[str, str]
    annotations: dict[str, str]
    ready: bool
    pod_ip: str | None
    image: str | None
    exit_code: int | None = None


class KubernetesPodApi(Protocol):
    """The API server surface the node runtime adapter depends on."""

    def ping(self) -> None: ...

    def get_pod(self, name: str) -> PodState | None: ...

    def create_pod(self, manifest: Mapping[str, Any]) -> PodState: ...


@dataclass(frozen=True, slots=True)
class KubernetesApiSettings:
    """Where the in-cluster API server is and which identity must answer."""

    namespace: str
    service_account_name: str
    request_timeout_seconds: float = 10.0
    # Tests and future out-of-cluster diagnostics pass an explicit endpoint and
    # transport; the default is the in-cluster service address only.
    api_server_url: str | None = None
    token_path: Path = SERVICE_ACCOUNT_TOKEN_PATH
    ca_bundle_path: Path = SERVICE_ACCOUNT_CA_BUNDLE_PATH


class KubernetesApi:
    """Synchronous API server client bound to one namespace and one identity."""

    def __init__(
        self,
        *,
        settings: KubernetesApiSettings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._namespace = settings.namespace
        token = _read_service_account_token(settings.token_path)
        _require_expected_service_account(token, settings)
        # The endpoint is resolved before the TLS material so a deployment that is
        # not running in a cluster reports that fact rather than a missing bundle.
        base_url = settings.api_server_url or _in_cluster_api_server_url()
        if transport is None:
            transport = httpx.HTTPTransport(verify=_ca_bundle(settings.ca_bundle_path))
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KubernetesApi:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def ping(self) -> None:
        """Verify that the API server answers with this credential."""
        self._send("GET", "/version")

    @property
    def namespace(self) -> str:
        return self._namespace

    def get_pod(self, name: str) -> PodState | None:
        payload = self._send_optional_json("GET", self._pod_path(name))
        if payload is None:
            return None
        return _pod_state(payload)

    def create_pod(self, manifest: Mapping[str, Any]) -> PodState:
        payload = self._send_json(
            "POST", f"/api/v1/namespaces/{self._namespace}/pods", body=dict(manifest)
        )
        return _pod_state(payload)

    def _pod_path(self, name: str) -> str:
        return f"/api/v1/namespaces/{self._namespace}/pods/{name}"

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request and raise for any error status."""
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as error:
            raise KubernetesUnavailableError(
                f"kubernetes api request failed: {type(error).__name__}"
            ) from error
        if response.status_code >= 400:
            raise KubernetesApiError(response.status_code, _error_message(response))
        return response

    def _send_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._send(method, path, body=body)
        payload = _decode_json(response)
        if payload is None:
            raise KubernetesApiError(response.status_code, "response carried no object")
        return payload

    def _send_optional_json(self, method: str, path: str) -> Any:
        """Send one read-only request, mapping a missing resource to ``None``."""
        try:
            return self._send_json(method, path)
        except KubernetesApiError as error:
            if error.status_code == 404:
                return None
            raise


def _decode_json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as error:
        raise KubernetesApiError(response.status_code, "response is not JSON") from error


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return str(payload["message"])[:200]
    return response.text[:200]


def _pod_state(payload: Any) -> PodState:
    """Project the pod fields the controller reasons about, refusing ambiguity."""
    if not isinstance(payload, dict):
        raise KubernetesApiError(200, "pod response is not an object")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise KubernetesApiError(200, "pod response carries no metadata")
    name = metadata.get("name")
    uid = metadata.get("uid")
    if not isinstance(name, str) or not name:
        raise KubernetesApiError(200, "pod response carries no name")
    if not isinstance(uid, str) or not uid:
        # Every fencing decision is anchored on the UID; a pod without one cannot
        # be stopped safely later, so it is never treated as an observation.
        raise KubernetesApiError(200, "pod response carries no uid")
    status = payload.get("status")
    spec = payload.get("spec")
    return PodState(
        name=name,
        uid=uid,
        phase=str(status.get("phase", "")) if isinstance(status, dict) else "",
        labels=_string_map(metadata.get("labels")),
        annotations=_string_map(metadata.get("annotations")),
        ready=_is_ready(status),
        pod_ip=(
            status.get("podIP")
            if isinstance(status, dict) and isinstance(status.get("podIP"), str)
            else None
        ),
        image=_container_image(spec),
        exit_code=_exit_code(status),
    )


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _is_ready(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(condition, dict)
        and condition.get("type") == "Ready"
        and condition.get("status") == "True"
        for condition in conditions
    )


def _container_image(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return None
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        return None
    first = containers[0]
    image = first.get("image") if isinstance(first, dict) else None
    return image if isinstance(image, str) else None


def _exit_code(status: Any) -> int | None:
    """The engine container's terminated exit code, when the API reports one."""
    if not isinstance(status, dict):
        return None
    container_statuses = status.get("containerStatuses")
    if not isinstance(container_statuses, list) or not container_statuses:
        return None
    first = container_statuses[0]
    if not isinstance(first, dict):
        return None
    state = first.get("state")
    terminated = state.get("terminated") if isinstance(state, dict) else None
    code = terminated.get("exitCode") if isinstance(terminated, dict) else None
    return code if isinstance(code, int) else None


def _read_service_account_token(path: Path) -> str:
    """Read the mounted credential; an absent mount is not a workable deployment."""
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise KubernetesUnavailableError(
            "the controller has no mounted ServiceAccount token; run it as a pod "
            "with a ServiceAccount rather than mounting a kubeconfig"
        ) from error
    if not token:
        raise KubernetesUnavailableError("the mounted ServiceAccount token is empty")
    return token


def _ca_bundle(path: Path) -> ssl.SSLContext:
    """The cluster CA the API server's serving certificate is verified against."""
    try:
        return ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError) as error:
        raise KubernetesUnavailableError(
            "the controller has no usable ServiceAccount CA bundle"
        ) from error


def _require_expected_service_account(token: str, settings: KubernetesApiSettings) -> None:
    """Refuse a credential that would not be the identity the Role grants.

    The API server answers ``403`` on the first write when the pod runs as another
    ServiceAccount, so a controller that cannot act would otherwise look healthy
    until an engine is needed.
    """
    subject = _token_subject(token)
    expected = f"system:serviceaccount:{settings.namespace}:{settings.service_account_name}"
    if subject != expected:
        raise KubernetesUnavailableError(
            "the mounted ServiceAccount credential belongs to "
            f"{subject or 'an unknown identity'}, not {expected}"
        )


def _token_subject(token: str) -> str | None:
    """The token's ``sub`` claim; the JWT signature is not re-verified locally."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    try:
        padded = segment + "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    return subject if isinstance(subject, str) and subject else None


def _in_cluster_api_server_url() -> str:
    """The service address every pod receives from the kubelet."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", str(DEFAULT_API_PORT))
    if not host:
        raise KubernetesUnavailableError(
            "KUBERNETES_SERVICE_HOST is not set; the Kubernetes driver only runs in-cluster"
        )
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"https://{authority}"
