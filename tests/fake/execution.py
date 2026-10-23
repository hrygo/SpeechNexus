"""Submission ledger and engine doubles that never contact a real engine."""

from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.fencing import BarrierConflictError, BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    RecordStop,
    StaleFencingTokenError,
    StopEvidenceConflictError,
    SubmissionIntent,
    VerifiedStopEvidence,
)


class InMemorySubmissionLedger:
    """Keeps copies so tests observe durable state instead of live objects."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._intents: dict[str, SubmissionIntent] = {}
        self._barriers: dict[tuple[str, str], BarrierSnapshot] = {}
        self._evidence: dict[str, VerifiedStopEvidence] = {}
        self._now = now or (lambda: datetime.now(UTC))

    def get(self, submit_intent_id: str) -> SubmissionIntent | None:
        stored = self._intents.get(submit_intent_id)
        return copy.deepcopy(stored) if stored is not None else None

    def query_outcome(self, submit_intent_id: str) -> ExecutionSnapshot | None:
        intent = self.get(submit_intent_id)
        return None if intent is None else ExecutionSnapshot(intent)

    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent:
        current = self.get(request.submit_intent_id)
        if current is None:
            raise InvalidExecutionRequestError("unknown submit intent")
        updated = request.apply(current, at=self._now())
        self._intents[request.submit_intent_id] = copy.deepcopy(updated)
        return updated

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None:
        intent.validate_new()
        barrier = self._barriers.get((intent.identity.gateway_id, intent.identity.pool_id))
        if barrier is not None and intent.identity.fencing_token < barrier.min_fencing_token:
            raise GatewayBarrierFencedError("durable barrier rejects stale submit")
        key = intent.identity.submit_intent_id
        if key in self._intents:
            raise ValueError(f"submit intent {key} already stored")
        if command_digest(json.loads(request_document)) != intent.request_digest:
            raise ValueError("request document does not match the recorded request digest")
        self._intents[key] = copy.deepcopy(intent)

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        key = request.observation.identity.submit_intent_id
        stored = self.get(key)
        if stored is None:
            raise InvalidExecutionRequestError("unknown execution observation")
        updated = request.apply(stored, at=self._now())
        self._intents[key] = copy.deepcopy(updated)
        return updated

    def record_stop(self, request: RecordStop) -> SubmissionIntent:
        key = request.evidence.submit_intent_id
        stored = self.get(key)
        if stored is None:
            raise InvalidExecutionRequestError("unknown submit intent for stop evidence")
        for evidence in self._evidence.values():
            if (
                evidence.evidence_id == request.evidence.evidence_id
                and evidence.submit_intent_id != key
            ):
                raise StopEvidenceConflictError("stop evidence belongs to another execution")
        updated = request.apply(stored, previous_evidence=self._evidence.get(key), at=self._now())
        self._evidence[key] = request.evidence
        self._intents[key] = copy.deepcopy(updated)
        return updated

    def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot:
        key = (fence.gateway_id, fence.request.pool_id)
        previous = self._barriers.get(key)
        token = fence.request.min_accepted_fencing_token
        version = 1 if previous is None else previous.version + 1
        if previous is not None:
            if previous.min_fencing_token > token:
                raise StaleFencingTokenError("durable minimum cannot decrease")
            if previous.min_fencing_token == token:
                if previous.evidence_digest != fence.evidence_digest:
                    raise BarrierConflictError("barrier has another proof")
                version = previous.version
        intents = sorted(
            (
                i
                for i in self._intents.values()
                if (i.identity.gateway_id, i.identity.pool_id) == key
            ),
            key=lambda i: i.identity.submit_intent_id,
        )
        fact = BarrierSnapshot(
            key[0],
            key[1],
            token,
            fence.evidence_digest,
            version,
            int(self._now().timestamp() * 1_000_000),
            tuple(
                i.identity.submit_intent_id
                for i in intents
                if i.execution_state.value == "submitting"
            ),
            tuple(
                i.identity.submit_intent_id
                for i in intents
                if i.execution_state.value in {"executing", "stopping"}
            ),
            tuple(
                i.identity.submit_intent_id
                for i in intents
                if i.execution_state.value == "unknown_quarantined"
            ),
        )
        self._barriers[key] = fact
        return fact


@dataclass
class FakeIngressOwnership:
    node_id: str = "node-1"
    gateway_id: str = "gateway-1"
    bound: bool = False

    def bind_ingress(self) -> None:
        if self.bound:
            raise RuntimeError("test owner already has an ingress guard")
        self.bound = True

    def hold(self) -> AbstractContextManager[None]:
        return nullcontext()


def fake_ingress(
    *, node_id: str = "node-1", gateway_id: str = "gateway-1", timeout_seconds: float = 1
) -> GatewayIngress:
    return GatewayIngress(
        FakeIngressOwnership(node_id, gateway_id), timeout_seconds=timeout_seconds
    )


class ScriptedEngine:
    """Engine double with scripted dispatch results and observations."""

    def __init__(
        self,
        *,
        before_dispatch: Callable[[SubmissionIntent], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.dispatch_count = 0
        self.dispatched: list[SubmissionIntent] = []
        self._outcomes: deque[DispatchOutcome] = deque()
        self._observations: dict[str, EngineObservation] = {}
        self._before_dispatch = before_dispatch
        self._now = now or (lambda: datetime.now(UTC))

    def script_outcome(self, outcome: DispatchOutcome) -> None:
        self._outcomes.append(outcome)

    def script_observation(self, submit_intent_id: str, observation: EngineObservation) -> None:
        self._observations[submit_intent_id] = observation

    def dispatch(self, intent: SubmissionIntent) -> DispatchOutcome:
        self.dispatch_count += 1
        self.dispatched.append(intent)
        if self._before_dispatch is not None:
            self._before_dispatch(intent)
        if self._outcomes:
            return self._outcomes.popleft()
        return DispatchOutcome(
            result=DispatchResult.ACKNOWLEDGED,
            observed_at=self._now(),
            engine_request_id=f"engine-request-{intent.identity.submit_intent_id}",
        )

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        return self._observations.get(intent.identity.submit_intent_id)
