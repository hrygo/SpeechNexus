"""Public Job create over real HTTP with the production admission assembly.

The atomic-admission suite drives ``PostgresPublicJobs.create`` directly and the
older Public Job HTTP suite assembles the adapter without an admission pool. Both
hide the request-body -> HTTP -> admission transaction -> public-row path, so the
coupled constraints found by hand in issue #690 were never pinned by a test:

* a TTS create is admitted against ``workspace_quotas``, so the body must name a
  provisioned workspace. Omitting ``workspace_id`` selects ``workspace-default``,
  which the asset migration creates without a quota row, and admission then
  refuses with 503 ``job_unavailable`` -- a code whose message and non-retryable
  flag describe a storage outage rather than missing capacity provisioning;
* the admission plan rebuilds a ``SpeechPlan`` from the full body text, whose own
  ceiling is 4096 code points, so 4097 characters are refused. #690 narrowed
  ``TtsJobInput.text.maxLength`` to that same ceiling, so the refusal is now the
  contract's declared 422 ``unsupported_parameter`` and happens before admission;
* a body over the deployment receive bound answers the declared 413
  ``PayloadTooLarge`` with code ``job_request_too_large`` (#703), which is a
  different code from both the 400 an unparsable document gets and the 422 above,
  so the two ceilings stay distinguishable by code.

Each case asserts the public status code, the stable error code and the exact
before/after state of the request's write surface. Rows come from the same
production transaction, so a refusal that still reserved capacity or wrote a Job
would fail here. The snapshot covers a fixed list, not "everything":

* row counts of the ten public/admission tables (``_counts``);
* ``workspace_quotas`` per workspace: ``held_attempts`` (the reservation counter
  admission increments in place), its CAS ``version``, and the columns bounding
  it -- ``max_attempts``, ``quarantined_attempts``, ``policy_revision``;
* ``speech_workspaces`` per workspace: the catalog row every quota hangs off
  (``display_name``, ``workspace_state``, ``metadata_revision``,
  ``policy_revision``);
* ``host_nodes`` per node: every ``held_<dimension>`` budget counter and its
  ``version`` CAS token (the admission role may ``UPDATE`` exactly those);
* ``engine_instances`` per ``(instance_id, instance_generation)``:
  ``held_slots`` and its ``version``.

Row counts alone cannot see a refusal that only committed one of those in-place
``UPDATE``s: no row appears or disappears, only counters and versions move. So
every refusal case compares the counter values, and every success case asserts
the expected movement -- one attempt held, one slot held on the known instance,
a positive host budget delta on the known node -- instead of "nothing changed".
This module pins current behaviour instead of inventing a new code: #690 landed
the text-ceiling contract correction, #702 the missing-quota 503 semantics, and
#703 the declared receive bound with its 413 and ``job_request_too_large``.

Boundaries -- what this module does not claim:

* The snapshot is a fixed column list over the four tables above plus the ten row
  counts. It does not cover ``resource_pools``, ``runtime_instance_heads`` /
  ``runtime_instance_intents``, ``submit_intents``, the dispatch/execution
  ledgers or any other table; a stray write there is not this module's evidence.
  Even inside the snapshotted tables it sees only those columns: for
  ``host_nodes`` the ``allocatable_*`` and ``quarantined_*`` families are not
  compared; for ``engine_instances`` only ``held_slots`` / ``version`` are, not
  its ``peak_*``, ``steady_*``, slot-bound or quarantine columns.
* The success assertion pins which fixture rows must move: node ``host-test`` and
  ``instance-new`` generation 1, the fixture's known ready identity. A product
  change that selects a different valid instance fails the case and is a scenario
  to re-derive, not a flake. It does not pin the per-dimension physical amounts:
  those derive from the stored runtime plan and are the admission unit's own
  assertions, so a dimension mix-up that keeps the total positive is not caught
  here.
* The refusal cases prove the request left no committed trace in that surface;
  they do not qualify network, commit-loss or failover behaviour. The same 400
  answers several refusing causes: this module separates the receive bound (413)
  and the declared text ceiling (422) from it, but not bad JSON from a bad
  ``Idempotency-Key``.
* The receive-bound case deliberately pins the current deployment default
  ``job_receive_max_bytes = 262144``. Raising that default fails its premise
  assertion with the observed value, which is a scenario change to re-derive,
  not a flake to delete.
* ``PLAN_TEXT_CEILING`` and ``CONTRACT_TEXT_CEILING`` are the same 4096 number
  since #690, so the text-ceiling case asserts one ceiling through two layers.
  #703 declared the receive bound and gave it 413 ``job_request_too_large``, so
  the two ceilings are pinned to two different codes here; moving one back onto
  the other's status or code must update these cases in the same change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import _counts, _seed, _store
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

PREFIX = "/v1/nexus/jobs"
WORKSPACE = "workspace-test"
BINDING = "binding-test"
VOICE = "voice-vivian"
TEXT = "synthetic public job"
# ``MAX_SPEECH_TEXT_CHARS`` in speech/domain/speech_synthesis.py.
PLAN_TEXT_CEILING = 4096
# ``TtsJobInput.text.maxLength`` in contracts/speech-openapi.yaml.
CONTRACT_TEXT_CEILING = 4_096
# ``settings.job_receive_max_bytes`` in backend/src/speechnexus_backend/settings.py.
RECEIVE_BOUND = 262_144
# The fixture's ready identity: ``_prepare_ready`` drives ``instance-new``
# generation 1 to ready, and admission selects instances by
# ``(instance_id, instance_generation)``. Success assertions pin these keys
# instead of "exactly one row moved", which would accept a write to any row.
HOST_NODE = "host-test"
INSTANCE = ("instance-new", 1)
INSTANCE_KEY = "/".join(str(part) for part in INSTANCE)
# ``host_nodes.topology_digest`` is ``sha256:`` plus 64 hex characters.
TOPOLOGY_DIGEST = "sha256:" + "a" * 64


def _app(admission: AdmissionDatabase) -> Any:
    """Assemble the same public surface production uses, admission pool included."""
    db = admission.runtime.foundation.database
    settings = _settings().model_copy(
        update={
            "database_schema": db.schema,
            "job_writer_database_url": SecretStr(db.url),
        }
    )
    store = _store(admission)
    return create_app(settings=settings, jobs=PublicJobs(store, store))


def _body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "kind": "tts",
        "model": BINDING,
        "input": {"text": TEXT},
        "voice": {"id": VOICE, "revision": "2"},
        "client_reference": "synthetic-reference",
    }
    body.update(changes)
    return body


def _wire_bytes(body: dict[str, object]) -> int:
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _grant_capacity(db: JobDatabase) -> None:
    """The synthetic seed reserves one of two attempts; give refusal cases headroom."""
    db.query("UPDATE workspace_quotas SET max_attempts = 64, held_attempts = 0")


def _add_spare_host(db: JobDatabase) -> None:
    """A second node row the admitted request must never touch.

    With a single node row, "the known node moved" and "some node moved" coincide,
    so the node identity assertion would be vacuous. The spare row keeps it
    falsifiable while staying inert for admission: no engine instance names it and
    its ``held_*`` counters start at zero.
    """
    db.query(
        "INSERT INTO host_nodes "
        "(host_node_id, memory_architecture, node_state, topology_digest, "
        "allocatable_unified_physical_memory_bytes, allocatable_device_memory_bytes, "
        "allocatable_host_memory_bytes, allocatable_cpu_decode_millicores, "
        "allocatable_host_pcm_buffer_bytes, allocatable_scratch_disk_bytes) "
        "VALUES ('host-spare', 'unified_memory', 'active', %s, "
        "1000000, 1000000, 1000000, 1000000, 1000000, 1000000)",
        (TOPOLOGY_DIGEST,),
    )


# ``workspace_quotas`` carries the reservation counter admission mutates in place:
# ``held_attempts`` is the number of admitted-but-unfinished attempts and
# ``version`` is its compare-and-set token. ``max_attempts``,
# ``quarantined_attempts`` and ``policy_revision`` bound that counter, so a
# refusal must leave every one of them exactly as it was.
QUOTA_COLUMNS = (
    "max_attempts",
    "held_attempts",
    "quarantined_attempts",
    "policy_revision",
    "version",
)
# ``speech_workspaces`` holds no attempt counter. The snapshot takes the columns
# the workspace update path can write (``workspaces_postgres.py`` updates
# ``display_name``, ``workspace_state`` and ``metadata_revision``) plus
# ``policy_revision``, which admission reads into the plan. The admission role has
# read-only access to this table, so comparing it is depth against a product write
# that bypasses the granted surface; the rows are snapshotted because the table is
# the FK target that makes a quota row resolvable.
WORKSPACE_COLUMNS = (
    "display_name",
    "workspace_state",
    "metadata_revision",
    "policy_revision",
)
# Admission reserves host budget in the same transaction as the quota:
# ``UPDATE host_nodes SET held_<dimension> = %s, version = version + 1``. The
# admission role is granted exactly those ``held_*`` columns and ``version``
# (``public_job_admission_grants.py``), so this is the complete in-place write
# surface on the node row.
HOST_COLUMNS = (*(("held_" + dimension) for dimension in DIMENSIONS), "version")
# ... and the instance slot counter: ``UPDATE engine_instances SET held_slots = %s,
# version = version + 1``. The admission role's instance grant is those two
# columns only.
INSTANCE_COLUMNS = ("held_slots", "version")


def _table_state(
    db: JobDatabase,
    table: str,
    keys: tuple[str, ...],
    columns: tuple[str, ...],
) -> dict[str, Any]:
    """Row count plus every snapshot column, keyed by the table's identity."""
    rows = {
        "/".join(str(row[name]) for name in keys): {name: row[name] for name in columns}
        for row in db.query(f"SELECT {', '.join((*keys, *columns))} FROM {table}")
    }
    return {"count": len(rows), "rows": rows}


