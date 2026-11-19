"""Every missing prerequisite behind a 503 names itself, from one table.

#920: the Job surface answered the same ``503 job_unavailable`` /
"Job storage is unavailable." for a blank writer DSN, a blank admission DSN, a
database that never ran the migration profile and a role without its grants, and
three call sites dropped the explanation a lower layer had already computed.

The table in ``speech/domain/job_prerequisites.py`` is the single source: these
tests pin that the rows differ, that each row names only its own key or command,
that a driver refusal is classified into the right row, and that every converting
route forwards the row instead of the generic sentence. Each case is written so the
mutation it forbids turns it red: deleting a key name from a detail, collapsing two
rows onto one message, or dropping the detail at a call site.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import JobRejectedError
from speechnexus_backend.speech.domain.job_prerequisites import (
    ADMISSION_DSN,
    ADMISSION_DSN_KEY,
    JOB_PREREQUISITES,
    MIGRATION_COMMAND,
    PROVISION_ROLES_COMMAND,
    ROLE_PRIVILEGES_MISSING,
    SCHEMA_NOT_MIGRATED,
    STORE_UNAVAILABLE,
    WRITER_DSN,
    WRITER_DSN_KEY,
    prerequisite,
    prerequisite_detail,
    prerequisite_of_error,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.interfaces.http import jobs as jobs_routes
from speechnexus_backend.speech.interfaces.http.job_errors import _ERRORS, job_storage_error
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import _body

PREFIX = "/v1/nexus/jobs"
# Everything an operator answer must never echo: a DSN scheme, a host, a port, a
# database name, a user or a credential.
LEAK = re.compile(r"postgresql://|mysql://|@|:[0-9]{2,5}/|password|secret|hunter2", re.IGNORECASE)


def _message(response: Any) -> str:
    # An httpx response and a starlette JSONResponse both carry the same envelope.
    body = response.text if hasattr(response, "text") else bytes(response.body).decode()
    return str(json.loads(body)["error"]["message"])


class Refusing:
    """Every service call the Job routes make answers one planted refusal."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def _refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    submit = _refuse
    list_jobs = _refuse
    artifacts_page = _refuse
    get = _refuse
    events = _refuse
    cancel = _refuse
    retry = _refuse


def _app(service: Any) -> Any:
    return create_app(settings=_settings(), jobs=cast(Any, service))


def test_every_prerequisite_row_is_distinct_and_names_only_its_own_item() -> None:
    conditions = [condition for condition, _, _ in JOB_PREREQUISITES]
    details = [detail for _, detail, _ in JOB_PREREQUISITES]
    assert len(set(conditions)) == len(conditions)
    # Two missing prerequisites may not answer the same sentence: that is the whole
    # defect the card names.
    assert len(set(details)) == len(details)
    for _, detail, action in JOB_PREREQUISITES:
        assert detail.strip() and action.strip()
        # Never a DSN value, host, database, user or credential; key names only.
        assert LEAK.search(detail) is None, detail
        assert LEAK.search(action) is None, action


def test_the_writer_row_names_the_writer_key_and_never_the_admission_key() -> None:
    assert WRITER_DSN_KEY in WRITER_DSN.detail
    assert ADMISSION_DSN_KEY not in WRITER_DSN.detail
    assert "blank" in WRITER_DSN.detail


def test_the_admission_row_names_the_admission_key_and_never_the_writer_key() -> None:
    assert ADMISSION_DSN_KEY in ADMISSION_DSN.detail
    assert WRITER_DSN_KEY not in ADMISSION_DSN.detail
    assert ADMISSION_DSN.detail != WRITER_DSN.detail


def test_the_migration_row_gives_the_shared_migration_command() -> None:
    assert "migration profile" in SCHEMA_NOT_MIGRATED.detail
    assert MIGRATION_COMMAND in SCHEMA_NOT_MIGRATED.detail
    assert MIGRATION_COMMAND in SCHEMA_NOT_MIGRATED.action


def test_the_grant_row_gives_the_shared_provisioning_command() -> None:
    assert PROVISION_ROLES_COMMAND in ROLE_PRIVILEGES_MISSING.detail
    assert PROVISION_ROLES_COMMAND in ROLE_PRIVILEGES_MISSING.action
    assert ROLE_PRIVILEGES_MISSING.detail != SCHEMA_NOT_MIGRATED.detail


