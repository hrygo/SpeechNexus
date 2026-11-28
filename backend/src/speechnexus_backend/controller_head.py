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

#: The repair command both this module's messages and the operations doc point at.
ADOPTION_COMMAND = "speechnexus-adopt-generation"

ADOPTION_ALIGNED = "aligned"
ADOPTION_ADOPTABLE = "adoptable"
ADOPTION_REFUSED = "refused"

#: What the ledger holds as evidence that the previous generation stopped serving. The
#: first two are the node's own testimony; ``missing`` is the operator's explicit
#: acceptance of the absence of any, and it is recorded as the weaker claim it is.
PREVIOUS_STOPPED = "node_reported_stopped"
PREVIOUS_VOIDED = "node_voided_prepare"
PREVIOUS_STOP_MISSING = "missing"
PREVIOUS_STOP_EVIDENCE_KINDS = (PREVIOUS_STOPPED, PREVIOUS_VOIDED, PREVIOUS_STOP_MISSING)

_STOP_EVIDENCE_WORDS = {
    PREVIOUS_STOPPED: "stopped",
    PREVIOUS_VOIDED: "a prepare it never ran",
    PREVIOUS_STOP_MISSING: "gone with no recorded stop evidence",
}

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
    # The node's own testimony about generations it no longer serves. Absent from a
    # controller built before the testimony was exposed, and an absent list is read as
    # "no testimony", never as "nothing was stopped": the repair that cites it must
    # then be told explicitly that it is accepting a generation without stop evidence.
    stopped_generations: tuple[int, ...] = ()
    voided_generations: tuple[int, ...] = ()
    last_fencing_token: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationAdoptionDecision:
    """Whether the ledger may adopt the node's generation, and on what evidence.

    ``outcome`` is ``aligned`` (the two counters already agree), ``adoptable`` or
    ``refused``. Only ``adoptable`` carries ``adopted_generation`` and
    ``previous_stop_evidence``; a refusal carries a stable ``code`` and the sentence
    an operator needs.
    """

    outcome: str
    ledger_generation: int
    adopted_generation: int | None = None
    previous_stop_evidence: str | None = None
    code: str | None = None
    reason: str | None = None


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
    fencing_token = document.get("last_fencing_token")
    if fencing_token is not None and (type(fencing_token) is not int or fencing_token < 0):
        raise ControllerConsultationError(
            "controller_head_invalid", "last_fencing_token is not a non-negative integer or null"
        )
    return NodeInstanceHead(
        instance_id=str(document.get("instance_id", instance_id)),
        current_generation=current,
        generation_high_water=high_water,
        stopped_generations=_generations(document, "stopped_generations"),
        voided_generations=_generations(document, "voided_generations"),
        last_fencing_token=fencing_token,
    )


def _generations(document: Mapping[str, object], name: str) -> tuple[int, ...]:
    """One generation list from the head response, ascending and de-duplicated.

    An unanswered list is no testimony at all, which is a different fact from an
    empty one; both are represented as ``()`` here because the adoption decision asks
    only whether the ledger's generation appears. A list the node does send must be
    generations: anything else is a broken answer, not a narrower one.
    """
    value = document.get(name)
    if value is None:
        return ()
    if not isinstance(value, list) or any(type(item) is not int or item < 1 for item in value):
        raise ControllerConsultationError(
            "controller_head_invalid", f"{name} is not a list of positive integers"
        )
    return tuple(sorted(set(value)))


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
        "not serve. The ledger cannot simply be restated at generation "
        f"{head.current_generation}: that answers `previous_generation_active`, because the "
        f"ledger still holds generation {ledger_generation} as unreleased (#1282 live). "
        "Converge one of the two ways: roll the node forward (stop generation "
        f"{head.current_generation}, prepare the next one, register it — this restarts the "
        "engine) and the ledger follows, or adopt the node's generation in the ledger with "
        f"{ADOPTION_COMMAND} --instance {head.instance_id} --node {node_id} --engine-url <url>, "
        "an audited repair that retires the intervening generations. Until one is done, "
        "admission gets no usable receipt for this instance"
    )