def _snapshot(db: JobDatabase) -> dict[str, Any]:
    """Everything a create request must, or must not, change.

    ``_counts`` only sees rows that appear or disappear. Admission also moves
    counter columns in place -- one attempt on ``workspace_quotas``, host budget
    on ``host_nodes`` and one slot on ``engine_instances`` -- so a refusal that
    committed those ``UPDATE``s keeps every row count flat and leaves no trace in
    a rows-only snapshot. Comparing the values is what makes such a refusal
    visible; the ``speech_workspaces`` catalog rows are compared for the same
    reason.
    """
    return {
        "rows": _counts(db),
        "workspace_quotas": _table_state(db, "workspace_quotas", ("workspace_id",), QUOTA_COLUMNS),
        "speech_workspaces": _table_state(
            db, "speech_workspaces", ("workspace_id",), WORKSPACE_COLUMNS
        ),
        "host_nodes": _table_state(db, "host_nodes", ("host_node_id",), HOST_COLUMNS),
        "engine_instances": _table_state(
            db, "engine_instances", ("instance_id", "instance_generation"), INSTANCE_COLUMNS
        ),
    }


def _row_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    rows = before["rows"]
    return {table: after["rows"][table] - rows[table] for table in rows}


def _quota(db: JobDatabase, workspace_id: str = WORKSPACE) -> dict[str, Any]:
    """The reservation row an admitted create must have moved."""
    rows = db.query(
        f"SELECT {', '.join(QUOTA_COLUMNS)} FROM workspace_quotas WHERE workspace_id = %s",
        (workspace_id,),
    )
    assert len(rows) == 1, rows
    return dict(rows[0])


