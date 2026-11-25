"""A scripted Kubernetes API server for the pod and event endpoints; no cluster.

Every cluster adapter test drives the real ``KubernetesApi`` over
``httpx.MockTransport`` with this double as the server, so the assertions cover
the JSON the driver would really submit and the requests it would really make.

The double answers only the operations the controller is allowed to perform
(read one Pod, list one Pod's events, create one Pod, delete one Pod) and it
enforces the same deletion precondition the API server enforces: a delete whose
``preconditions.uid`` names another Pod identity is refused with ``409`` and
removes nothing. That is what makes "a rebuilt Pod is never deleted by the old
generation's stop" testable.

The event endpoint applies the ``fieldSelector`` it is sent, exactly as the API
server does: only events for that Pod name and that Pod UID come back. An event
recorded for an earlier Pod of the same name therefore stays invisible to a
request that names the current UID, which is what makes "the previous
generation's failure does not explain this one" testable.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApi,
    KubernetesApiSettings,
)

NAMESPACE = "speechnexus"
SERVICE_ACCOUNT = "speechnexus-node-controller"
API_URL = "https://kubernetes.test"
POD_UID = "pod-uid-1"
REPLACEMENT_POD_UID = "pod-uid-2"
_PODS = f"/api/v1/namespaces/{NAMESPACE}/pods"
_EVENTS = f"/api/v1/namespaces/{NAMESPACE}/events"


class FakeKubernetesCluster:
    """A scripted API server: one Pod per name, deletes with UID preconditions."""

    def __init__(self) -> None:
        self.pods: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        # Each entry is the DELETE the driver sent: the name and the request body.
        self.deletes: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        # Every Event document the API server holds, across all Pods.
        self.events: list[dict[str, Any]] = []
        self.gets = 0
        self.event_lists = 0
        self.create_status = 201
        self.delete_status = 200
        # A refused read answers with this status instead of the Pod, i.e. the
        # API server is reachable but this read fails.
        self.pod_read_status = 200
        self.event_status = 200
        # None means "never Ready": the readiness condition never appears.
        self.ready_after_gets: int | None = 1
        self.terminal_phase: str | None = None
        self.terminal_exit_code: int | None = None
        self.replacement_uid: str | None = None
        # Deletion behaviour. ``pods_after_delete`` counts the GETs that still
        # return the Pod after a delete is accepted; None keeps it visible
        # forever, i.e. a deletion that was requested but never completed.
        self.pods_after_delete: int | None = 1
        self.exit_code_on_delete: int | None = None
        # Removes the Pod first and answers 404, i.e. the Pod vanished between the
        # controller's read and its delete request.
        self.vanish_before_delete = False
        # Replaces the Pod's UID just before the delete is evaluated, i.e. the Pod
        # was rebuilt between the controller's read and its delete request.
        self.rebuild_before_delete = False
        self._remaining: dict[str, int | None] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.35.5"})
        if request.method == "POST" and path == _PODS:
            return self._create_pod(request)
        if request.method == "GET" and path == _EVENTS:
            return self._list_events(request)
        if path.startswith(_PODS + "/"):
            name = path.rsplit("/", 1)[-1]
            if request.method == "GET":
                return self._get_pod(name)
            if request.method == "DELETE":
                return self._delete_pod(name, request)
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def record_event(
        self,
        name: str,
        *,
        reason: str,
        event_type: str = "Warning",
        uid: str = POD_UID,
        message: str = "",
    ) -> None:
        """Add one Event document the API server will serve for that Pod.

        ``message`` is free text other cluster components write; tests use it to
        prove that none of it reaches an observation.
        """
        self.events.append(
            {
                "apiVersion": "v1",
                "kind": "Event",
                "metadata": {"name": f"{name}.{len(self.events)}", "namespace": NAMESPACE},
                "involvedObject": {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "namespace": NAMESPACE,
                    "name": name,
                    "uid": uid,
                },
                "reason": reason,
                "type": event_type,
                "message": message,
            }
        )

    def _list_events(self, request: httpx.Request) -> httpx.Response:
        self.event_lists += 1
        if self.event_status >= 400:
            return httpx.Response(self.event_status, json={"message": "event list rejected"})
        selectors = _field_selector(request.url.params.get("fieldSelector"))
        items = [event for event in self.events if _matches(event, selectors)]
        return httpx.Response(200, json={"apiVersion": "v1", "kind": "EventList", "items": items})

    def _create_pod(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.created.append(body)
        if self.create_status >= 400:
            return httpx.Response(self.create_status, json={"message": "pod create rejected"})
        pod = pod_from_manifest(body)
        self.pods[body["metadata"]["name"]] = pod
        return httpx.Response(201, json=pod)

    def _get_pod(self, name: str) -> httpx.Response:
        self.gets += 1
        if self.pod_read_status >= 400:
            return httpx.Response(self.pod_read_status, json={"message": "pod read rejected"})
        pod = self.pods.get(name)
        if pod is None:
            return httpx.Response(404, json={"message": "not found", "reason": "NotFound"})
        self._advance(pod)
        if name in self._remaining:
            remaining = self._remaining[name]
            if remaining is not None:
                if remaining <= 0:
                    self._finish_deletion(name)
                    return httpx.Response(404, json={"message": "not found", "reason": "NotFound"})
                self._remaining[name] = remaining - 1
            self._mark_terminating(pod)
        return httpx.Response(200, json=pod)

    def _delete_pod(self, name: str, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.deletes.append({"name": name, "body": body})
        if self.delete_status >= 400:
            return httpx.Response(self.delete_status, json={"message": "delete rejected"})
        if self.vanish_before_delete:
            self._finish_deletion(name)
            return httpx.Response(404, json={"message": "not found", "reason": "NotFound"})
        pod = self.pods.get(name)
        if pod is None:
            return httpx.Response(404, json={"message": "not found", "reason": "NotFound"})
        if self.rebuild_before_delete:
            pod["metadata"]["uid"] = REPLACEMENT_POD_UID
        precondition = body.get("preconditions")
        uid = precondition.get("uid") if isinstance(precondition, Mapping) else None
        if uid is not None and uid != pod["metadata"]["uid"]:
            # The real API server also refuses this: the Pod was rebuilt under the
            # same name, so the pinned UID no longer identifies it.
            return httpx.Response(409, json={"message": "Precondition failed: UID"})
        self._remaining[name] = self.pods_after_delete
        return httpx.Response(200, json={"apiVersion": "v1", "kind": "Status", "status": "Success"})

    def _finish_deletion(self, name: str) -> None:
        self.pods.pop(name, None)
        self._remaining.pop(name, None)

    def _mark_terminating(self, pod: dict[str, Any]) -> None:
        status = pod["status"]
        status["conditions"] = []
        if self.exit_code_on_delete is None:
            status["phase"] = "Running"
            return
        code = self.exit_code_on_delete
        status["phase"] = "Succeeded" if code == 0 else "Failed"
        status["containerStatuses"] = [
            {"name": "engine", "state": {"terminated": {"exitCode": code}}}
        ]

    def _advance(self, pod: dict[str, Any]) -> None:
        status = pod["status"]
        if self.replacement_uid is not None:
            pod["metadata"]["uid"] = self.replacement_uid
        if self.terminal_phase is not None:
            status["phase"] = self.terminal_phase
            status["conditions"] = []
        if self.terminal_exit_code is not None:
            status["containerStatuses"] = [
                {"name": "engine", "state": {"terminated": {"exitCode": self.terminal_exit_code}}}
            ]
        if (
            self.terminal_phase is None
            and self.ready_after_gets is not None
            and self.gets >= self.ready_after_gets
        ):
            status["phase"] = "Running"
            status["conditions"] = [{"type": "Ready", "status": "True"}]
            status["podIP"] = "10.0.0.7"


def pod_from_manifest(body: Mapping[str, Any]) -> dict[str, Any]:
    """The Pod document the API server would store for one submitted manifest."""
    metadata = body["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "uid": POD_UID,
            "labels": dict(metadata.get("labels", {})),
            "annotations": dict(metadata.get("annotations", {})),
        },
        "spec": body["spec"],
        "status": {"phase": "Pending", "conditions": []},
    }


def mark_terminated(pod: dict[str, Any], exit_code: int) -> None:
    """Script one Pod as a terminated engine container with that exit code."""
    pod["status"]["phase"] = "Succeeded" if exit_code == 0 else "Failed"
    pod["status"]["conditions"] = []
    pod["status"]["containerStatuses"] = [
        {"name": "engine", "state": {"terminated": {"exitCode": exit_code}}}
    ]


def mark_running(pod: dict[str, Any], *, ready: bool) -> None:
    """Script one Pod as running, with or without its Ready condition."""
    pod["status"]["phase"] = "Running"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True" if ready else "False"}]
    if ready:
        pod["status"]["podIP"] = "10.0.0.7"


def mark_deleting(pod: dict[str, Any], at: str = "2026-11-25T00:00:00Z") -> None:
    """Script one Pod as deleted but still served, as the API server reports it."""
    pod["metadata"]["deletionTimestamp"] = at
    pod["metadata"]["deletionGracePeriodSeconds"] = 20


def _field_selector(raw: str | None) -> dict[str, str]:
    """The ``fieldSelector`` query as a mapping."""
    if not raw:
        return {}
    selectors: dict[str, str] = {}
    for term in raw.split(","):
        key, _, value = term.partition("=")
        if key and value:
            selectors[key] = value
    return selectors


def _matches(event: Mapping[str, Any], selectors: Mapping[str, str]) -> bool:
    """Whether one Event document satisfies every selector the client sent."""
    involved = event.get("involvedObject")
    fields = involved if isinstance(involved, Mapping) else {}
    for key, expected in selectors.items():
        path = key.split(".")
        if len(path) != 2 or path[0] != "involvedObject":
            return False
        if str(fields.get(path[1], "")) != expected:
            return False
    return True


def service_account_token(subject: str | None = None) -> str:
    """A bound-token shaped credential: header.payload.signature."""
    identity = subject or f"system:serviceaccount:{NAMESPACE}:{SERVICE_ACCOUNT}"
    payload = json.dumps({"sub": identity}, separators=(",", ":")).encode()
    segment = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{segment}.signature"


def api_for(tmp_path: Path, cluster: FakeKubernetesCluster) -> KubernetesApi:
    """The real client, pointed at one scripted API server."""
    token_path = tmp_path / "token"
    token_path.write_text(service_account_token(), encoding="ascii")
    return KubernetesApi(
        settings=KubernetesApiSettings(
            namespace=NAMESPACE,
            service_account_name=SERVICE_ACCOUNT,
            api_server_url=API_URL,
            token_path=token_path,
        ),
        transport=httpx.MockTransport(cluster.handler),
    )


class FakeClock:
    """A monotonic clock the tests advance instead of sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
