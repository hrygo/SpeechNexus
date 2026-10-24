"""InnoDB plumbing only; use identical production lease SQL and pure rules."""

from dataclasses import dataclass, field
from typing import cast

from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import (
    LeaseRejectedError,
    LeaseResult,
    QuarantineLease,
    rejected,
)
from speechnexus_backend.speech.infrastructure.lease_release_sql import release_in_transaction
from speechnexus_backend.speech.infrastructure.leases_sql import (
    LeaseCursor,
    quarantine_in_transaction,
    query_in_transaction,
)
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLLeaseTransactions:
    driver: MySQLJobTransactions = field(repr=False)

    def _run(self, request: QuarantineLease | ReleaseLease | str) -> LeaseResult:
        connection = self.driver.connect()
        lease_id = request if isinstance(request, str) else request.lease_id
        try:
            with connection.cursor() as raw:
                cursor = cast(LeaseCursor, raw)
                if isinstance(request, str):
                    cursor.execute("SET TRANSACTION READ ONLY")
                    result = query_in_transaction(cursor, request)
                else:

                    def clock() -> int:
                        cursor.execute(
                            "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) "
                            "* 1000000 AS SIGNED) AS now_us"
                        )
                        return cast(int, cursor.fetchall()[0]["now_us"])

                    result = (
                        release_in_transaction(cursor, request, clock=clock)
                        if isinstance(request, ReleaseLease)
                        else quarantine_in_transaction(cursor, request, clock=clock)
                    )
            connection.commit()
            return result
        except LeaseRejectedError as error:
            connection.rollback()
            return rejected(lease_id, error.code)
        except ValueError, TypeError, KeyError:
            connection.rollback()
            return rejected(lease_id, "reconciliation_required")
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def quarantine_lease(self, request: QuarantineLease) -> LeaseResult:
        return self._run(request)

    def query_lease(self, lease_id: str) -> LeaseResult:
        return self._run(lease_id)

    def release_lease(self, request: ReleaseLease) -> LeaseResult:
        return self._run(request)
