"""The dispatcher's claim on the canonical bucket/lease tables, on a real database.

Issue #1176 段 B2+ replaces the old ``WITH ... FOR UPDATE SKIP LOCKED ... RETURNING``
claim with ``claim_batch``: a bucket lease, a ``job_state = 'claimed'`` CAS and a
canonical ``claim_expires_at``. The five oracles below are what the product needs from
that switch, and none of them can be proven against a fake or a single-threaded call:

1. two dispatcher instances racing the same ready Jobs claim each Job exactly once;
2. an expired lease is taken over and the old owner is fenced off on its next write;
3. a renewal extends the canonical lease and a second dispatcher cannot take the Job
   before it expires;
4. a dispatcher that dies after claiming does not lose the Job: after the lease
   expires the Job is reclaimed (``job.reclaim``) and dispatched again;
5. a foreign lane and a Job without an admission anchor are not claimed at all.

The schema is the real alembic head and the role is a fresh non-owner identity with the
same column grants provisioning installs, so a missing ``UPDATE (claim_expires_at)``
fails here with ``42501`` instead of passing on an owner connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchClaimLostError,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication
from speechnexus_portable_sql.claims import bucket_for
from speechnexus_portable_sql.clock import to_epoch_us

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_canonical_claim"
DIGEST = "sha256:" + "a" * 64
LANE = ("tts",)
#: A lane this build can assemble but which owns no Job in these cases. ``voice_validation``
#: requires admission, so it is a legal queue lane; nothing seeded here belongs to it.
FOREIGN_LANE = ("voice_validation",)


@dataclass
class ClaimSandbox:
    url: str = field(repr=False)
    schema: str
    role: str
    gateway_role: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    jobs: PublicJobs = field(repr=False)
    pools: list[Any] = field(default_factory=list)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return list(cursor.fetchall()) if cursor.description else []

    def pool(self, purpose: str) -> Any:
        """One pool per caller, so two dispatchers really are two instances."""

        pool = speech_pool(
            make_conninfo(self.url, user=self.role, password=PASSWORD),
            schema=self.schema,
            max_size=4,
            name=f"claim-{purpose}-" + uuid.uuid4().hex[:8],
        )
        pool.open()
        self.pools.append(pool)
        return pool

    def queue(
        self, *, kinds: tuple[str, ...] = LANE, admission: bool = True
    ) -> PostgresDispatchQueue:
        return PostgresDispatchQueue(self.pool("queue"), kinds=kinds, require_admission=admission)

    def state_store(self) -> PostgresDispatchStateStore:
        return PostgresDispatchStateStore(self.pool("state"))

    def canonical(self, job_id: str) -> dict[str, Any]:
        rows = self.query(
            "SELECT claim_owner_id, claim_version, job_state, claim_expires_at "
            "FROM coordination_jobs WHERE job_id = %s",
            (job_id,),
        )
        assert len(rows) == 1, rows
        return rows[0]

    def public(self, job_id: str) -> dict[str, Any]:
        rows = self.query(
            "SELECT state, status_version FROM speech_jobs WHERE job_id = %s", (job_id,)
        )
        assert len(rows) == 1, rows
        return rows[0]


def _seed_binding(admin: psycopg.Connection[dict[str, Any]]) -> None:
    admin.execute(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) "
        "VALUES ('tts-quality', '1')"
    )
    admin.execute(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, 'tts-quality', 1, '{}', %s)",
        (DIGEST, DIGEST),
    )
    definition = {"binding_id": "tts-quality", "tasks": ["tts_file"], "release_tier": "production"}
    admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
        "definition_digest, request_digest, created_at_us) "
        "VALUES ('tts-quality', '1', 'binding-rev-1', %s, 1, %s, %s, %s, 1)",
        (DIGEST, json.dumps(definition), DIGEST, DIGEST),
    )
    with admin.transaction(), admin.cursor() as cursor:
        assert append_publication(
            cursor,
            Publication("binding", "tts-quality", 1, "binding-rev-1", 1),
            expected_current_revision=None,
            expected_catalog_revision=1,
        )
        assert append_publication(cursor, Publication("voice", "voice-vivian", 2, "voice-rev-2", 2))


@pytest.fixture
def sandbox() -> Iterator[ClaimSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; a skip is not claim evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_canonical_" + suffix, "sn_canonical_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = portable_pg_connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    sandbox: ClaimSandbox | None = None
    try:
        # The production shape: one login identity for the writer/dispatcher paths and a
        # separate gateway identity, because the coordination grant set refuses to hand
        # one role both sides of the fence. Missing columns fail here as ``42501``.
        gateway_role = role + "_gw"
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        admin.execute(f'CREATE ROLE "{gateway_role}"')
        for grant in (
            *job_runtime_grants(schema, role),
            *job_dispatch_grants(schema, role),
            *public_job_grants(schema, role),
            *public_job_submission_grants(schema, role),
            *coordination_runtime_grants(schema, speech_role=role, gateway_role=gateway_role),
            # WRITER_BASELINE_SELECT: the API's Job writer reads publication and asset
            # facts that no Job grant function declares.
            f"GRANT SELECT ON speech_workspaces, speech_publications, "
            f"speech_publication_heads, speech_catalog_state, speech_publication_retirements, "
            f"speech_binding_revisions, speech_assets, speech_pronunciation_revisions "
            f'TO "{role}"',
        ):
            admin.execute(grant)
        _seed_binding(admin)
        _seed_capacity(admin)
        writer_pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="claim-writer-" + suffix,
        )
        writer_pool.open()
        adapter = PostgresPublicJobs(writer_pool, timeout_seconds=3)
        sandbox = ClaimSandbox(
            url,
            schema,
            role,
            gateway_role,
            admin,
            PublicJobs(adapter, adapter),
            pools=[writer_pool],
        )
        yield sandbox
    finally:
        if sandbox is not None:
            for pool in sandbox.pools:
                pool.close()
        for identity in (role, role + "_gw"):
            admin.execute(f'DROP OWNED BY "{identity}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        # Every identity is created before the yield, so both names are always droppable.
        for identity in (role, role + "_gw"):
            admin.execute(f'DROP ROLE IF EXISTS "{identity}"')
        admin.close()


def _body(text: str = "canonical claim job") -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": text},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "canonical-" + uuid.uuid4().hex[:8],
    }


def _submit_admitted(sandbox: ClaimSandbox, index: int) -> str:
    """A Job the production lane can claim: writer rows plus the stage lease."""

    job_id = _submit(sandbox, index)
    _complete_anchor(sandbox.admin, job_id)
    return job_id


def _submit(sandbox: ClaimSandbox, index: int) -> str:
    created = sandbox.jobs.submit(
        _body(f"canonical claim {index}"),
        request_id=f"request-canonical-{uuid.uuid4().hex[:12]}",
        idempotency_key=None,
    )
    return str(created.job_id)


def _anchorless_job(sandbox: ClaimSandbox, job_id: str, *, kind: str = "tts") -> None:
    """A due, bucketed Job whose admission never wrote a document, attempt or lease.

    The public row and the coordination row are exactly what a legacy assembly left
    behind; ``require_admission`` is the only thing that can exclude it.
    """

    now_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    sandbox.admin.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "idempotency_key_digest, state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-canonical', %s, 'tts-quality', %s, %s, 'pending', 1, %s, %s)",
        (job_id, kind, DIGEST, DIGEST, now_us, now_us),
    )
    sandbox.admin.execute(
        "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, initial_event_id, "
        "initial_event_digest, request_digest, bucket_no, due_at, job_state, claim_version, "
        "last_event_seq) VALUES (%s, %s, %s, %s, %s, %s, %s, "
        "to_timestamp(%s / 1000000.0) AT TIME ZONE 'UTC', 'pending', 0, 0)",
        (
            job_id,
            job_id,
            job_id + "-attempt",
            job_id + "-event",
            DIGEST,
            DIGEST,
            int(bucket_for(job_id)),
            now_us - 1_000_000,
        ),
    )


CAPACITY = {
    "pool_id": "pool-canonical",
    "host_node_id": "node-canonical",
    "instance_id": "instance-canonical",
    "generation": 1,
    "gateway_id": "gateway-canonical",
}


def _seed_capacity(admin: psycopg.Connection[dict[str, Any]]) -> None:
    """The engine instance a stage lease points at, as the runtime fixture would leave it."""

    admin.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES (%s, 'batch', 'active')",
        (CAPACITY["pool_id"],),
    )
    admin.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest) "
        "VALUES (%s, 'unified', 'ready', %s)",
        (CAPACITY["host_node_id"], DIGEST),
    )
    admin.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
        "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
        "management_mode, loading_policy, config_digest, instance_state, max_slots) "
        "VALUES (%s, %s, %s, %s, %s, 'binding-rev-1', 'manifest-canonical', "
        "'template-canonical', 'controller', 'eager', %s, 'ready', 4)",
        (
            CAPACITY["instance_id"],
            CAPACITY["generation"],
            CAPACITY["host_node_id"],
            CAPACITY["pool_id"],
            CAPACITY["gateway_id"],
            DIGEST,
        ),
    )


def _complete_anchor(admin: psycopg.Connection[dict[str, Any]], job_id: str) -> None:
    """Complete the admission anchor: quota, attempt and the reserved stage lease.

    The writer that runs without an admission pool writes the operation row only. A
    claimable production Job also carries the attempt and the stage lease admission's own
    transaction writes (``admission_sql``), so this adds exactly those two rows. A Job
    missing them is the "anchor from an older release" the production lane must leave
    unreachable.
    """

    row = admin.execute(
        "SELECT o.current_attempt_id, s.workspace_id, s.request_digest "
        "FROM admission_operations AS o JOIN speech_jobs AS s ON s.job_id = o.operation_id "
        "WHERE o.operation_id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None, job_id
    attempt_id = row["current_attempt_id"]
    admin.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, policy_revision) "
        "VALUES (%s, 8, 'policy-rev-1') "
        "ON CONFLICT (workspace_id) DO NOTHING",
        (row["workspace_id"],),
    )
    admin.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) "
        "VALUES (%s, %s, %s, 'binding-rev-1', 'manifest-canonical', %s, 'policy-rev-1', "
        "'batch', %s, %s)",
        (
            job_id,
            attempt_id,
            row["workspace_id"],
            DIGEST,
            row["request_digest"],
            json.dumps(["stage-tts"]),
        ),
    )
    admin.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, deadline_at_us) "
        "VALUES (%s, %s, %s, 'stage-tts', %s, %s, %s, %s, %s, 1, 1, %s)",
        (
            f"{job_id}-stage-lease",
            job_id,
            attempt_id,
            CAPACITY["host_node_id"],
            CAPACITY["instance_id"],
            CAPACITY["generation"],
            CAPACITY["pool_id"],
            CAPACITY["gateway_id"],
            int(datetime.now(UTC).timestamp() * 1_000_000) + 60_000_000,
        ),
    )


def _claims(
    queue: PostgresDispatchQueue, worker_id: str, lease_ms: int, limit: int
) -> list[ClaimedJob]:
    return asyncio.run(queue.claim_due(worker_id, lease_ms, limit))


def _race(
    first: PostgresDispatchQueue, second: PostgresDispatchQueue, limit: int
) -> tuple[list[ClaimedJob], list[ClaimedJob]]:
    async def go() -> tuple[list[ClaimedJob], list[ClaimedJob]]:
        alpha, beta = await asyncio.gather(
            first.claim_due("dispatcher-alpha", 30_000, limit),
            second.claim_due("dispatcher-beta", 30_000, limit),
        )
        return alpha, beta

    return asyncio.run(go())


def test_two_dispatchers_claim_each_ready_job_exactly_once(sandbox: ClaimSandbox) -> None:
    """Oracle 1: the union of two concurrent claims has no duplicate and loses nothing."""

    ready = {_submit_admitted(sandbox, index) for index in range(4)}
    alpha, beta = _race(sandbox.queue(), sandbox.queue(), limit=2)

    claimed = [claim.job.job_id for claim in (*alpha, *beta)]
    assert len(claimed) == len(set(claimed)), claimed
    assert set(claimed) == ready
    assert sorted((len(alpha), len(beta))) == [2, 2], (len(alpha), len(beta))

    # Exactly one canonical owner per Job, and every Job stays claimed: a second claim
    # would have bumped ``claim_version`` or left a different owner behind.
    owners = set()
    for job_id in ready:
        row = sandbox.canonical(job_id)
        assert row["job_state"] == "claimed", row
        assert row["claim_version"] == 1, row
        assert row["claim_owner_id"] in ("dispatcher-alpha", "dispatcher-beta"), row
        assert row["claim_expires_at"] is not None, row
        owners.add(row["claim_owner_id"])
    assert owners == {"dispatcher-alpha", "dispatcher-beta"}


def test_an_expired_lease_is_taken_over_and_fences_the_old_owner(sandbox: ClaimSandbox) -> None:
    """Oracle 2: takeover happens, ``renew`` returns None and ``finish`` raises."""

    job_id = _submit_admitted(sandbox, 1)
    states = sandbox.state_store()
    first = _claims(sandbox.queue(), "dispatcher-alpha", 40, 1)
    assert [claim.job.job_id for claim in first] == [job_id]
    old = first[0]

    # The worker stalls longer than its 40 ms lease. The claim is committed, so this is
    # a dead lease on a row nobody is writing.
    time.sleep(0.1)
    second = _claims(sandbox.queue(), "dispatcher-beta", 30_000, 1)
    assert [claim.job.job_id for claim in second] == [job_id]
    new = second[0]
    # claim 0->1, ``job.reclaim`` 1->2, takeover 2->3: the reclaim bump is the fence.
    assert new.claim_version == 3 and old.claim_version == 1

    assert asyncio.run(states.renew(old, 30_000)) is None
    with pytest.raises(DispatchClaimLostError):
        asyncio.run(states.finish(old, "succeeded", result_json=None, error_code="synthetic"))

    # The fenced writer left no public trace: no running transition, no terminal event.
    # The only event is the one the writer's submit recorded.
    assert sandbox.public(job_id) == {"state": "pending", "status_version": 1}
    events = sandbox.query(
        "SELECT state FROM speech_job_events WHERE job_id = %s ORDER BY event_seq", (job_id,)
    )
    assert len(events) == 1, events
    assert events[0]["state"] not in {"running", "succeeded", "failed"}, events
    row = sandbox.canonical(job_id)
    assert (row["claim_owner_id"], row["claim_version"], row["job_state"]) == (
        "dispatcher-beta",
        3,
        "claimed",
    )
    # The new owner still holds a working claim.
    renewed = asyncio.run(states.renew(new, 30_000))
    assert renewed is not None and renewed.claim_version == 3


def test_renewal_extends_the_canonical_lease_and_blocks_another_dispatcher(
    sandbox: ClaimSandbox,
) -> None:
    """Oracle 3: a renewed lease is the stored one, and it is not reclaimable early."""

    job_id = _submit_admitted(sandbox, 2)
    states = sandbox.state_store()
    claimed = _claims(sandbox.queue(), "dispatcher-alpha", 50, 1)
    assert [claim.job.job_id for claim in claimed] == [job_id]

    renewed = asyncio.run(states.renew(claimed[0], 30_000))
    assert renewed is not None
    stored = sandbox.canonical(job_id)["claim_expires_at"]
    assert to_epoch_us(stored) == renewed.lease_expires_at_us
    assert renewed.lease_expires_at_us > claimed[0].lease_expires_at_us

    # Long past the original 50 ms lease, far before the renewed one.
    time.sleep(0.1)
    assert _claims(sandbox.queue(), "dispatcher-beta", 30_000, 1) == []
    row = sandbox.canonical(job_id)
    assert (row["claim_owner_id"], row["claim_version"], row["job_state"]) == (
        "dispatcher-alpha",
        1,
        "claimed",
    )


def test_a_crashed_dispatcher_does_not_lose_the_job(sandbox: ClaimSandbox) -> None:
    """Oracle 4: after the lease expires the Job is reclaimed and dispatched again."""

    job_id = _submit_admitted(sandbox, 3)
    # The instance whose process dies: only its own pool goes away below.
    crashed_pool = sandbox.pool("crashed")
    crashed = PostgresDispatchQueue(crashed_pool, kinds=LANE, require_admission=True)
    first = _claims(crashed, "dispatcher-alpha", 40, 1)
    assert [claim.job.job_id for claim in first] == [job_id]
    # The process dies between the claim commit and any transition: no renew, no finish,
    # no release of the Job row. Only the pool goes away.
    crashed_pool.close()
    row = sandbox.canonical(job_id)
    assert (row["claim_owner_id"], row["claim_version"], row["job_state"]) == (
        "dispatcher-alpha",
        1,
        "claimed",
    )

    time.sleep(0.1)
    again = _claims(sandbox.queue(), "dispatcher-beta", 30_000, 1)
    assert [claim.job.job_id for claim in again] == [job_id]
    # 1 -> 2 is ``job.reclaim`` putting the dead claim back to ``pending``; 2 -> 3 is the
    # new claim. Without the reclaim the Job would stay ``claimed`` forever.
    reclaimed = again[0]
    assert reclaimed.claim_version == 3
    assert reclaimed.job.job_id == job_id and reclaimed.job.state == "pending"
    assert asyncio.run(sandbox.state_store().mark_running(reclaimed.job)).state == "running"


def test_the_production_lane_claims_only_complete_admitted_anchors(
    sandbox: ClaimSandbox,
) -> None:
    """Oracle 5: lane and admission anchors are claim predicates, not post-filters."""

    admitted = _submit_admitted(sandbox, 4)
    # The same Job facts without the stage lease: what an older release's anchor looks like.
    partial = _submit(sandbox, 5)
    orphan = "job-anchorless-" + uuid.uuid4().hex[:8]
    _anchorless_job(sandbox, orphan)

    # A lane that owns none of these Jobs claims nothing even though ``admitted`` is
    # complete and due, so the positive case below is evidence for the filter, not luck.
    assert _claims(sandbox.queue(kinds=("asr",)), "dispatcher-foreign", 30_000, 8) == []
    for job_id in (admitted, partial, orphan):
        assert sandbox.canonical(job_id) == {
            "claim_owner_id": None,
            "claim_version": 0,
            "job_state": "pending",
            "claim_expires_at": None,
        }, job_id

    # The production lane claims exactly the Job whose anchor is complete.
    claimed = _claims(sandbox.queue(), "dispatcher-own", 30_000, 8)
    assert [claim.job.job_id for claim in claimed] == [admitted]
    for job_id in (partial, orphan):
        row = sandbox.canonical(job_id)
        assert (row["claim_owner_id"], row["claim_version"], row["job_state"]) == (
            None,
            0,
            "pending",
        ), (job_id, row)

    # Without the admission filter the two are claimable: bucket, due_at, state and lane
    # all match, so the predicate is the only thing that excluded them above.
    unfiltered = _claims(sandbox.queue(admission=False), "dispatcher-unfiltered", 30_000, 8)
    assert {claim.job.job_id for claim in unfiltered} == {partial, orphan}
