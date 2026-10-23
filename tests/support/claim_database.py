"""InnoDB driver plumbing for the identical production claim SQL/domain."""

from dataclasses import dataclass, field
from typing import cast

from speechnexus_backend.speech.domain.claims import (
    ClaimJob,
    ClaimRejectedError,
    ClaimResult,
    rejected,
)
from speechnexus_backend.speech.infrastructure.claims_sql import (
    ClaimCursor,
    claim_in_transaction,
    query_in_transaction,
)
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLClaimTransactions:
    driver: MySQLJobTransactions = field(repr=False)

    def _run(self, request: ClaimJob | str) -> ClaimResult:
        connection = self.driver.connect()
        job_id = request if isinstance(request, str) else request.job_id
        try:
            with connection.cursor() as raw:
                cursor = cast(ClaimCursor, raw)
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

                    result = claim_in_transaction(cursor, request, clock=clock)
            connection.commit()
            return result
        except ClaimRejectedError as error:
            connection.rollback()
            return rejected(job_id, error.code)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_job(self, request: ClaimJob) -> ClaimResult:
        return self._run(request)

    def query_claim(self, job_id: str) -> ClaimResult:
        return self._run(job_id)
