"""Install the current cancellation fence in historical test schemas.

Gateway submit reads the fence transactionally and must not treat a missing
table as an absent cancellation. Historical fixtures that stop before 0016
(Public Jobs) or 0035 (cancellation fence) therefore install the same tables
and minimal grants as the current migrations. This helper is test-only and is
not imported by production modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    JOB_CANCELLATION_FENCE_DDL,
    NOT_SUBMITTED_TABLE,
    cancellation_not_submitted_grants,
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import (
    JOBS,
    PUBLIC_JOB_DDL,
)

Rows = Sequence[Mapping[str, Any] | tuple[Any, ...]]
Query = Callable[[str], Rows]


def ensure_cancellation_fence_schema(
    query: Query,
    *,
    schema: str,
    roles: Sequence[str] = (),
) -> None:
    """Create the 0035 projection and grant the writer only its own columns.

    ``query`` executes one SQL statement and returns fetched rows (empty for DDL).
    A schema that already has the fence projection is left untouched, so the
    head fixtures keep their migration-produced object identities.
    """

    def present(table: str) -> bool:
        rows = query(f'SELECT to_regclass(\'"{schema}"."{table}"\') AS name')
        if not rows:
            return False
        row = rows[0]
        return bool(row["name"] if isinstance(row, Mapping) else row[0])

    fence = present("speech_job_cancellation_fences")
    not_submitted = present(NOT_SUBMITTED_TABLE)
    if fence and not_submitted:
        return
    if fence:
        statement = next(part for part in JOB_CANCELLATION_FENCE_DDL if NOT_SUBMITTED_TABLE in part)
        query(statement)
    else:
        # 0012 and older predate Public Jobs; the fence FK needs speech_jobs first.
        if not present(JOBS):
            for statement in PUBLIC_JOB_DDL:
                query(statement)
        for statement in JOB_CANCELLATION_FENCE_DDL:
            query(statement)
    for role in roles:
        for statement in (
            *job_cancellation_fence_grants(schema, role),
            *cancellation_not_submitted_grants(schema, role),
        ):
            query(statement)


__all__ = ["ensure_cancellation_fence_schema"]