def _expect_row_moved(
    before: dict[str, Any],
    after: dict[str, Any],
    key: str,
    columns: tuple[str, ...],
    label: str,
) -> dict[str, int]:
    """The named row's counters advanced by one CAS step, every other row is still.

    Pinning the key matters: "exactly one row moved" also passes when admission
    charges a different node or instance than the one the request was admitted
    against. ``held`` counters are cumulative occupancy, so they may only grow,
    and the row's ``version`` must advance by exactly one; an unchanged named row
    means the reservation never landed where it belongs.
    """
    assert after["count"] == before["count"], f"{label}: row count changed"
    before_rows, after_rows = before["rows"], after["rows"]
    assert set(before_rows) == set(after_rows), f"{label}: identity set changed"
    assert key in before_rows, f"{label}: {key} is not a fixture row"
    previous, current = before_rows[key], after_rows[key]
    assert current["version"] == previous["version"] + 1, (
        f"{label}: {key} version {previous['version']} -> {current['version']}"
    )
    deltas = {name: current[name] - previous[name] for name in columns if name != "version"}
    assert all(delta >= 0 for delta in deltas.values()), f"{label}: {key} moved down: {deltas}"
    assert sum(deltas.values()) > 0, f"{label}: {key} counter did not move"
    others_moved = {
        name for name in after_rows if name != key and after_rows[name] != before_rows[name]
    }
    assert not others_moved, f"{label}: a row other than {key} moved: {sorted(others_moved)}"
    return deltas


