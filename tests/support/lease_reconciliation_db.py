"""InnoDB plumbing for the bounded lease sweep; identical production SQL and rules.

Mirrors ``tests/support/lease_database.MySQLLeaseTransactions``: the selection
statement, the release transaction and the four projection updates are the product's
own modules, and only the connection, the commit and the portable clock are InnoDB's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from speechnexus_backend.speech.application.lease_reconciliation import (
    LeaseCandidate,
    release_request,
)
from speechnexus_backend.speech.domain.leases import LeaseResult
from speechnexus_backend.speech.infrastructure.lease_reconciliation_sql import (
    CandidateCursor,
    candidate_rows,
)
from tests.support.job_database import MySQLJobTransactions
from tests.support.lease_database import MySQLLeaseTransactions

#: The same expression the InnoDB test clocks use; the sweep compares deadlines
#: against the database's own instant, never the scheduler host's.
CLOCK = "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"


@dataclass(frozen=True)
class MySQLLeaseReconciliation:
    driver: MySQLJobTransactions = field(repr=False)

    def candidates(self, limit: int) -> tuple[LeaseCandidate, ...]:
        connection = self.driver.connect()
        try:
            with connection.cursor() as raw:
                cursor = cast(CandidateCursor, raw)
                cursor.execute(CLOCK)
                now_us = cast(int, cursor.fetchmany(1)[0]["now_us"])
                rows = candidate_rows(cursor, now_us=now_us, limit=limit)
            connection.commit()
        finally:
            connection.close()
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
        return MySQLLeaseTransactions(self.driver).release_lease(release_request(candidate))
