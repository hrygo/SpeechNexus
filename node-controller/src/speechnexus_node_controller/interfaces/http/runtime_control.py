"""``internal/runtime-control`` routes of the restricted node controller.

The routes only translate HTTP into the application service: node scope,
idempotency, generation/fencing and stop evidence stay in the domain and
application layers. Every route is scoped to this controller's own ``node_id``
and the service is synchronous (it inspects the local container daemon), so the
handlers run it in a worker thread instead of blocking the event loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.intents import (
    RuntimeIntentNotFoundError,
    TargetNodeMismatchError,
)
from speechnexus_node_controller.domain.payloads import intent_response_payload
from speechnexus_node_controller.interfaces.http.schemas import (
    NodeReconcileRequestPayload,
    NodeReconcileResponse,
    RuntimeControlErrorResponse,
    RuntimeInstanceIntentRequestPayload,
    RuntimeInstanceIntentResponse,
)
from speechnexus_node_controller.interfaces.http.security import ComponentCredential

_REQUEST_ID_RESPONSE_HEADER = {
    "X-Request-ID": {
        "description": "Validated client request ID or a server-generated correlation ID.",
        "schema": {"type": "string"},
    }
}


def _error_response(description: str) -> dict[str, object]:
    return {
        "description": description,
        "headers": _REQUEST_ID_RESPONSE_HEADER,
        "model": RuntimeControlErrorResponse,
    }


def create_runtime_control_router(
    *,
    service: NodeRuntimeControlService,
    credential: ComponentCredential,
) -> APIRouter:
    """Build the runtime lifecycle routes for exactly one node controller."""
    router = APIRouter(tags=["runtime-control"])

    def require_own_node(node_id: str) -> None:
        if node_id != service.node_id:
            raise TargetNodeMismatchError(
                f"the request is addressed to node {node_id}, not {service.node_id}"
            )

    @router.post(
        "/internal/v1/nodes/{node_id}/instance-intents",
        operation_id="submitRuntimeInstanceIntent",
        summary="Submit an approved instance prepare or stop intent to a target host node",
        response_model=RuntimeInstanceIntentResponse,
        dependencies=[Depends(credential)],
        responses={
            200: {"headers": _REQUEST_ID_RESPONSE_HEADER},
            401: _error_response("The per-node component credential is missing or invalid."),
            409: _error_response(
                "Stable conflict codes: `target_node_mismatch`, `stale_fencing_token`, "
                "`stale_instance_generation`, `gateway_drain_incomplete`, or `intent_conflict`."
            ),
            422: _error_response("The request failed schema or approved-template validation."),
            503: _error_response(
                "The node runtime did not let the controller complete or verify the action; "
                "the intent stays unresolved and is not reported as stopped."
            ),
        },
    )
    async def submit_runtime_instance_intent(
        node_id: str, payload: RuntimeInstanceIntentRequestPayload
    ) -> RuntimeInstanceIntentResponse:
        require_own_node(node_id)
        outcome = await run_in_threadpool(service.submit_intent, payload.to_domain())
        return RuntimeInstanceIntentResponse.model_validate(
            intent_response_payload(outcome.intent, idempotent_replay=outcome.idempotent_replay)
        )

    @router.get(
        "/internal/v1/nodes/{node_id}/instance-intents/{intent_id}",
        operation_id="getRuntimeInstanceIntent",
        summary=("Query observed container/process lifecycle state and verified stop evidence"),
        response_model=RuntimeInstanceIntentResponse,
        response_model_exclude={"idempotent_replay"},
        dependencies=[Depends(credential)],
        responses={
            200: {"headers": _REQUEST_ID_RESPONSE_HEADER},
            401: _error_response("The per-node component credential is missing or invalid."),
            404: _error_response("No runtime instance intent with that identity is recorded here."),
            422: _error_response("The path failed identifier validation."),
        },
    )
    async def get_runtime_instance_intent(
        node_id: str, intent_id: str
    ) -> RuntimeInstanceIntentResponse:
        require_own_node(node_id)
        intent = await run_in_threadpool(service.get_intent, intent_id)
        if intent is None:
            raise RuntimeIntentNotFoundError(
                f"node {node_id} has no recorded runtime instance intent {intent_id}"
            )
        return RuntimeInstanceIntentResponse.model_validate(intent_response_payload(intent))

    @router.post(
        "/internal/v1/nodes/{node_id}/reconcile",
        operation_id="reconcileNodeInstances",
        summary=(
            "Report observed local runtime instances and container exit facts for node "
            "reconciliation"
        ),
        response_model=NodeReconcileResponse,
        dependencies=[Depends(credential)],
        responses={
            200: {"headers": _REQUEST_ID_RESPONSE_HEADER},
            401: _error_response("The per-node component credential is missing or invalid."),
            409: _error_response(
                "The snapshot controller identity or a reported generation does not match "
                "this node controller's scope."
            ),
            422: _error_response("The reconciliation snapshot failed schema validation."),
        },
    )
    async def reconcile_node_instances(
        node_id: str, payload: NodeReconcileRequestPayload
    ) -> NodeReconcileResponse:
        require_own_node(node_id)
        outcome = await run_in_threadpool(service.reconcile, payload.to_domain())
        return NodeReconcileResponse(
            node_id=outcome.node_id,
            accepted_at=outcome.accepted_at,
            quarantined_instance_ids=list(outcome.quarantined_instance_ids),
        )

    return router
