"""Bounded transaction adapter for a binding-disable coordinator."""

from __future__ import annotations

import time
from typing import Any, cast

from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.domain.runtime_intents import identifier, integer
from speechnexus_backend.speech.infrastructure.binding_disable_targets_postgres import (
    TargetCursor,
    capture_disable_targets,
    list_disable_event_ids,
    list_disable_targets,
    record_abort_intent,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresDisableCoordinatorStore:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Disable coordinator requires a bounded storage timeout")
        self._pool, self._timeout = pool, timeout_seconds

    def event_ids(self, after: str | None, limit: int) -> list[str]:
        return cast(list[str], self._read(list_disable_event_ids, after=after, limit=limit))

    def targets(
        self, event_id: str, after: str | None, limit: int, gateway_id: str, node_id: str
    ) -> list[DisableExecutionTarget]:
        return cast(
            list[DisableExecutionTarget],
            self._read(
                list_disable_targets,
                event_id,
                after=after,
                limit=limit,
                gateway_id=gateway_id,
                node_id=node_id,
            ),
        )

    def leases(
        self, event_id: str, gateway_id: str, node_id: str, after: str | None, limit: int
    ) -> list[str]:
        identifier(event_id)
        identifier(gateway_id)
        identifier(node_id)
        integer(limit, minimum=1, maximum=128)
        if after is not None:
            identifier(after)

        def read(cursor: TargetCursor) -> list[str]:
            cursor.execute(
                "SELECT l.lease_id FROM speech_binding_disable_intents i "
                "JOIN speech_publication_lifecycle_events e ON e.event_id=i.event_id "
                "JOIN admission_attempts a ON a.binding_revision_id=e.revision_id "
                "JOIN admission_leases l ON l.operation_id=a.operation_id "
                "AND l.attempt_id=a.attempt_id WHERE i.event_id=%s "
                "AND e.action='model_binding.disable' AND l.gateway_id=%s "
                "AND l.host_node_id=%s AND l.lease_state<>'released' "
                "AND (%s::text IS NULL OR l.lease_id>%s) ORDER BY l.lease_id LIMIT %s",
                (event_id, gateway_id, node_id, after, after, limit),
            )
            leases = [row["lease_id"] for row in cursor.fetchall()]
            for lease_id in leases:
                identifier(lease_id)
            return leases

        return cast(list[str], self._read(read))

    def capture(
        self, event_id: str, wait_timeout_ms: int, limit: int, gateway_id: str, node_id: str
    ) -> None:
        with self._pool.connection(timeout=self._timeout) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(self._timeout * 1000)),),
            )
            with connection.cursor() as cursor:
                capture_disable_targets(
                    cast(TargetCursor, cursor),
                    event_id,
                    wait_timeout_ms=wait_timeout_ms,
                    limit=limit,
                    gateway_id=gateway_id,
                    node_id=node_id,
                )

    def begin_abort(self, event_id: str, submit_intent_id: str) -> bool:
        with self._pool.connection(timeout=self._timeout) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(self._timeout * 1000)),),
            )
            with connection.cursor() as cursor:
                inserted = record_abort_intent(
                    cast(TargetCursor, cursor),
                    event_id,
                    submit_intent_id,
                    recorded_at_us=time.time_ns() // 1000,
                )
        return inserted  # Only a confirmed commit authorizes the outbound call.

    def _read(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        with self._pool.connection(timeout=self._timeout) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(self._timeout * 1000)),),
            )
            with connection.cursor() as cursor:
                return operation(cast(TargetCursor, cursor), *args, **kwargs)
