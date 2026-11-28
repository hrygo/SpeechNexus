"""PostgreSQL adapter for the bounded lease sweep: one read, then one release each.

The sweep reads candidates in a short read-only transaction and then releases them
one transactional call at a time, through the injected ``LeaseTransactions`` port.
That port is ``PostgresLeaseTransactions``, whose ``release_lease`` is the one
implementation of the counter rollback (``lease_release_sql.release_in_transaction``):
this module adds no UPDATE of its own and therefore cannot drift from the
dispatcher's release.

The release runs under the operator release identity
(``SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL``, ``sn_quarantine_release``). That
identity already declares exactly the release surface -- full reads of the four
projection tables, column reads of ``submit_intents``/``speech_jobs.state`` and the
column UPDATE set ``release_in_transaction`` performs -- and its startup gate
(``verify_quarantine_release_role``) compares that surface with the live catalog, so
a deployment that re-provisions it narrowly fails here by name. Reusing it is
deliberate: a second role with the same release columns would be a second
declaration of the same rule. What this path never does is record testimony: the
released lease row itself is the durable fact, exactly as it is for the dispatcher's
own release.
"""

from __future__ import annotations

import time
from typing import cast

from speechnexus_backend.speech.application.lease_reconciliation import (
    LeaseCandidate,
    LeaseReconciliationTransactions,
    release_request,
)
from speechnexus_backend.speech.application.leases import LeaseTransactions
from speechnexus_backend.speech.domain.leases import LeaseResult
from speechnexus_backend.speech.infrastructure.lease_reconciliation_sql import (
    candidate_rows,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import _DeadlineCursor
from speechnexus_backend.speech.infrastructure.leases_sql import LeaseCursor
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool


class PostgresLeaseReconciliation(LeaseReconciliationTransactions):
    """Bounded reads plus the existing release port; no counter arithmetic here."""

    def __init__(
        self,
        pool: SpeechPool,
        releases: LeaseTransactions,
        *,
        timeout_seconds: float = 3,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Lease reconciliation requires a bounded deadline")
        self._pool = pool
        self._releases = releases
        self._timeout = timeout_seconds

    def candidates(self, limit: int) -> tuple[LeaseCandidate, ...]:
        """Read at most ``limit`` eligible leases in one bounded read-only read.

        The read is read-only on the connection, not by convention: a sweep must not
        be able to write through the statement that decides what it will release.
        "Now" comes from the database clock, so the deadline comparison cannot be
        moved by a host clock difference between the scheduler and the database.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError("Lease reconciliation needs a positive bound")
        deadline = time.monotonic() + self._timeout
        with self._pool.connection(timeout=self._timeout) as connection:
            connection.read_only = True
            try:
                with connection.cursor() as raw:
                    cursor = _DeadlineCursor(cast(LeaseCursor, raw), deadline)
                    rows = candidate_rows(cursor, now_us=cursor.clock(), limit=limit)
            finally:
                # Read-only is a *session* characteristic, and the pool hands the
                # same session to the next checkout. Leaving it set makes the
                # release that follows this read run READ ONLY and fail as
                # ``reconciliation_required`` -- which reads like a ledger defect
                # rather than the connection state it is. Restore it explicitly.
                connection.rollback()
                connection.read_only = False
        return tuple(
            LeaseCandidate(
                lease_id=row["lease_id"],
                version=row["version"],
                operation_id=row["operation_id"],
                attempt_id=row["attempt_id"],
                stage_id=row["stage_id"],
                reserved_slots=row["reserved_slots"],
                attempt_quota_state=row["attempt_quota_state"],
            )
            for row in rows
        )

    def release(self, candidate: LeaseCandidate) -> LeaseResult:
        """One release transaction over the version the selection read."""
        return self._releases.release_lease(release_request(candidate))


__all__ = ["PostgresLeaseReconciliation"]
