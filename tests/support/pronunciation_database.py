"""Test-only InnoDB driver for the exact production pronunciation statements."""

from collections.abc import Callable

import pymysql

from speechnexus_backend.speech.application.pronunciation_documents import check_command
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
    PronunciationUnavailableError,
    revision_number,
)
from speechnexus_backend.speech.infrastructure.pronunciation_drafts_sql import (
    create_draft,
    read_draft,
    recover_draft,
)
from tests.support.job_database import MySQLJobTransactions
from tests.support.profile_database import ProfileCursor


class MySQLPronunciationDrafts:
    def __init__(self, connector: MySQLJobTransactions) -> None:
        self.connector, self.lose_reply = connector, False

    def _run[T](self, operation: Callable[[ProfileCursor], T], *, read_only: bool = False) -> T:
        for attempt in range(3):
            try:
                return self._once(operation, read_only=read_only)
            except pymysql.OperationalError as error:
                if read_only or error.args[0] != 1213 or attempt == 2:
                    raise PronunciationUnavailableError from None
        raise PronunciationUnavailableError

    def _once[T](self, operation: Callable[[ProfileCursor], T], *, read_only: bool) -> T:
        connection = self.connector.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY" if read_only else "START TRANSACTION")
                result = operation(ProfileCursor(cursor))
            connection.commit()
            if self.lose_reply:
                self.lose_reply = False
                raise pymysql.OperationalError("Synthetic lost committed reply")
            return result
        except pymysql.OperationalError as error:
            connection.rollback()
            if error.args[0] == 1213:
                raise
            raise PronunciationUnavailableError from None
        except (
            pymysql.Error,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
            ProfileUnavailableError,
            PronunciationUnavailableError,
        ):
            connection.rollback()
            raise PronunciationUnavailableError from None
        finally:
            connection.close()

    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult:
        check_command(command)
        try:
            return self._run(lambda cursor: create_draft(cursor, command, cursor.clock))
        except PronunciationUnavailableError:
            return PronunciationDraftResult("unknown")

    def recover(self, command: CreatePronunciationDraft) -> PronunciationDraftResult:
        check_command(command)
        try:
            return self._run(lambda cursor: recover_draft(cursor, command), read_only=True)
        except PronunciationUnavailableError:
            return PronunciationDraftResult("unknown")

    def get(self, pronunciation_id: str, revision: str) -> PronunciationDraft | None:
        identifier(pronunciation_id)
        revision_number(revision)
        return self._run(
            lambda cursor: read_draft(cursor, pronunciation_id, revision), read_only=True
        )
