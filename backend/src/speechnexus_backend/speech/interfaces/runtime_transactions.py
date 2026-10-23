"""Internal coordination boundary; codecs are injected by the composition root."""

from collections.abc import Callable
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.application.runtime_intents import RuntimeService
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    CreateRuntimeIntent,
    RuntimeIdentity,
    RuntimeIntent,
    RuntimeResult,
    identifier,
)


class RuntimeTransactionBoundary:
    def __init__(
        self,
        service: RuntimeService,
        *,
        owner_id: str,
        decode_request: Callable[[Any], RuntimeIntent],
        render_request: Callable[[RuntimeIntent], dict[str, Any]],
        decode_observation: Callable[[str, RuntimeIntent], dict[str, Any]],
    ) -> None:
        identifier(owner_id)
        self._owner_id = owner_id
        self._service = service
        self._decode = decode_request
        self._render = render_request
        self._observation = decode_observation

    def create_runtime_intent(
        self, request: Any, *, approved_plan: ApprovedRuntimePlan | None
    ) -> dict[str, Any]:
        validate_coordination("create_runtime_intent", request)
        try:
            command = CreateRuntimeIntent(
                self._decode(request["request"]),
                int(request["expected_head_version"])
                if request["expected_head_version"] is not None
                else None,
                approved_plan,
                self._owner_id,
            )
        except ValueError:
            raise ContractValidationError("Invalid create_runtime_intent") from None
        return self._wire("create_runtime_intent", self._service.create(command))

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        target = request["target"]
        if target["kind"] != "runtime_intent":
            raise ContractValidationError("Runtime boundary only queries node/intent identities")
        identity = RuntimeIdentity(target["target_node_id"], target["intent_id"])
        return self._wire(
            "query_outcome",
            self._service.query(identity, request_digest=request.get("request_digest")),
        )

    def _wire(self, operation: str, result: RuntimeResult) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": operation,
            "outcome": result.outcome,
            "scope_id": result.identity.intent_id,
            "recovery": result.recovery,
        }
        if result.fact is not None:
            fact = result.fact
            wire["version"] = str(fact.version)
            if fact.observation_document is None:
                wire["value"] = {
                    "kind": "runtime",
                    "runtime": None,
                    "request": self._render(fact.request),
                    "intent_state": fact.intent_state,
                    "recorded_at_us": fact.recorded_at_us,
                    "updated_at_us": fact.updated_at_us,
                    "version": str(fact.version),
                }
            else:
                wire["value"] = {
                    "kind": "runtime",
                    "runtime": self._observation(fact.observation_document, fact.request),
                }
        if result.error_code is not None:
            wire["error_code"] = result.error_code
        validate_coordination("TransactionResult", wire)
        return wire
