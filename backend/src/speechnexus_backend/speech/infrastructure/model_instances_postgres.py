"""Read-only keyset projection of recorded runtime instance generations.

The projection reads the instance row and the immutable binding revision it
names, and nothing else. Commands, runtime intents, leases, submit intents and
stop evidence documents stay outside this surface, so no private execution fact
can reach the wire.

Rows are ordered by the instance primary key ``(instance_id, instance_generation)``
alone. That pair is unique by construction, so the ordering is total and carries
no secondary or tie-breaking term over a mutable column such as a state or a
timestamp. ``instance_id`` is restricted to ``[a-z0-9._:-]`` and the database
collation is byte-ordered (``C``, pinned by ``bootstrap.sh`` and the CI cluster),
so PostgreSQL and Python compare these keys identically and the service can
reject a page that is not strictly ascending instead of silently re-sorting it.

The binding revision join is left-joined and then required by the row decoder:
an instance whose revision row is missing cannot be projected truthfully, so the
page fails closed instead of omitting the instance or inventing a binding.

``observed_started_at``, heartbeat times, loaded artifact digests and the
instance stop document are not columns this reader selects. They are absent from
this store, so the decoder never fabricates them.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.model_instances import (
    ModelInstance,
    ModelInstanceLookup,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_COLUMNS = (
    "instance.instance_id, instance.instance_generation, instance.host_node_id, "
    "instance.pool_id, instance.gateway_id, instance.binding_revision_id, "
    "revision.binding_id, revision.revision, "
    "instance.config_digest, instance.instance_state, instance.ready_at_us, "
    "instance.stopped_at_us, instance.quarantine_reason"
)

_BASE = (
    "SELECT " + _COLUMNS + " FROM engine_instances AS instance "
    "LEFT JOIN speech_binding_revisions AS revision "
    "ON revision.revision_id = instance.binding_revision_id"
)

_ORDER = " ORDER BY instance.instance_id, instance.instance_generation LIMIT %s"

# The presence CTE holds exactly one row, so the left join below always returns
# exactly one row: an absent generation still reports whether its instance id has
# any recorded generation at all.
_FIND = (
    "WITH presence AS (SELECT EXISTS (SELECT 1 FROM engine_instances AS listed "
    "WHERE listed.instance_id = %s) AS instance_exists) "
    "SELECT presence.instance_exists, " + _COLUMNS + " FROM presence "
    "LEFT JOIN engine_instances AS instance ON instance.instance_id = %s "
    "AND instance.instance_generation = %s "
    "LEFT JOIN speech_binding_revisions AS revision "
    "ON revision.revision_id = instance.binding_revision_id"
)


def statement(query: ModelInstanceQuery) -> tuple[str, tuple[Any, ...]]:
    """Compose the single statement this reader may run for one page.

    The returned text is the whole query surface: the selected relations, the
    keyset frontier, the optional exact filters and one total ordering. A caller
    cannot widen it, and the ordering keeps both key columns so no page can
    depend on the database's incidental row order.
    """
    if not isinstance(query, ModelInstanceQuery):
        raise ModelInstanceUnavailableError
    after_id = query.after.instance_id if query.after is not None else ""
    after_generation = query.after.generation if query.after is not None else 0
    predicate = " AND instance.host_node_id = %s" if query.host_node_id is not None else ""
    predicate += " AND instance.instance_state = %s" if query.state is not None else ""
    parameters: list[Any] = [after_id, after_generation]
    if query.host_node_id is not None:
        parameters.append(query.host_node_id)
    if query.state is not None:
        parameters.append(query.state)
    parameters.append(query.limit + 1)
    text = (
        _BASE
        + " WHERE (instance.instance_id, instance.instance_generation) > (%s, %s)"
        + predicate
        + _ORDER
    )
    return text, tuple(parameters)


class PostgresModelInstanceReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Model instance reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def scan(self, query: ModelInstanceQuery) -> tuple[ModelInstance, ...]:
        text, parameters = statement(query)
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelInstanceUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                records = connection.execute(text, parameters).fetchall()
                result = tuple(_instance(row) for row in records)
                if time.monotonic() >= deadline:
                    raise ModelInstanceUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ModelInstanceUnavailableError,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise ModelInstanceUnavailableError from None

    def find(self, instance_id: str, generation: int) -> ModelInstanceLookup:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelInstanceUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                row = connection.execute(_FIND, (instance_id, instance_id, generation)).fetchone()
                if row is None:
                    raise ModelInstanceUnavailableError
                exists = row["instance_exists"]
                if not isinstance(exists, bool):
                    raise ModelInstanceUnavailableError
                instance = _instance(row) if row["instance_id"] is not None else None
                result = ModelInstanceLookup(exists, instance)
                if time.monotonic() >= deadline:
                    raise ModelInstanceUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ModelInstanceUnavailableError,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise ModelInstanceUnavailableError from None


def _instance(row: dict[str, Any]) -> ModelInstance:
    binding_id = row["binding_id"]
    revision = row["revision"]
    if binding_id is None or revision is None:
        # The instance names a binding revision that is not recorded; refusing
        # keeps the row out instead of projecting a guessed binding identity.
        raise ModelInstanceUnavailableError
    return ModelInstance(
        instance_id=row["instance_id"],
        host_node_id=row["host_node_id"],
        gateway_id=row["gateway_id"],
        binding_id=binding_id,
        binding_revision=revision,
        binding_revision_id=row["binding_revision_id"],
        pool_id=row["pool_id"],
        generation=row["instance_generation"],
        config_digest=row["config_digest"],
        state=row["instance_state"],
        ready_at_us=row["ready_at_us"],
        stopped_at_us=row["stopped_at_us"],
        quarantine_reason=row["quarantine_reason"],
    )