def decide_generation_adoption(
    ledger_generation: int,
    head: NodeInstanceHead,
    *,
    node_id: str,
    accept_missing_stop_evidence: bool = False,
) -> GenerationAdoptionDecision:
    """The one place that decides whether the ledger may adopt the node's generation.

    Written once and reused: the writer (``speechnexus-adopt-generation``) executes this
    decision, and the read-only readiness report calls it to say whether the audited
    repair is available for a mismatched row. Two copies of these conditions -- one per
    caller -- would drift exactly like the two counters did.

    What the repair is not: it is not a way around ``previous_generation_active``. The
    registration path refuses a higher generation because *the ledger* has no stop
    evidence for the current one, and it is right to: it is an operator attestation
    with no node testimony. This decision substitutes a different, checkable evidence --
    the node's own lifecycle head saying that ledger generation ``G`` is no longer
    served -- and records it in an audit row. Where that testimony is absent the answer
    is a refusal, and continuing requires the caller to say explicitly that it accepts a
    generation without stop evidence.
    """
    if head.current_generation is None:
        return GenerationAdoptionDecision(
            ADOPTION_REFUSED,
            ledger_generation,
            code="node_has_no_intent",
            reason=(
                f"node {node_id} holds no runtime intent for {head.instance_id}, so there is "
                "no generation to adopt; register the instance where it actually runs"
            ),
        )
    adopted = head.current_generation
    if adopted == ledger_generation:
        return GenerationAdoptionDecision(
            ADOPTION_ALIGNED,
            ledger_generation,
            reason=(
                f"the ledger and node {node_id} already agree on generation {ledger_generation}; "
                "nothing to adopt"
            ),
        )
    if adopted < ledger_generation:
        return GenerationAdoptionDecision(
            ADOPTION_REFUSED,
            ledger_generation,
            code="ledger_ahead_of_node",
            reason=(
                f"the ledger is at generation {ledger_generation} but node {node_id} prepared "
                f"only generation {adopted}; adoption moves the ledger forward to what the node "
                "serves and never back, so this divergence is not repaired here -- prepare the "
                "ledger's generation on the node instead"
            ),
        )
    if head.generation_high_water is None or head.generation_high_water < adopted:
        return GenerationAdoptionDecision(
            ADOPTION_REFUSED,
            ledger_generation,
            code="node_head_incomplete",
            reason=(
                f"node {node_id} reports generation {adopted} as current but a high water of "
                f"{head.generation_high_water}; the head contradicts itself, so its testimony "
                "cannot be cited"
            ),
        )
    if not head.last_fencing_token:
        # Missing, or a zero epoch: the receipt must carry the token the node serves, and
        # an invented one would attest an epoch nobody holds. Refusing here keeps this
        # decision the only place that says whether the repair is available -- the report
        # and the writer must not answer differently.
        return GenerationAdoptionDecision(
            ADOPTION_REFUSED,
            ledger_generation,
            code="node_head_incomplete",
            reason=(
                f"node {node_id} did not report a usable fencing token for generation "
                f"{adopted} (got {head.last_fencing_token}); the adopted generation's receipt "
                "must carry the token the node serves, and an invented token would attest an "
                "epoch nobody holds"
            ),
        )
    if ledger_generation in head.stopped_generations:
        evidence = PREVIOUS_STOPPED
    elif ledger_generation in head.voided_generations:
        evidence = PREVIOUS_VOIDED
    elif accept_missing_stop_evidence:
        evidence = PREVIOUS_STOP_MISSING
    else:
        return GenerationAdoptionDecision(
            ADOPTION_REFUSED,
            ledger_generation,
            code="previous_generation_stop_unconfirmed",
            reason=(
                f"node {node_id} reports generation {adopted} as current but says nothing about "
                f"generation {ledger_generation}: it is not in stopped_generations "
                f"{list(head.stopped_generations)} or voided_generations "
                f"{list(head.voided_generations)}. Adopting it would retire a generation without "
                "evidence that it stopped serving. If the intervening generations are known to be "
                "gone (for example an operator stop nobody recorded in the ledger), pass "
                "--accept-missing-stop-evidence: the audit row then records `missing`, which is "
                "the weaker claim it actually is"
            ),
        )
    return GenerationAdoptionDecision(
        ADOPTION_ADOPTABLE,
        ledger_generation,
        adopted_generation=adopted,
        previous_stop_evidence=evidence,
        code=None,
        reason=(
            f"node {node_id} serves generation {adopted} at fencing token "
            f"{head.last_fencing_token} and reports generation {ledger_generation} as "
            f"{_STOP_EVIDENCE_WORDS[evidence]}; the ledger can adopt generation {adopted}"
        ),
    )