def _expect_reservation(
    before: dict[str, Any], after: dict[str, Any], workspace_id: str = WORKSPACE
) -> None:
    """One attempt, one slot and host budget are now held by this admission.

    A success case that only asserted "nothing else changed" would pass against an
    admission that never touched the reservation rows, so the change itself is
    pinned: this workspace's ``held_attempts`` and CAS ``version`` advance by one,
    every *other* workspace quota row and the whole ``speech_workspaces`` catalog
    stay put, the known node's ``held_*`` budget grows with its ``version``,
    the known instance gains exactly one held slot, and the ten public/admission
    tables grow by one row each.
    """
    before_quotas = before["workspace_quotas"]["rows"]
    previous = before_quotas[workspace_id]
    after_quotas = after["workspace_quotas"]["rows"]
    assert after_quotas[workspace_id] == {
        **previous,
        "held_attempts": previous["held_attempts"] + 1,
        "version": previous["version"] + 1,
    }
    assert after["workspace_quotas"]["count"] == before["workspace_quotas"]["count"]
    # The reservation must be addressed to one workspace. Another quota row
    # moving in the same transaction is a cross-tenant leak, not a side effect.
    assert {key: row for key, row in after_quotas.items() if key != workspace_id} == {
        key: row for key, row in before_quotas.items() if key != workspace_id
    }, "a workspace other than the admitted one had its quota row changed"
    assert after["speech_workspaces"] == before["speech_workspaces"]
    slot_deltas = _expect_row_moved(
        before["engine_instances"],
        after["engine_instances"],
        INSTANCE_KEY,
        INSTANCE_COLUMNS,
        "instance slots",
    )
    assert slot_deltas == {"held_slots": 1}, f"unexpected instance slot movement: {slot_deltas}"
    _expect_row_moved(
        before["host_nodes"], after["host_nodes"], HOST_NODE, HOST_COLUMNS, "host budget"
    )
    assert _row_deltas(before, after) == dict.fromkeys(before["rows"], 1)


def _error(response: Response) -> tuple[str, str, bool]:
    document = response.json()
    assert isinstance(document, dict) and isinstance(document["error"], dict), document
    error = document["error"]
    assert isinstance(error["code"], str) and isinstance(error["type"], str)
    return error["code"], error["type"], bool(error["retryable"])


async def test_admitted_tts_create_writes_job_intent_and_reservation(
    admission_database: AdmissionDatabase,
) -> None:
    """A valid body returns 202 and every public/admission row commits once."""
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _add_spare_host(db)
    _grant_capacity(db)
    body = _body()
    before = _snapshot(db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        created = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-admitted-0001"}
        )
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["object"] == "nexus.job" and job["status"] == "queued"
        # The workspace survives the cross-layer hop: the response, the Job row and
        # the stored intent all name the workspace whose quota was reserved.
        assert job["workspace_id"] == WORKSPACE
        assert created.headers["Location"] == PREFIX + "/" + job["id"]
        admitted = _snapshot(db)
        # The reservation itself is asserted: ``held_attempts`` +1 for this
        # workspace, one slot on the known instance, a positive budget delta on
        # the known node, and one row in each public/admission table.
        _expect_reservation(before, admitted)
        intent = db.query(
            "SELECT request_digest, request_document FROM speech_job_intents WHERE job_id = %s",
            (job["id"],),
        )
        assert len(intent) == 1
        document = json.loads(intent[0]["request_document"])
        assert document["workspace_id"] == WORKSPACE
        assert document["input"] == {"text": TEXT}
        assert db.query("SELECT workspace_id FROM speech_jobs WHERE job_id = %s", (job["id"],)) == [
            {"workspace_id": WORKSPACE}
        ]
        assert db.query("SELECT job_id FROM speech_job_intents") == [{"job_id": job["id"]}]
        read = await client.get(PREFIX + "/" + job["id"])
        assert read.status_code == 200 and read.json()["id"] == job["id"]
        # A read is side-effect free: no row, no counter, no catalog column moves.
        assert _snapshot(db) == admitted


