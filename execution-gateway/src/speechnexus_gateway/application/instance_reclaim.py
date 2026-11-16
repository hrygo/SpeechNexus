"""Report whether an idle on-demand instance generation may be reclaimed.

This is the reporting half of the reclaim order, exactly like
:class:`~speechnexus_gateway.application.instance_drain.InstanceDrainService`: it reads
recorded facts, applies :func:`~speechnexus_gateway.domain.instance_reclaim.decide` and
writes nothing. The Scheduler still stops the instance and releases its capacity; this
service only answers "is it idle enough to stop" and "is the stop it asked for verified".

Read failures are never folded into "not idle". A store that cannot answer, or a stop
producer that answers with something other than release evidence, propagates: reporting a
benign held decision for a broken read is how a reclaim failure would vanish.

The voice-design release gate is the only producer of the ``reclaimed`` answer, so a
generation whose stop was "confirmed" by an engine sleep acknowledgment stays held with
``release_unconfirmed`` instead of releasing a reservation nobody released.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.instance_drain import InstanceIdentity
from speechnexus_gateway.domain.instance_reclaim import (
    ReclaimDecision,
    ReclaimRecord,
    ReclaimRequest,
    decide,
)
from speechnexus_gateway.domain.ports import (
    InstanceReclaimFactsReader,
    InstanceReleaseEvidencePort,
)
from speechnexus_gateway.domain.submission import (
    InstanceGenerationMismatchError,
    StaleFencingTokenError,
)
from speechnexus_gateway.domain.voice_design_release import VoiceDesignReleaseEvidence


@dataclass(frozen=True, slots=True)
class ReclaimSnapshot:
    """One reclaim answer for one recorded instance generation."""

    request: ReclaimRequest
    identity: InstanceIdentity
    record: ReclaimRecord
    decision: ReclaimDecision
    observed_at: datetime

    def value(self) -> dict[str, object]:
        """The stable, secret-free rendering; it authorizes nothing by itself."""
        return {
            "instance_id": self.identity.instance_id,
            "instance_generation": self.identity.instance_generation,
            "pool_id": self.identity.pool_id,
            "loading_policy": self.record.loading_policy,
            **self.decision.value(),
        }


class InstanceReclaimService:
    """Owns the reclaim decision; it stops nothing and releases no capacity."""

    def __init__(
        self,
        *,
        ingress: GatewayIngress,
        facts: InstanceReclaimFactsReader,
        releases: InstanceReleaseEvidencePort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ingress = ingress
        self._facts = facts
        self._releases = releases
        self._now = now or (lambda: datetime.now(UTC))

    def evaluate(self, request: ReclaimRequest) -> ReclaimSnapshot:
        """Return the freshness-checked reclaim decision for one instance generation."""
        self._ingress.check_ownership()
        identity, record = self._verify_authority(request)
        release = self._release(record, identity)
        decision = decide(record.facts(cooldown_ms=request.cooldown_ms, release=release))
        return ReclaimSnapshot(
            request=request,
            identity=identity,
            record=record,
            decision=decision,
            observed_at=self._now(),
        )

    def _verify_authority(self, request: ReclaimRequest) -> tuple[InstanceIdentity, ReclaimRecord]:
        """Reject a request naming another node/pool/gateway or a stale fencing token.

        The identity and the facts come from one read, so the token floor and the
        inventory the decision uses cannot describe two different generations.
        """
        if request.host_node_id != self._ingress.node_id:
            raise InstanceGenerationMismatchError("the reclaim names another host node")
        identity = InstanceIdentity(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            host_node_id=request.host_node_id,
            pool_id=request.pool_id,
            gateway_id=self._ingress.gateway_id,
        )
        record = self._facts.read_reclaim_record(identity)
        if record is None:
            raise InstanceGenerationMismatchError(
                "the reclaim does not match this gateway's recorded instance generation"
            )
        if request.fencing_token < max(
            record.last_fencing_token, self._ingress.enforced_minimum(request.pool_id)
        ):
            raise StaleFencingTokenError(
                "the reclaim carries a fencing token this gateway already rejects"
            )
        return identity, record

    def _release(
        self, record: ReclaimRecord, identity: InstanceIdentity
    ) -> VoiceDesignReleaseEvidence | None:
        return self._releases.release_evidence(
            instance_id=identity.instance_id,
            instance_generation=identity.instance_generation,
            pool_id=identity.pool_id,
            binding_revision_id=record.binding_revision_id,
            fencing_token=record.last_fencing_token,
        )
