"""InnoDB plumbing for the identical production runtime SQL/domain."""

from dataclasses import dataclass, field
from typing import cast

import pymysql

from speechnexus_backend.speech.domain.runtime_intents import (
    CreateRuntimeIntent,
    RuntimeIdentity,
    RuntimeRejectedError,
    RuntimeResult,
    rejected,
)
from speechnexus_backend.speech.domain.runtime_observations import ObserveRuntimeIntent
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import decode_observation
from speechnexus_backend.speech.infrastructure.runtime_observation_sql import observe_in_transaction
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    RuntimeCursor,
    create_in_transaction,
    query_in_transaction,
)
from tests.support.job_database import MySQLJobTransactions


@dataclass(frozen=True)
class MySQLRuntimeTransactions:
    driver: MySQLJobTransactions = field(repr=False)

    def _run(
        self,
        identity: RuntimeIdentity,
        command: CreateRuntimeIntent | ObserveRuntimeIntent | None = None,
        *,
        request_digest: str | None = None,
        conflict_if_absent: bool = False,
    ) -> RuntimeResult:
        connection = self.driver.connect()
        try:
            with connection.cursor() as raw:
                cursor = cast(RuntimeCursor, raw)
                if command is None:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    result = query_in_transaction(
                        cursor,
                        identity,
                        request_digest=request_digest,
                        conflict_if_absent=conflict_if_absent,
                    )
                else:

                    def clock() -> int:
                        cursor.execute(
                            "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) "
                            "* 1000000 AS SIGNED) AS now_us"
                        )
                        return cast(int, cursor.fetchall()[0]["now_us"])

                    if isinstance(command, CreateRuntimeIntent):
                        result = create_in_transaction(
                            cursor, command, prepare_request(command.request), clock=clock
                        )
                    else:
                        prepared = decode_observation(command.original_document)
                        result = observe_in_transaction(cursor, command, prepared, clock=clock)
            connection.commit()
            return result
        except RuntimeRejectedError as error:
            connection.rollback()
            return rejected(identity, error.code)
        except pymysql.IntegrityError as error:
            connection.rollback()
            if error.args[0] != 1062 or not isinstance(command, CreateRuntimeIntent):
                raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        assert isinstance(command, CreateRuntimeIntent)
        return self._run(
            identity,
            request_digest=prepare_request(command.request).request_digest,
            conflict_if_absent=True,
        )

    def create_runtime_intent(self, command: CreateRuntimeIntent) -> RuntimeResult:
        return self._run(command.request.identity, command)

    def query_runtime_intent(
        self, identity: RuntimeIdentity, *, request_digest: str | None = None
    ) -> RuntimeResult:
        return self._run(identity, request_digest=request_digest)

    def observe_runtime(self, command: ObserveRuntimeIntent) -> RuntimeResult:
        return self._run(command.observation.identity, command)