async def test_text_above_the_plan_ceiling_is_refused_without_rows(
    admission_database: AdmissionDatabase,
) -> None:
    """4096 code points are accepted and 4097 are refused by the contract.

    #690 narrowed ``TtsJobInput.text.maxLength`` to the admission plan's ceiling,
    so the refusal is the declared 422 ``unsupported_parameter`` and it happens at
    the contract check: the document never reaches admission, which is why no row
    and no in-place counter moves. A body over the receive bound instead answers
    the declared 413 ``job_request_too_large`` of #703.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _add_spare_host(db)
    _grant_capacity(db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _snapshot(db)
        at_limit = _body(input={"text": "中" * PLAN_TEXT_CEILING})
        accepted = await client.post(
            PREFIX, json=at_limit, headers={"Idempotency-Key": "synthetic-at-plan-ceiling"}
        )
        assert accepted.status_code == 202, accepted.text
        after_limit = _snapshot(db)
        _expect_reservation(before, after_limit)

        over_limit = _body(input={"text": "中" * (PLAN_TEXT_CEILING + 1)})
        # The body stays far below the receive bound, so only the plan's own text
        # ceiling can explain this refusal.
        assert _wire_bytes(over_limit) < _settings().job_receive_max_bytes
        refused = await client.post(
            PREFIX, json=over_limit, headers={"Idempotency-Key": "synthetic-over-plan-ceiling"}
        )
        assert refused.status_code == 422, refused.text
        code, kind, retryable = _error(refused)
        assert (code, kind, retryable) == ("unsupported_parameter", "invalid_request_error", False)
        # The refusal committed nothing: the accepted attempt is still the only
        # reservation, and no quota, node-budget or instance-slot counter moved.
        assert _snapshot(db) == after_limit


async def test_create_without_a_provisioned_workspace_quota_is_refused_without_rows(
    admission_database: AdmissionDatabase,
) -> None:
    """An unnamed workspace or a missing quota row is refused before admission.

    ``JobCreateBase.workspace_id`` is optional and the server substitutes
    ``workspace-default``; admission still reserves against ``workspace_quotas``,
    so the default workspace is only creatable where an operator registered a
    quota for it. Provisioning that row makes the same unnamed body succeed,
    which is the evidence that the refusal is the missing reservation target and
    not an invalid request document. Issue #702 owns the refusal's public shape:
    503 ``job_unavailable`` with a message and a non-retryable flag that describe
    a storage outage rather than an unprovisioned workspace.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _add_spare_host(db)
    unnamed = _body()
    del unnamed["workspace_id"]
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _snapshot(db)
        refused = await client.post(
            PREFIX, json=unnamed, headers={"Idempotency-Key": "synthetic-unnamed-workspace"}
        )
        assert refused.status_code == 503, refused.text
        assert _error(refused) == ("job_unavailable", "service_unavailable_error", False)
        assert _snapshot(db) == before

        # A workspace row without its quota row fails identically when named.
        db.query(
            "INSERT INTO speech_workspaces "
            "(workspace_id, display_name, workspace_state, created_at_us) "
            "VALUES ('workspace-quota-gap', 'test', 'active', 1)"
        )
        # The catalog row is this case's setup, so the baseline moves with it; a
        # missing quota beside an existing workspace must still reserve nothing.
        gap_before = _snapshot(db)
        gap = await client.post(
            PREFIX,
            json=_body(workspace_id="workspace-quota-gap"),
            headers={"Idempotency-Key": "synthetic-quota-gap"},
        )
        assert gap.status_code == 503, gap.text
        assert _error(gap) == ("job_unavailable", "service_unavailable_error", False)
        assert _snapshot(db) == gap_before

        # Naming a workspace that does not exist at all stays the client error.
        unknown = await client.post(
            PREFIX,
            json=_body(workspace_id="workspace-unknown"),
            headers={"Idempotency-Key": "synthetic-unknown-workspace"},
        )
        assert unknown.status_code == 422, unknown.text
        assert _error(unknown) == ("workspace_not_found", "invalid_request_error", False)
        assert _snapshot(db) == gap_before

        db.query(
            "INSERT INTO workspace_quotas "
            "(workspace_id, max_attempts, held_attempts, policy_revision) "
            "VALUES ('workspace-default', 8, 0, 'policy-test')"
        )
        provisioned_before = _snapshot(db)
        provisioned = await client.post(
            PREFIX, json=unnamed, headers={"Idempotency-Key": "synthetic-unnamed-workspace"}
        )
        assert provisioned.status_code == 202, provisioned.text
        assert provisioned.json()["workspace_id"] == "workspace-default"
        # The same unnamed body now reserves one attempt against the provisioned
        # default workspace, which is the evidence the refusal was the missing
        # reservation target and not an invalid document.
        _expect_reservation(provisioned_before, _snapshot(db), "workspace-default")


