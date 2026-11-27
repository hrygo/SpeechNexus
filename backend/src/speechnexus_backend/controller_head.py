"""Ask the node controller which generation it actually prepared.

The coordination ledger keeps ``runtime_instance_heads.current_generation`` and the
node controller keeps its own intent store; nothing makes the two numbers move
together. In the field they read 1 and 11 for the same instance while the pod serving
traffic was the eleventh, so a readiness receipt was signed for a generation no pod
served and every backend surface called it ``valid`` (#1282).

The controller is the only authority on what it prepared, so a receipt is signed or
reported only after this module has asked it. Both callers (the registration /
renewal command and the read-only report) go through one client and one comparison:
a second copy of the rule would be a second place to get the numbers wrong.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

CONTROLLER_URL_VARIABLE = "SPEECHNEXUS_CONTROLLER_API_URL"
CONTROLLER_TOKEN_VARIABLE = "SPEECHNEXUS_CONTROLLER_API_TOKEN"

_MAX_HEAD_BYTES = 65_536
_HEAD_PATH = "/internal/v1/nodes/{node_id}/instances/{instance_id}/head"


class ControllerConsultationError(Exception):
    """The controller could not answer, so no generation fact was obtained."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class NodeInstanceHead:
    """The lifecycle head the node derived for one instance."""

    instance_id: str
    current_generation: int | None
    generation_high_water: int | None


@dataclass(frozen=True, slots=True)
class ControllerEndpoint:
    url: str
    token: str


def configured_endpoint(env: Mapping[str, str]) -> ControllerEndpoint | None:
    """Read the controller endpoint, or ``None`` when the deployment has none.

    Half a configuration is an error rather than a silent skip: a URL without the
    component credential would fail every call, and skipping it quietly is how a
    deployment ends up believing it cross-checks when it never does.
    """
    url = env.get(CONTROLLER_URL_VARIABLE, "").strip()
    token = env.get(CONTROLLER_TOKEN_VARIABLE, "").strip()
    if not url and not token:
        return None
    if not url or not token:
        raise ControllerConsultationError(
            "controller_not_configured",
            f"set both {CONTROLLER_URL_VARIABLE} and {CONTROLLER_TOKEN_VARIABLE}, or neither",
        )
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ControllerConsultationError(
            "invalid_controller_url",
            f"{CONTROLLER_URL_VARIABLE} must be an http(s) origin without credentials",
        )
    return ControllerEndpoint(url.rstrip("/"), token)


def read_head(
    endpoint: ControllerEndpoint,
    *,
    node_id: str,
    instance_id: str,
    timeout_seconds: float = 5.0,
) -> NodeInstanceHead:
    """Read one instance's derived head from its node controller."""
    if not 0 < timeout_seconds <= 30:
        raise ControllerConsultationError(
            "invalid_controller_timeout", "controller timeout is out of range"
        )
    path = _HEAD_PATH.format(
        node_id=quote(node_id, safe=""), instance_id=quote(instance_id, safe="")
    )
    request = Request(
        endpoint.url + path,
        headers={"Authorization": f"Bearer {endpoint.token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            body = response.read(_MAX_HEAD_BYTES + 1)
    except HTTPError as error:
        raise ControllerConsultationError(
            "controller_refused",
            f"the controller answered {error.code} for the head of {instance_id} on {node_id}",
        ) from None
    except URLError, TimeoutError, OSError:
        raise ControllerConsultationError(
            "controller_unreachable",
            f"the controller at {endpoint.url} is unreachable from this host",
        ) from None
    if status != 200:
        raise ControllerConsultationError(
            "controller_refused",
            f"the controller answered {status} for the head of {instance_id} on {node_id}",
        )
    if len(body) > _MAX_HEAD_BYTES:
        raise ControllerConsultationError(
            "controller_head_too_large", "the head response exceeds the bounded size"
        )
    try:
        document = json.loads(body)
    except ValueError:
        raise ControllerConsultationError(
            "controller_head_invalid", "the head response is not JSON"
        ) from None
    if not isinstance(document, dict):
        raise ControllerConsultationError(
            "controller_head_invalid", "the head response is not a JSON object"
        )
    current = document.get("current_generation")
    high_water = document.get("generation_high_water")
    for name, value in (("current_generation", current), ("generation_high_water", high_water)):
        if value is not None and (type(value) is not int or value < 1):
            raise ControllerConsultationError(
                "controller_head_invalid", f"{name} is not a positive integer or null"
            )
    return NodeInstanceHead(
        instance_id=str(document.get("instance_id", instance_id)),
        current_generation=current,
        generation_high_water=high_water,
    )


def generation_disagreement(
    ledger_generation: int, head: NodeInstanceHead, *, node_id: str
) -> str | None:
    """Why the ledger's generation and the node's disagree, or ``None`` when they agree.

    A node with no intent for the instance (both generations null) is not a
    disagreement to report: nothing was prepared there, which is a different problem
    with its own answer (register the instance at all).
    """
    if head.current_generation is None:
        return None
    if head.current_generation == ledger_generation:
        return None
    return (
        f"instance {head.instance_id} is registered at generation {ledger_generation} in the "
        f"coordination ledger, but node {node_id} has prepared generation "
        f"{head.current_generation} (high water {head.generation_high_water}); a receipt "
        f"signed for generation {ledger_generation} would attest an instance that node does "
        "not serve. Register the serving generation "
        f"(`speechnexus-register-engine --generation {head.current_generation} ...`) if the "
        "node is right, or prepare generation "
        f"{ledger_generation} again if the ledger is right"
    )
