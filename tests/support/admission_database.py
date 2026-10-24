"""InnoDB oracle for identical production admission SQL; not a product adapter."""

from dataclasses import dataclass, field
from typing import cast

import pymysql
from pymysql.cursors import DictCursor

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionResult,
    AdmitRecipe,
    rejected,
    unknown,
)
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    prepare_admission,
)
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import (
    AdmissionCursor,
    read_original,
)
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLAdmissionTransactions:
    driver: MySQLJobTransactions = field(repr=False)
    max_command_bytes: int

    def _run(
        self,
        identity: AdmissionIdentity,
        *,
        prepared: PreparedAdmission | None = None,
        request_digest: str | None = None,
        recover_prepared: PreparedAdmission | None = None,
        conflict_if_absent: bool = False,
    ) -> AdmissionResult:
        connection: pymysql.Connection[DictCursor] | None = None
        ready_to_commit = False
        try:
            connection = self.driver.connect()
            with connection.cursor() as raw:
                cursor = cast(AdmissionCursor, raw)
                if prepared is None:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    found = read_original(
                        cursor,
                        identity,
                        max_command_bytes=self.max_command_bytes,
                        request_digest=request_digest,
                        prepared=recover_prepared,
                    )
                    result = (
                        found
                        if found is not None
                        else rejected(identity, "idempotency_conflict")
                        if conflict_if_absent
                        else unknown(identity)
                    )
                else:

                    def clock() -> int:
                        cursor.execute(
                            "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) "
                            "* 1000000 AS SIGNED) AS now_us"
                        )
                        return cast(int, cursor.fetchall()[0]["now_us"])

                    result = admit_in_transaction(
                        cursor, prepared, clock=clock, max_command_bytes=self.max_command_bytes
                    )
            ready_to_commit = True
            connection.commit()
            return result
        except AdmissionRejectedError as error:
            if connection is not None:
                connection.rollback()
            return rejected(identity, error.code)
        except pymysql.IntegrityError as error:
            if connection is not None:
                connection.rollback()
            if error.args[0] != 1062 or prepared is None:
                return rejected(identity, "reconciliation_required")
        except pymysql.OperationalError as error:
            if connection is not None:
                connection.rollback()
            if prepared is None or ready_to_commit:
                # A failed read cannot disprove the original write. A lost
                # commit reply cannot authorize another admission either.
                return unknown(identity)
            if error.args[0] == 3819:  # MySQL classifies CHECK failure as operational.
                return rejected(identity, "reconciliation_required")
            raise
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
        return self._run(identity, recover_prepared=prepared, conflict_if_absent=True)

    def admit_recipe(self, command: AdmitRecipe) -> AdmissionResult:
        return self._run(
            command.request.identity,
            prepared=prepare_admission(command, max_command_bytes=self.max_command_bytes),
        )

    def query_admission(
        self, identity: AdmissionIdentity, *, request_digest: str | None = None
    ) -> AdmissionResult:
        return self._run(identity, request_digest=request_digest)
