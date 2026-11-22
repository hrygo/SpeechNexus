"""The one table behind a refused Job operation that is missing a prerequisite.

A refused Job operation used to answer ``503 job_unavailable`` /
"Job storage is unavailable." for every missing prerequisite: a blank writer DSN, a
blank admission DSN, a database that never ran the migration profile and a role
without its grants all read like a storage outage, so an operator could not tell
which key or command repairs the deployment. Six call sites in the Job surface each
decided their own answer, and three of them dropped the explanation a lower layer
had already computed (#877 item 1, #920).

One table decides it now. Each row is *condition -> what an operator sees -> what an
operator runs*:

* ``condition`` is the stable slug the classification below reports and the
  operator map in ``docs/operations/compose-minimal.md`` lists;
* ``detail`` is the public message the Job surface returns in
  ``error.message``. It names environment variable keys and commands only -- never
  a DSN value, host, database name, user or credential;
* ``action`` is the repair the operator map prints beside the condition.

The rows are plain literals so ``tests/ci/test_job_prerequisite_map.py`` can read
them under the system interpreter (that suite never imports the backend) and prove
``docs/operations/compose-minimal.md`` quotes exactly this table, row for row. The
domain also keeps no vendor import and no vendor code: the driver翻好 ``reason``
(``schema_missing`` / ``permission_denied`` / ``unavailable``)再往上抛,分类只读这一个字段,
不读 SQLSTATE 也不认 psycopg 类型。
"""

from __future__ import annotations

from dataclasses import dataclass

WRITER_DSN_KEY = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
ADMISSION_DSN_KEY = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"
MIGRATION_COMMAND = "docker compose --profile migration run --rm migrate"
PROVISION_ROLES_COMMAND = "scripts/dev-env/provision-job-roles.py"

# condition -> (public detail, operator action). Every row is one *missing
# prerequisite*, so two conditions never share a detail: an operator who reads two
# answers must be able to tell which key or command each of them names (#920).
JOB_PREREQUISITES: tuple[tuple[str, str, str], ...] = (
    (
        "writer_dsn_blank",
        "SPEECHNEXUS_JOB_WRITER_DATABASE_URL is blank: the Job surface is not "
        "assembled, so no Job route can create, read or cancel. Set that key for the "
        "api process; it is the identity that reads and writes Job storage.",
        "Set SPEECHNEXUS_JOB_WRITER_DATABASE_URL in the api environment.",
    ),
    (
        "admission_dsn_blank",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL is blank: a create cannot reserve "
        "capacity and write its Job in one admission transaction, so no Job is "
        "written. Reads and cancellations keep working; set it to the same database "
        "as the Job writer.",
        "Set SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL to the Job writer's database.",
    ),
    (
        "schema_not_migrated",
        "The Job database has not run the migration profile: a required relation is "
        "missing, so no Job can be stored. Run "
        "docker compose --profile migration run --rm migrate, then restart the api "
        "process.",
        "Run docker compose --profile migration run --rm migrate.",
    ),
    (
        "role_privileges_missing",
        "The Job role lacks a grant the store needs, so the database refused the read "
        "or the write. Run scripts/dev-env/provision-job-roles.py for the api role, "
        "then send a new request.",
        "Run scripts/dev-env/provision-job-roles.py for the api role.",
    ),
    (
        "store_unavailable",
        "Job storage is unavailable.",
        "Read the api log for the storage failure; no configuration key is missing.",
    ),
)


@dataclass(frozen=True, slots=True)
class JobPrerequisite:
    """One missing prerequisite and the public answer that names it."""

    condition: str
    detail: str
    action: str


_BY_CONDITION: dict[str, JobPrerequisite] = {}
for _condition, _detail, _action in JOB_PREREQUISITES:
    # A duplicated condition would silently drop one row from the map an operator
    # reads and checks, so the table refuses to load instead.
    if _condition in _BY_CONDITION:
        raise ValueError("Duplicate Job prerequisite condition: " + _condition)
    _BY_CONDITION[_condition] = JobPrerequisite(_condition, _detail, _action)


def prerequisite(condition: str) -> JobPrerequisite:
    """Return the one row for a condition slug; an unknown slug is a defect."""
    return _BY_CONDITION[condition]


WRITER_DSN = prerequisite("writer_dsn_blank")
ADMISSION_DSN = prerequisite("admission_dsn_blank")
SCHEMA_NOT_MIGRATED = prerequisite("schema_not_migrated")
ROLE_PRIVILEGES_MISSING = prerequisite("role_privileges_missing")
STORE_UNAVAILABLE = prerequisite("store_unavailable")


def prerequisite_of_error(error: BaseException | None) -> JobPrerequisite | None:
    """Classify one refusal into the table above, or return ``None``.

    The classification walks ``__cause__``/``__context__`` because a store wraps a
    driver failure into its own named refusal; the SQLSTATE stays readable on the
    chain even when the wrapper suppresses the traceback. ``None`` means the refusal
    has no configuration cause to name (a decoder defect, a pool timeout, a genuine
    outage), and the caller keeps the declared "Job storage is unavailable." answer.
    """
    seen: set[int] = set()
    frame = error
    while frame is not None and id(frame) not in seen:
        seen.add(id(frame))
        reason = getattr(frame, "reason", None)
        if reason == "schema_missing":
            return SCHEMA_NOT_MIGRATED
        if reason == "permission_denied":
            return ROLE_PRIVILEGES_MISSING
        frame = frame.__cause__ or frame.__context__
    return None


def prerequisite_detail(error: BaseException | None) -> str | None:
    """The public ``detail`` for one refusal, or ``None`` when nothing is missing."""
    found = prerequisite_of_error(error)
    return None if found is None else found.detail
