"""Serialize local engine sends with fencing, without holding database transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

from speechnexus_gateway.domain.fencing import (
    POOLS,
    BarrierRequest,
    BarrierSnapshot,
    IngressFence,
    IngressUnavailableError,
)
from speechnexus_gateway.domain.ports import BarrierLedger, IngressOwnership
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionIdentity,
)


@dataclass
class _PoolIngress:
    sending: Lock = field(default_factory=Lock)
    minimum: int = 0


class GatewayIngress:
    """One node-local gateway identity, shared by every submit and seal consumer.

    The owner must retain its process lease throughout each hold, including external I/O.
    Replacement processes cannot share that identity while an old sender can still resume.
    Multiple gateways use distinct identities/credentials; this is not global admission.
    """

    def __init__(self, ownership: IngressOwnership, *, timeout_seconds: float = 3) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("ingress waits must be bounded")
        ownership.bind_ingress()
        self._ownership = ownership
        self._timeout = timeout_seconds
        self._pools = {pool: _PoolIngress() for pool in POOLS}
        self._state_lock = Lock()

    @property
    def gateway_id(self) -> str:
        return self._ownership.gateway_id

    @property
    def node_id(self) -> str:
        return self._ownership.node_id

    def check_ownership(self) -> None:
        with self._ownership.hold():
            pass

    @contextmanager
    def dispatch(self, identity: SubmissionIdentity) -> Iterator[None]:
        if identity.pool_id not in self._pools:
            raise InvalidExecutionRequestError("unknown ingress pool")
        if (identity.host_node_id, identity.gateway_id) != (
            self._ownership.node_id,
            self._ownership.gateway_id,
        ):
            raise InvalidExecutionRequestError("execution belongs to another gateway or node")
        with self._ownership.hold():
            lane = self._pools[identity.pool_id]
            if not lane.sending.acquire(timeout=self._timeout):
                raise IngressUnavailableError("gateway has not finished a local send")
            try:
                with self._state_lock:
                    if identity.fencing_token < lane.minimum:
                        raise GatewayBarrierFencedError("gateway already rejects this token")
                yield
            finally:
                lane.sending.release()

    def seal(self, request: BarrierRequest, ledger: BarrierLedger) -> BarrierSnapshot:
        if request.abort_in_flight:
            # There is not yet an engine abort adapter. A fence alone cannot honor an
            # abort request or manufacture a stop fact. False supports enumerate/reconcile.
            raise ExecutionCleanupPendingError("abort_in_flight requires a qualified abort adapter")
        with self._ownership.hold():
            lane = self._pools[request.pool_id]
            with self._state_lock:
                if request.min_accepted_fencing_token < lane.minimum:
                    raise StaleFencingTokenError("ingress minimum cannot decrease")
                lane.minimum = request.min_accepted_fencing_token
            # Raise the floor before waiting, so new stale sends do not extend the wait.
            # A timeout or persistence ambiguity retains that floor and returns no proof.
            if not lane.sending.acquire(timeout=self._timeout):
                raise IngressUnavailableError("old local dispatch has not settled")
            try:
                return ledger.seal_barrier(
                    IngressFence(
                        self._ownership.gateway_id,
                        self._ownership.node_id,
                        request,
                    )
                )
            finally:
                lane.sending.release()
