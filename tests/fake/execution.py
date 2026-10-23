"""Submission ledger and engine doubles that never contact a real engine."""

from __future__ import annotations

import copy
import json
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.submission import DispatchResult, SubmissionIntent


class InMemorySubmissionLedger:
    """Keeps copies so tests observe durable state instead of live objects."""

    def __init__(self) -> None:
        self._intents: dict[str, SubmissionIntent] = {}

    def get(self, submit_intent_id: str) -> SubmissionIntent | None:
        stored = self._intents.get(submit_intent_id)
        return copy.deepcopy(stored) if stored is not None else None

    def add(self, intent: SubmissionIntent, *, request_document: str) -> None:
        key = intent.identity.submit_intent_id
        if key in self._intents:
            raise ValueError(f"submit intent {key} already stored")
        if command_digest(json.loads(request_document)) != intent.request_digest:
            raise ValueError("request document does not match the recorded request digest")
        self._intents[key] = copy.deepcopy(intent)

    def save(self, intent: SubmissionIntent, *, expected_version: int) -> None:
        key = intent.identity.submit_intent_id
        stored = self._intents.get(key)
        if stored is None:
            raise ValueError(f"submit intent {key} is not stored")
        if stored.version != expected_version:
            raise ValueError(f"submit intent {key} version conflict")
        self._intents[key] = copy.deepcopy(intent)


class ScriptedEngine:
    """Engine double with scripted dispatch results and observations."""

    def __init__(
        self, *, before_dispatch: Callable[[SubmissionIntent], None] | None = None
    ) -> None:
        self.dispatch_count = 0
        self.dispatched: list[SubmissionIntent] = []
        self._outcomes: deque[DispatchOutcome] = deque()
        self._observations: dict[str, EngineObservation] = {}
        self._before_dispatch = before_dispatch

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
            observed_at=datetime.now(UTC),
            engine_request_id=f"engine-request-{intent.identity.submit_intent_id}",
        )

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        return self._observations.get(intent.identity.submit_intent_id)
