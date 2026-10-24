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
from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandDeletionTombstone,
    CommandRedactionResult,
)
from speechnexus_gateway.domain.fencing import BarrierConflictError, BarrierSnapshot, IngressFence
from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import OutputIntervalRecord, OutputIntervalWrite
from speechnexus_gateway.domain.ports import AbortOutcome, DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot, MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    LeaseState,
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
        self._request_documents: dict[str, str | None] = {}
        self._redactions: dict[str, CommandDeletionTombstone] = {}
        self._lease_states: dict[str, LeaseState] = {}
        self._intervals: dict[str, OutputIntervalRecord] = {}
        self._stream_rates: dict[tuple[str, str, str], int] = {}
        self._instances: dict[tuple[str, int], InstanceIdentity] = {}
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
        if intent.output_stream_id is not None and intent.identity.sample_rate_hz is not None:
            stream_key = (
                intent.identity.operation_id,
                intent.identity.attempt_id,
                intent.output_stream_id,
            )
            previous_rate = self._stream_rates.get(stream_key, intent.identity.sample_rate_hz)
            if previous_rate != intent.identity.sample_rate_hz:
                raise InvalidExecutionRequestError("logical stream has another sample rate")
            self._stream_rates[stream_key] = intent.identity.sample_rate_hz
        self._intents[key] = copy.deepcopy(intent)
        self._request_documents[key] = request_document
        self._lease_states.setdefault(intent.identity.lease_id, LeaseState.RESERVED)

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

    def _barrier_fact(
        self,
        key: tuple[str, str],
        *,
        min_fencing_token: int,
        evidence_digest: str,
        version: int,
    ) -> BarrierSnapshot:
        """Rebuild one barrier fact with the inventory as it stands now."""
        intents = sorted(
            (
                intent
                for intent in self._intents.values()
                if (intent.identity.gateway_id, intent.identity.pool_id) == key
            ),
            key=lambda intent: intent.identity.submit_intent_id,
        )
        return BarrierSnapshot(
            key[0],
            key[1],
            min_fencing_token,
            evidence_digest,
            version,
            int(self._now().timestamp() * 1_000_000),
            tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value == "submitting"
            ),
            tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value in {"executing", "stopping"}
            ),
            tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value == "unknown_quarantined"
            ),
        )

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
        fact = self._barrier_fact(
            key,
            min_fencing_token=token,
            evidence_digest=fence.evidence_digest,
            version=version,
        )
        self._barriers[key] = fact
        return fact

    def get_barrier(self, gateway_id: str, pool_id: str) -> BarrierSnapshot | None:
        stored = self._barriers.get((gateway_id, pool_id))
        if stored is None:
            return None
        return self._barrier_fact(
            (gateway_id, pool_id),
            min_fencing_token=stored.min_fencing_token,
            evidence_digest=stored.evidence_digest,
            version=stored.version,
        )

    def set_test_lease_state(self, lease_id: str, state: LeaseState) -> None:
        """Synthetic fixture fact only; this fake does not implement A's release transaction."""
        self._lease_states[lease_id] = state

    def register_instance(self, identity: InstanceIdentity) -> None:
        """Synthetic fixture fact only; this fake keeps no ``engine_instances`` table."""
        self._instances[(identity.instance_id, identity.instance_generation)] = identity

    def read_instance_identity(
        self, instance_id: str, instance_generation: int
    ) -> InstanceIdentity | None:
        return self._instances.get((instance_id, instance_generation))

    def read_drain_inventory(self, identity: InstanceIdentity) -> DrainInventory:
        key = (
            identity.instance_id,
            identity.instance_generation,
            identity.host_node_id,
            identity.pool_id,
            identity.gateway_id,
        )
        intents = sorted(
            (
                intent
                for intent in self._intents.values()
                if (
                    intent.identity.instance_id,
                    intent.identity.instance_generation,
                    intent.identity.host_node_id,
                    intent.identity.pool_id,
                    intent.identity.gateway_id,
                )
                == key
            ),
            key=lambda intent: intent.identity.submit_intent_id,
        )
        return DrainInventory(
            submitting_intent_ids=tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value == "submitting"
            ),
            active_intent_ids=tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value in {"executing", "stopping"}
            ),
            quarantined_intent_ids=tuple(
                intent.identity.submit_intent_id
                for intent in intents
                if intent.execution_state.value == "unknown_quarantined"
            ),
        )

    def redact_execution_command(
        self, permit: AuthorizedCommandRedaction
    ) -> CommandRedactionResult:
        key = permit.request.submit_intent_id
        current = self.get(key)
        if current is None:
            raise InvalidExecutionRequestError("unknown command deletion execution")
        result = permit.apply(
            current,
            lease_state=self._lease_states[current.identity.lease_id],
            document_present=self._request_documents[key] is not None,
            previous=self._redactions.get(key),
            at=self._now(),
        )
        self._intents[key] = copy.deepcopy(result.intent)
        self._redactions[key] = result.tombstone
        self._request_documents[key] = None
        return result

    def append_interval(self, record: OutputIntervalRecord) -> OutputIntervalWrite:
        interval = record.interval
        current = self.get(interval.submit_intent_id)
        if current is None or record.stream_key not in self._stream_rates:
            raise InvalidExecutionRequestError("unknown execution or logical output stream")
        overlaps = any(
            stored.stream_key == record.stream_key
            and stored.interval.meter_kind is interval.meter_kind
            and stored.interval.sample_start < interval.sample_end
            and stored.interval.sample_end > interval.sample_start
            for stored in self._intervals.values()
        )
        result = record.apply(
            current,
            stream_sample_rate_hz=self._stream_rates[record.stream_key],
            previous=self._intervals.get(interval.interval_id),
            overlaps=overlaps,
            at=self._now(),
        )
        if not result.replayed:
            self._intervals[interval.interval_id] = record
            self._intents[interval.submit_intent_id] = copy.deepcopy(result.intent)
        return result

    def query_output_interval(self, interval_id: str) -> OutputIntervalRecord | None:
        return self._intervals.get(interval_id)


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


class ScriptedAbort:
    """Abort double: reports only scripted outcomes and never contacts an engine."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self._outcomes: deque[AbortOutcome] = deque()
        self._now = now or (lambda: datetime.now(UTC))

    def script_outcome(self, outcome: AbortOutcome) -> None:
        self._outcomes.append(outcome)

    def abort(self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int) -> AbortOutcome:
        self.calls.append((intent.identity.submit_intent_id, reason, wait_timeout_ms))
        if self._outcomes:
            return self._outcomes.popleft()
        # The default is the honest one: nothing was verified, so nothing is released.
        return AbortOutcome(observed_at=self._now())
