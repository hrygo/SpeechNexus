"""Test-only InnoDB plumbing for the unchanged profile SQL; no product adapter."""

from collections.abc import Callable
from typing import Any

import pymysql

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    ProfileDraft,
    ProfileDraftResult,
    ProfileUnavailableError,
    revision_number,
)
from speechnexus_backend.speech.infrastructure.profile_drafts_sql import (
    create_draft,
    read_draft,
    recover_draft,
)
from tests.support.job_database import MySQLJobTransactions


class ProfileCursor:
    def __init__(self, raw: Any) -> None:
        self.raw, self.rowcount = raw, -1

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        conflict = " ON CONFLICT " in query
        if conflict:
            query = query.split(" ON CONFLICT ")[0].replace("INSERT INTO", "INSERT IGNORE INTO", 1)
        self.raw.execute(query, parameters)
        self.rowcount = self.raw.rowcount
        if conflict:
            self.raw.execute("SHOW WARNINGS")
            if any(row["Code"] != 1062 for row in self.raw.fetchall()):
                raise ProfileUnavailableError
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.raw.fetchall())

    def clock(self) -> int:
        self.execute("SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6))*1000000 AS SIGNED) AS t")
        return int(self.fetchall()[0]["t"])


class MySQLProfileDrafts:
    def __init__(self, connector: MySQLJobTransactions) -> None:
        self.connector = connector
        self.lose_reply = False

    def _run[T](self, operation: Callable[[ProfileCursor], T], *, read_only: bool = False) -> T:
        for attempt in range(3):
            try:
                return self._once(operation, read_only=read_only)
            except pymysql.OperationalError as error:
                # Native 1213 guarantees the complete transaction rolled back.
                # Never apply this rule to reads or a lost COMMIT reply.
                if read_only or error.args[0] != 1213 or attempt == 2:
                    raise ProfileUnavailableError from None
        raise ProfileUnavailableError

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
            raise ProfileUnavailableError from None
        except pymysql.Error, ValueError, TypeError, KeyError, ProfileUnavailableError:
            connection.rollback()
            raise ProfileUnavailableError from None
        finally:
            connection.close()

    def create(self, command: CreateProfileDraft) -> ProfileDraftResult:
        try:
            return self._run(lambda cursor: create_draft(cursor, command, cursor.clock))
        except ProfileUnavailableError:
            return ProfileDraftResult("unknown")

    def recover(self, command: CreateProfileDraft) -> ProfileDraftResult:
        try:
            return self._run(lambda cursor: recover_draft(cursor, command), read_only=True)
        except ProfileUnavailableError:
            return ProfileDraftResult("unknown")

    def get(self, profile_id: str, revision: str) -> ProfileDraft | None:
        identifier(profile_id)
        revision_number(revision)
        return self._run(lambda cursor: read_draft(cursor, profile_id, revision), read_only=True)