def test_the_declared_storage_message_is_the_table_row() -> None:
    # The generic answer is one row of the same table, so the operator map lists it
    # beside the four failures that do name a key.
    assert _ERRORS["job_unavailable"][2] == STORE_UNAVAILABLE.detail
    assert _message(job_storage_error("req-storage")) == STORE_UNAVAILABLE.detail


@pytest.mark.parametrize("sqlstate", ["42P01", "3F000"])
def test_a_missing_relation_or_schema_is_the_migration_row(sqlstate: str) -> None:
    error = psycopg.errors.UndefinedTable('relation "admission_leases" does not exist')
    error.sqlstate = sqlstate
    assert prerequisite_of_error(error) is SCHEMA_NOT_MIGRATED
    message = _message(job_storage_error("req-migration", error))
    assert message == SCHEMA_NOT_MIGRATED.detail
    # The refusal must not read as the driver's own sentence: it names the repair.
    assert "does not exist" not in message


def test_a_wrapped_missing_relation_keeps_its_classification() -> None:
    # The store wraps a driver failure into its own refusal with ``from None``; the
    # SQLSTATE is still readable on the chain, so the answer stays the migration one.
    try:
        try:
            raise psycopg.errors.UndefinedTable('relation "speech_jobs" does not exist')
        except psycopg.errors.UndefinedTable:
            raise PublicJobUnavailableError from None
    except PublicJobUnavailableError as wrapped:
        assert prerequisite_detail(wrapped) == SCHEMA_NOT_MIGRATED.detail
        assert _message(job_storage_error("req-wrapped", wrapped)) == SCHEMA_NOT_MIGRATED.detail


def test_a_missing_grant_is_classified_apart_from_a_missing_migration() -> None:
    grant = psycopg.errors.InsufficientPrivilege("permission denied for table speech_jobs")
    assert prerequisite_of_error(grant) is ROLE_PRIVILEGES_MISSING
    assert _message(job_storage_error("req-grant", grant)) == ROLE_PRIVILEGES_MISSING.detail
    assert ROLE_PRIVILEGES_MISSING.detail != SCHEMA_NOT_MIGRATED.detail


def test_an_unclassified_failure_keeps_the_declared_storage_answer() -> None:
    # A decoder defect or a pool timeout has no configuration cause; inventing one
    # would send an operator to a key that changes nothing.
    assert prerequisite_of_error(ValueError("synthetic decoder defect")) is None
    assert prerequisite_of_error(None) is None
    assert _message(job_storage_error("req-plain", ValueError("synthetic"))) == (
        STORE_UNAVAILABLE.detail
    )


def test_an_empty_detail_falls_back_to_the_table() -> None:
    # A refusal that knows nothing (``PublicJobUnavailableError()``) must not blank
    # the message: the declared storage answer stays readable.
    assert _message(job_storage_error("req-empty", PublicJobUnavailableError())) == (
        STORE_UNAVAILABLE.detail
    )


def test_an_explicit_detail_wins_over_the_table_classification() -> None:
    # The store knows the one item that is missing ("no current qualified voice");
    # the boundary must not overwrite it with the generic row.
    error = PublicJobUnavailableError(ADMISSION_DSN.detail)
    assert _message(job_storage_error("req-explicit", error)) == ADMISSION_DSN.detail


