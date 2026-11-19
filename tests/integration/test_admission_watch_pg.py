"""The admission watch writes one line per verdict change on a real database.

``#1041``: the report answers "would a create be admitted" but nothing asks it when
nobody calls the route. This case drives the production report service through the
watcher and a real coordination schema, so the parts that only a database can settle
are real: the verdict comes from ``decide_create_admission`` over the shipped rows,
the flip is caused by changing a deployment fact (the node leaves ``active``), and
the recovery by putting it back.

What is asserted is the count, not the presence of logs:

* the first observation logs one ``admission_watch_started`` line;
* flipping ``admitting`` -> ``degraded`` logs exactly one ``admission_watch_changed``
  line carrying ``workspace_id`` and ``from``/``to``;
* polling the same verdict again logs nothing -- the whole defect this guards is a
  poller that answers every interval and drowns the transition;
* flipping back logs exactly one more change line.

Boundaries: PostgreSQL with the shipped schema and roles, one watcher in-process.
The interval and the alert threshold are constructor values here; their deployment
wiring is ``admission_watch_runtime`` and its unit suite. A real scheduler, a
restarted process (whose first observation is a ``started`` line, not a change) and
a long-running alert streak are not exercised here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import (
    runtime_database as runtime_database,
)
from tests.integration.test_public_job_admission_http_pg import HOST_NODE, WORKSPACE
from tests.integration.test_public_job_atomic_admission_pg import _seed, _store

from speechnexus_backend.speech.application.admission_watch import (
    EVENT_CHANGED,
    EVENT_STARTED,
    AdmissionWatcher,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

LOGGER = "speechnexus_backend.speech.application.admission_watch"

#: Attributes ``logging`` puts on every record; everything else a record carries was
#: supplied by the watch as ``extra`` and is what a JSON log formatter writes.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _line(record: logging.LogRecord) -> str:
    payload = {
        key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRIBUTES
    }
    return json.dumps(
        {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **payload,
        },
        sort_keys=True,
        default=str,
    )


def test_a_flip_logs_one_line_a_repeat_logs_none_and_a_flip_back_logs_one(
    admission_database: AdmissionDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    store = _store(admission_database)
    # The same application service object the HTTP route calls.
    watcher = AdmissionWatcher(
        PublicJobs(store, store),
        workspaces=(WORKSPACE,),
        interval_seconds=60,
        alert_after=3,
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    observed = 0
    quota_before: Any = db.query(
        "SELECT held_attempts, quarantined_attempts, version FROM workspace_quotas "
        "WHERE workspace_id = %s",
        (WORKSPACE,),
    )

    def drain(phase: str, *, expect: int) -> None:
        nonlocal observed
        fresh = caplog.records[observed:]
        observed += len(fresh)
        for record in fresh:
            print(f"WATCH line phase={phase} {_line(record)}")
        print(f"WATCH phase={phase} lines={len(fresh)}")
        assert len(fresh) == expect, (phase, [_line(record) for record in fresh])

    first = watcher.poll()[0]
    drain("first-observation", expect=1)
    assert first.report.admitting and not first.changed

    db.query(
        "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
        (HOST_NODE,),
    )
    degraded = watcher.poll()[0]
    drain("flip-to-degraded", expect=1)
    assert degraded.changed
    assert degraded.report.status == "degraded"
    assert degraded.report.cause == "engine_resource_disabled"

    repeated = watcher.poll()[0]
    drain("repeat", expect=0)
    assert not repeated.changed
    assert repeated.report.cause == "engine_resource_disabled"

    db.query(
        "UPDATE host_nodes SET node_state = 'active' WHERE host_node_id = %s",
        (HOST_NODE,),
    )
    recovered = watcher.poll()[0]
    drain("flip-back", expect=1)
    assert recovered.changed
    assert recovered.report.admitting

    records = caplog.records
    assert [getattr(record, "event", None) for record in records] == [
        EVENT_STARTED,
        EVENT_CHANGED,
        EVENT_CHANGED,
    ]
    changed = records[1]
    assert getattr(changed, "workspace_id", None) == WORKSPACE
    assert getattr(changed, "from", None) == {"status": "admitting", "cause": None}
    assert getattr(changed, "to", None) == {
        "status": "degraded",
        "cause": "engine_resource_disabled",
    }
    assert isinstance(getattr(changed, "detail", None), str)
    assert getattr(records[2], "from", None) == {
        "status": "degraded",
        "cause": "engine_resource_disabled",
    }
    assert getattr(records[2], "to", None) == {"status": "admitting", "cause": None}

    # Reading the verdict must not reserve anything: the quota counters admission
    # charges a create against are the same after every poll above.
    quota_after: Any = db.query(
        "SELECT held_attempts, quarantined_attempts, version FROM workspace_quotas "
        "WHERE workspace_id = %s",
        (WORKSPACE,),
    )
    assert len(quota_before) == 1
    assert quota_after == quota_before