async def test_body_over_the_receive_bound_is_refused_before_any_write(
    admission_database: AdmissionDatabase,
) -> None:
    """A wire body above ``job_receive_max_bytes`` is refused before parse and write.

    The text is exactly at the plan ceiling and inside the contract ceiling, so
    the document itself would be admissible. Only the extra ``padding`` member --
    unknown to the contract, sized past the deployment's receive bound -- pushes
    the serialised body over that bound. That makes the bound falsifiable: with
    the bound in place the receive layer answers the declared 413
    ``PayloadTooLarge`` with code ``job_request_too_large`` before parsing, while
    an assembly without it lets the same body reach contract validation and
    answers 422 ``unsupported_parameter``. Loosening ``max_bytes`` at the one
    assembly point (the Job router in ``app.py``) or answering 400 again
    therefore turns this case red.

    Two deliberate couplings; neither is a flake:

    * ``RECEIVE_BOUND`` mirrors the deployment default. Raising
      ``job_receive_max_bytes`` fails the premise assertion below with the
      observed value: the contract-legal-but-oversized scenario disappears, so
      re-derive the case instead of deleting the guard.
    * ``PLAN_TEXT_CEILING`` / ``CONTRACT_TEXT_CEILING`` move with #690; the 413
      and ``job_request_too_large`` asserted here are #703's declared answer for
      this bound.

    Refusal is pre-write: no Job, intent, submission, operation, attempt,
    request, lease or outbox row appears, and no quota attempt, node-budget or
    instance-slot counter moves.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _add_spare_host(db)
    max_bytes = _settings().job_receive_max_bytes
    text = "中" * PLAN_TEXT_CEILING
    assert len(text) <= CONTRACT_TEXT_CEILING
    # ``padding`` is not a contract member; it exists only to grow the body past
    # the receive bound while the text stays admissible.
    body = _body(input={"text": text}, padding="a" * (RECEIVE_BOUND + 1024))
    assert max_bytes == RECEIVE_BOUND, (
        f"job_receive_max_bytes moved to {max_bytes}; re-derive this case instead of "
        "deleting the premise guard"
    )
    assert _wire_bytes(body) > max_bytes > 0
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _snapshot(db)
        refused = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-oversized-body"}
        )
        assert refused.status_code == 413, refused.text
        code, kind, retryable = _error(refused)
        assert (code, kind, retryable) == ("job_request_too_large", "invalid_request_error", False)
        # A parsed body would be 422 for the unknown member instead of 413, and a
        # written one would move at least one of the ten row counts or one of the
        # in-place counters.
        assert _snapshot(db) == before


async def test_same_key_replay_returns_the_original_job_without_new_rows(
    admission_database: AdmissionDatabase,
) -> None:
    """One key replays the stored Job; a fresh key is a second admitted Job.

    Replay is decided before any reservation, so it must neither reserve a second
    attempt nor write the public rows again -- and, since the snapshot covers the
    in-place counters, the replay also proves the quota, node-budget and
    instance-slot counters were not held twice. A different key with the same body
    is a genuinely new Job, which is what keeps the replay assertion meaningful.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _add_spare_host(db)
    _grant_capacity(db)
    body = _body()
    headers = {"Idempotency-Key": "synthetic-replay-key"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _snapshot(db)
        created = await client.post(PREFIX, json=body, headers=headers)
        assert created.status_code == 202, created.text
        original = created.json()
        after_create = _snapshot(db)
        _expect_reservation(before, after_create)

        replay = await client.post(PREFIX, json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == original["id"]
        assert replay.headers["Location"] == created.headers["Location"]
        # Byte-identical state: no second attempt, slot or host budget was held.
        assert _snapshot(db) == after_create
        assert _quota(db)["held_attempts"] == 1

        fresh = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-replay-key-2"}
        )
        assert fresh.status_code == 202, fresh.text
        assert fresh.json()["id"] != original["id"]
        # A different key is a second admission: exactly one more attempt, slot and
        # host budget, again addressed to the same known rows.
        _expect_reservation(after_create, _snapshot(db))
