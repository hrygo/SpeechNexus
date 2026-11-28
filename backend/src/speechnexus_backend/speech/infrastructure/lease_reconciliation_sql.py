"""Select the reserved leases no dispatch can ever reach; the predicate is written once.

#1248: a Job that reached a terminal state **before** any dispatcher claimed it kept
its admission lease in ``reserved`` forever, because the only path that returned the
capacity (``job_dispatcher._settle_not_submitted``) is reachable only after a claim.
The refused cancellation of a still-``pending`` Job is one such terminal: it sets
``speech_jobs.state='cancelled'`` and ``coordination_jobs.job_state='cancelled'`` with
no verified stop and no execution, so the acceptance is terminal and nothing will
ever move the lease again. The four projections stayed held, and the next admission
read that as ``capacity_exhausted``.

This statement is the whole selection rule. Every condition is required:

* ``lease_state = 'reserved'`` -- neither isolated nor released;
* ``deadline_at_us < now`` -- the frozen admission deadline has passed, so this
  lease can no longer be the one an execution is about to be created under;
* ``speech_jobs.state`` is in ``TERMINAL_STATES`` -- the domain's single definition
  of "a public Job can never transition again". The dispatcher refuses such a Job
  *before* planning, marking running or submitting (``_dispatch_claimed``), so a
  terminal Job cannot create a submit intent later;
* one of two execution shapes:

  * no ``submit_intents`` row -- the operation never reached the gateway, so there
    is no execution and nothing whose stop could be unproven;
  * or an execution that is durably terminal *with its stop proven*: the submit
    intent says ``succeeded``/``complete`` and ``execution_stop_evidence`` records
    ``execution_stopped = 1`` and ``resources_released = 1``. A Job can reach this
    shape when the dispatcher saw the terminal execution but never settled the Job
    (the 2026-11-28 field case: the engine answered, the gateway kept
    ``succeeded``/``complete`` and its terminal-response evidence, and the Job
    stayed ``running`` with the lease ``reserved`` past its deadline, so the four
    projections held forever and the API's settlement preflight refused to start).
    Nothing is running here, so the capacity may return; the release transaction
    still re-verifies the same evidence through the one release implementation.

Why one read is enough (no second re-check, no "already swept" marker): every
condition is monotone. A terminal state never reopens, ``deadline_at_us`` is an
immutable column of the lease row, and a submit intent can only be created for a
claimable Job -- which a terminal one is not. The release transaction still
re-locks the lease and re-reads the submit intents through the one release
implementation, so an intent that appears in between refuses the release instead of
being ignored.

Both shapes are monotone for the same reason: a terminal Job state never reopens, a
succeeded/complete execution intent never regresses, and stop evidence rows are
append-only. ``job_state`` of ``coordination_jobs`` is deliberately **not** part of
this predicate: an accepted cancellation writes ``cancelled`` there while the public Job
is still ``cancelling`` and the execution may still be running. Using that column
would release the capacity of a live execution, so terminality is decided by the
public Job state, whose terminal set is ``domain.public_jobs.TERMINAL_STATES``.
"""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.public_jobs import TERMINAL_STATES
from speechnexus_portable_sql.contract import ListMarker

type Row = dict[str, Any]

#: The one selection statement. ``LIMIT`` is outside the portable subset, so the
#: bound is applied with ``fetchmany`` after a total order (the documented
#: replacement for keyset paging in ``docs/architecture/database-portability.md``).
CANDIDATE_STATEMENT = (
    "SELECT l.lease_id, l.version, l.operation_id, l.attempt_id, l.stage_id, "
    "l.reserved_slots, a.quota_state AS attempt_quota_state "
    "FROM admission_leases AS l "
    "JOIN admission_attempts AS a ON a.operation_id = l.operation_id "
    "AND a.attempt_id = l.attempt_id "
    "JOIN speech_jobs AS j ON j.job_id = l.operation_id "
    "WHERE l.lease_state = 'reserved' AND l.deadline_at_us < ? "
    "AND ((j.state IN (?) "
    "AND NOT EXISTS (SELECT 1 FROM submit_intents AS s WHERE s.lease_id = l.lease_id)) "
    "OR EXISTS (SELECT 1 FROM submit_intents AS s "
    "JOIN execution_stop_evidence AS e ON e.submit_intent_id = s.submit_intent_id "
    "WHERE s.lease_id = l.lease_id AND s.execution_state = 'succeeded' "
    "AND s.cleanup_state = 'complete' AND e.execution_stopped = 1 "
    "AND e.resources_released = 1)) "
    "ORDER BY l.deadline_at_us, l.lease_id"
)

#: The states the predicate admits, read from the domain instead of restated here:
#: a new terminal state must reach the sweep and the dispatcher's refusal together.
CANDIDATE_STATES = tuple(sorted(TERMINAL_STATES))


class CandidateCursor(Protocol):
    """Only what the selection needs: one bounded read, at most ``limit`` rows."""

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchmany(self, size: int) -> list[Row]: ...


def candidate_rows(cursor: CandidateCursor, *, now_us: int, limit: int) -> tuple[Row, ...]:
    """Read at most ``limit`` releasable leases, oldest deadline first.

    The bound is a truncation, not a pagination key: a backlog is drained over
    several passes, and this statement is repeated from the start each time because
    every settled row leaves the result set through ``lease_state``.
    """
    if type(now_us) is not int or now_us < 0:
        raise ValueError("Lease reconciliation needs the database instant")
    if type(limit) is not int or limit < 1:
        raise ValueError("Lease reconciliation needs a positive bound")
    cursor.execute(CANDIDATE_STATEMENT, (now_us, ListMarker(CANDIDATE_STATES)))
    return tuple(cursor.fetchmany(limit))


__all__ = [
    "CANDIDATE_STATEMENT",
    "CANDIDATE_STATES",
    "CandidateCursor",
    "candidate_rows",
]