JOB_PATH = PREFIX + "/job-00000000000000000000000000000001"
# One case per converting site in ``interfaces/http/jobs.py`` -- twelve of them,
# ordered by the file. ``stage`` names where the planted refusal has to be raised so
# the case really reaches that site instead of stopping at the one before it:
# ``submit``/``retry``/``cancel`` raise inside the write, ``list_jobs``/
# ``artifacts_page``/``artifacts``/``get`` raise on the read or the projection.
FORWARDING_CASES: tuple[tuple[str, str, str, str, str], ...] = (
    # _create: the service refusal, then the store's own mechanism refusal.
    ("post", PREFIX, "submit", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("post", PREFIX, "submit", "rejected", SCHEMA_NOT_MIGRATED.detail),
    # _list: the page read, then the batched Artifact projection of that page.
    ("get", PREFIX, "list_jobs", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("get", PREFIX, "artifacts_page", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    # _read: the existence read (an outage here used to escape as a 500), then the
    # result projection (a contract-invalid persisted document is storage, not a
    # missing Job, so this answer must not be the 404 one).
    ("get", JOB_PATH, "get", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("get", JOB_PATH, "artifacts", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    # _cancel: the service refusal, then a contract-invalid cancellation view.
    ("post", JOB_PATH + "/cancellations", "cancel", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("post", JOB_PATH + "/cancellations", "cancel", "invalid", STORE_UNAVAILABLE.detail),
    # _retry: the store mechanism refusal, the service refusal, the projection.
    ("post", JOB_PATH + "/retries", "retry", "rejected", SCHEMA_NOT_MIGRATED.detail),
    ("post", JOB_PATH + "/retries", "retry", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("post", JOB_PATH + "/retries", "retry", "invalid", STORE_UNAVAILABLE.detail),
    # _open_stream: the existence read, then the projection of that read.
    ("get", JOB_PATH + "/events", "get", "unavailable", SCHEMA_NOT_MIGRATED.detail),
    ("get", JOB_PATH + "/events", "get", "invalid", STORE_UNAVAILABLE.detail),
    # _open_stream also serves the readiness report: the admission-role read is the
    # one converting site this route owns.
    (
        "get",
        "/v1/nexus/jobs/admission-readiness",
        "admission_readiness",
        "unavailable",
        SCHEMA_NOT_MIGRATED.detail,
    ),
)


def _planted(kind: str) -> BaseException:
    """The refusal a stage raises: a named miss, a store rejection, or a defect."""
    if kind == "rejected":
        return JobRejectedError("storage_rejected", SCHEMA_NOT_MIGRATED.detail)
    if kind == "invalid":
        # A contract violation has no missing configuration item: it keeps the
        # declared storage sentence rather than advertising a key.
        return ContractValidationError("synthetic contract defect")
    return PublicJobUnavailableError(SCHEMA_NOT_MIGRATED.detail)


class StageRefusal:
    """A service that answers normally until ``stage``, then raises the refusal."""

    def __init__(self, stage: str, error: BaseException) -> None:
        self._stage = stage
        self._error = error

    def _maybe(self, stage: str) -> None:
        if stage == self._stage:
            raise self._error

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("submit")
        raise AssertionError("this case must refuse in submit")

    def list_jobs(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("list_jobs")
        return (("synthetic-job",), None)

    def artifacts_page(self, page: Any) -> Any:
        self._maybe("artifacts_page")
        raise AssertionError("this case must refuse in the page projection")

    def get(self, job_id: str) -> Any:
        self._maybe("get")
        return "synthetic-job"

    def artifacts(self, job: Any) -> Any:
        self._maybe("artifacts")
        raise AssertionError("this case must refuse in the result projection")

    def events(self, *args: Any, **kwargs: Any) -> Any:
        return ()

    def cancel(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("cancel")
        raise AssertionError("this case must refuse in cancel")

    def retry(self, *args: Any, **kwargs: Any) -> Any:
        self._maybe("retry")
        raise AssertionError("this case must refuse in retry")

    def admission_readiness(self, workspace_id: str) -> Any:
        self._maybe("admission_readiness")
        raise AssertionError("this case must refuse in the readiness read")


@pytest.mark.parametrize(("method", "path", "stage", "kind", "expected"), FORWARDING_CASES)
async def test_every_converting_route_forwards_the_prerequisite_row(
    method: str, path: str, stage: str, kind: str, expected: str
) -> None:
    # Each case is one converting site: it must answer the table's own text (or the
    # declared storage sentence for a defect with no missing item) instead of an
    # empty detail, or the generic line at a site that already knows better.
    app = _app(StageRefusal(stage, _planted(kind)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        call = getattr(client, method)
        response = await (call(path, json=_body()) if method == "post" else call(path))

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "job_unavailable"
    assert response.json()["error"]["retryable"] is False
    assert _message(response) == expected


def test_the_forwarding_cases_cover_every_converting_site() -> None:
    # The review caught this suite covering six of the twelve sites while the PR
    # claimed all of them. The count is read from the source, so adding a site
    # without a case -- or losing one -- fails here instead of in a review. The
    # thirteenth site is the ``_read`` existence read that had no conversion at all,
    # so the same 500-versus-503 defect was hiding behind a missing case.
    source = Path(jobs_routes.__file__).read_text(encoding="utf-8")
    assert source.count("job_storage_error(request_id") == len(FORWARDING_CASES)


def test_the_shipped_rows_are_reachable_by_condition() -> None:
    for condition, detail, action in JOB_PREREQUISITES:
        row = prerequisite(condition)
        assert (row.condition, row.detail, row.action) == (condition, detail, action)
    with pytest.raises(KeyError):
        prerequisite("no_such_condition")
