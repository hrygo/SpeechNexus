"""InnoDB plumbing for the production owner SQL/domain oracle, test-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from speechnexus_backend.speech.domain.owners import (
    AcquireOwner,
    ActivateOwner,
    OwnerRejectedError,
    OwnerResult,
    RenewOwner,
    reject,
)
from speechnexus_backend.speech.infrastructure.owners_sql import (
    OwnerCursor,
    mutate_in_transaction,
    query_in_transaction,
)
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLOwnerTransactions:
    driver: MySQLJobTransactions = field(repr=False)

    def _run(self, request: AcquireOwner | RenewOwner | ActivateOwner | str) -> OwnerResult:
        connection = self.driver.connect()
        pool_id = (
            request
            if isinstance(request, str)
            else (
                request.pool_id if isinstance(request, AcquireOwner) else request.authority.pool_id
            )
        )
        try:
            with connection.cursor() as raw:
                cursor = cast(OwnerCursor, raw)
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

                    result = mutate_in_transaction(cursor, request, clock=clock)
            connection.commit()
            return result
        except OwnerRejectedError as error:
            connection.rollback()
            return reject(pool_id, error.code)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def acquire_owner(self, request: AcquireOwner) -> OwnerResult:
        return self._run(request)

    def renew_owner(self, request: RenewOwner) -> OwnerResult:
        return self._run(request)

    def activate_owner(self, request: ActivateOwner) -> OwnerResult:
        return self._run(request)

    def query_owner(self, pool_id: str) -> OwnerResult:
        return self._run(pool_id)
