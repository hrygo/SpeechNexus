"""The readiness report and a create answer one refusal from the same stored rows.

The failure this module exists to catch is not a wrong message: it is a report that
says ``admitting`` while every create is refused. ``#857`` proved that defect cannot
be caught by hand-built facts -- its equivalence test compared two models that were
missing the whole selection layer, and the real database answered differently -- so
every row below drives the production adapter and the real coordination rows, then
compares both surfaces on the same instant.

Each row mutates one deployment fact, asks for a create and reads the report:

* the create answer must be the row's declared public code (or a created Job);
* ``report.cause`` must be that same code's cause, and
  ``report.status == "admitting"`` exactly when the create succeeded.

The report runs ``decide_create_admission`` -- the predicate the create transaction
runs -- so a row cannot pass by agreeing with a second copy of the rules. The probe
set is the create path's own selection layer: the first ready instance of each
binding. The last three cases build the deployment shape the single-instance rows
cannot express (two ready instances behind one binding, and two bindings), because
the report used to scan *every* ready instance and answer ``admitting`` from a
healthy sibling of the instance a create would pin (#857 third round, #863).

Boundaries: PostgreSQL with the shipped schema and roles. The rows that need a
second instance or a second binding build them through the product's own
``create_runtime_intent`` / ``observe_runtime`` ports and catalog publication, not
by hand-writing the engine tables. It is not a real engine, gateway or GPU: readiness
receipts are rewritten into the state a renewal would produce rather than elapsed by
a clock. The workspace refusals are answered by the service before admission is
consulted, so they are pinned here by name on both surfaces. MySQL/InnoDB and
concurrency are covered by their own suites.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.fake.runtime_intents import BINDING as RUNTIME_BINDING
from tests.fake.runtime_intents import COMMAND as RUNTIME_COMMAND
from tests.fake.runtime_intents import PLAN as RUNTIME_PLAN
from tests.fake.runtime_intents import REQUEST as RUNTIME_REQUEST
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import (
    runtime_database as runtime_database,
)
from tests.integration.test_public_job_admission_http_pg import (
    BINDING,
    HOST_NODE,
    INSTANCE,
    PREFIX,
    TOPOLOGY_DIGEST,
    WORKSPACE,
    _app,
    _body,
    _error,
)
from tests.integration.test_public_job_atomic_admission_pg import _job, _seed, _store
from tests.integration.test_runtime_observations import _command, _receipt, _wire

from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication

REPORT_CAUSE_BY_CREATE_CODE: dict[str, str] = {
    "engine_not_ready": "engine_receipt_expired",
    "engine_state_mismatch": "engine_state_mismatch",
    "engine_resource_disabled": "engine_resource_disabled",
    "job_capacity_exhausted": "capacity_exhausted",
    "workspace_not_found": "workspace_not_found",
    "workspace_disabled": "workspace_disabled",
    "workspace_not_configured": "workspace_not_configured",
    "job_unavailable": "storage_unavailable",
}


pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


REPORT_PATH = PREFIX + "/admission-readiness"


def _query(db: Any, statement: str, parameters: tuple[object, ...] = ()) -> None:
    db.query(statement, parameters)


ROWS: tuple[tuple[str, Callable[[Any, int], None] | None, str | None], ...] = (
    # (id, mutation, expected create public code; ``None`` means "a Job is created")
    ("healthy", None, None),
    (
        "node-quarantined",
        lambda db, now: _query(
            db,
            "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
            (HOST_NODE,),
        ),
        "engine_resource_disabled",
    ),
    (
        "quota-full",
        lambda db, now: _query(
            db,
            "UPDATE workspace_quotas SET held_attempts = max_attempts, "
            "quarantined_attempts = 0 WHERE workspace_id = %s",
            (WORKSPACE,),
        ),
        "job_capacity_exhausted",
    ),
    (
        "slots-full",
        lambda db, now: _query(
            db,
            "UPDATE engine_instances SET held_slots = max_slots "
            "WHERE instance_id = %s AND instance_generation = %s",
            INSTANCE,
        ),
        "job_capacity_exhausted",
    ),
    (
        "pool-draining",
        lambda db, now: _query(db, "UPDATE resource_pools SET pool_state = 'draining'"),
        "engine_resource_disabled",
    ),
    (
        "undecodable-intent",
        lambda db, now: _query(
            db,
            "UPDATE runtime_instance_intents SET request_document = %s",
            ("{}",),
        ),
        "engine_state_mismatch",
    ),
    (
        "no-ready-instance",
        lambda db, now: _query(
            db,
            "UPDATE engine_instances SET instance_state = 'draining' "
            "WHERE instance_id = %s AND instance_generation = %s",
            INSTANCE,
        ),
        "job_capacity_exhausted",
    ),
    # Receipt expiry needs the renewal-shaped rewrite the product itself produces,
    # not a clock wait: admission also requires verified_at_us <= ready_at_us <= now.
    ("receipt-expired", None, "engine_not_ready"),
)


def _expire_receipt(admission: AdmissionDatabase) -> None:
    from tests.integration.test_public_job_admission_http_pg import _rewrite_receipt

    db = admission.runtime.foundation.database
    now_us = admission.runtime.now()
    _rewrite_receipt(db, verified_at_us=now_us - 1000, expires_at_us=now_us - 1)


# The fixture has one binding and one ready instance; these rows add the second of
# each. ``instance-z`` sorts after ``instance-new`` so the create keeps pinning the
# first instance, which is the arrangement a report that scanned every ready
# instance could not see (#857 third round).
OTHER_BINDING = "binding-other"
OTHER_INSTANCE = ("instance-z", 1)
OTHER_HOST = "host-z"
OTHER_POOL = "asr-realtime"
OTHER_GATEWAY = "gateway-z"
OTHER_DIGEST = "sha256:" + "c" * 64


def _register_ready_instance(
    admission: AdmissionDatabase,
    *,
    binding_id: str,
    instance_id: str,
    host_node_id: str,
    pool_id: str,
    gateway_id: str,
) -> None:
    """Register and qualify one more ready instance through the product ports.

    ``_prepare_ready`` only produces the fixture's first instance, so a second
    ready instance -- the shape the single-instance rows cannot express -- is built
    with the same ``create_runtime_intent`` / ``observe_runtime`` pair an operator
    registration runs. The host and pool rows are inserted first because the
    fixture has one of each; a second instance has to sit on its own node for the
    per-node mutations to mean anything.
    """
    runtime = admission.runtime
    db = runtime.foundation.database
    if not db.query("SELECT 1 FROM resource_pools WHERE pool_id = %s", (pool_id,)):
        db.query(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state, owner_id, "
            "fencing_token, owner_state, owner_expires_at_us) "
            "VALUES (%s, 'realtime', 'active', %s, 7, 'active', %s)",
            (pool_id, RUNTIME_COMMAND.owner_id, runtime.now() + 60000000),
        )
    if not db.query("SELECT 1 FROM host_nodes WHERE host_node_id = %s", (host_node_id,)):
        db.query(
            "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, "
            "topology_digest, allocatable_unified_physical_memory_bytes, "
            "allocatable_device_memory_bytes, allocatable_host_memory_bytes, "
            "allocatable_cpu_decode_millicores, allocatable_host_pcm_buffer_bytes, "
            "allocatable_scratch_disk_bytes) "
            "VALUES (%s, 'unified_memory', 'active', %s, "
            "1000000, 0, 0, 1000000, 1000000, 1000000)",
            (host_node_id, TOPOLOGY_DIGEST),
        )
    plan = replace(
        RUNTIME_PLAN,
        binding=replace(
            RUNTIME_BINDING,
            target_node_id=host_node_id,
            pool_id=pool_id,
            gateway_id=gateway_id,
            binding_revision_id=binding_id,
        ),
    )
    request = replace(
        RUNTIME_REQUEST,
        intent_id="runtime-" + instance_id,
        instance_id=instance_id,
        binding=plan.binding,
    )
    created = runtime.port.create_runtime_intent(
        replace(RUNTIME_COMMAND, request=request, approved_plan=plan)
    )
    assert created.outcome == "committed", created
    wire = _wire(runtime, request)
    assert (
        runtime.port.observe_runtime(_command(wire, receipt=_receipt(runtime, request))).outcome
        == "committed"
    )


def _publish_other_binding(admission: AdmissionDatabase) -> None:
    """Publish a second complete binding, cloned from the fixture's binding.

    The probe set is one instance per binding, so a second binding is what
    separates "this binding's pin target is refused" from "every create is
    refused"; ``_seed`` publishes only ``binding-test`` and the deliberately
    incomplete binding.
    """
    db = admission.runtime.foundation.database
    source = db.query(
        "SELECT definition_json FROM speech_binding_revisions WHERE binding_id = %s",
        (BINDING,),
    )[0]["definition_json"]
    definition = json.loads(source)
    definition["binding_id"] = OTHER_BINDING
    db.query(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES (%s, '1')",
        (OTHER_BINDING,),
    )
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, %s, 1, '{}', %s)",
        (OTHER_DIGEST, OTHER_BINDING, OTHER_DIGEST),
    )
    db.query(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
        "definition_digest, request_digest, created_at_us) "
        "VALUES (%s, '1', %s, %s, 1, %s, %s, %s, 1)",
        (
            OTHER_BINDING,
            OTHER_BINDING,
            OTHER_DIGEST,
            json.dumps(definition),
            OTHER_DIGEST,
            OTHER_DIGEST,
        ),
    )
    # ``_seed`` leaves the catalog at revision 3 (one binding, the incomplete one and
    # one voice), so the fourth publication is this binding.
    with db.admin.transaction(), db.admin.cursor() as cursor:
        assert append_publication(
            cursor,
            Publication("binding", OTHER_BINDING, 1, OTHER_BINDING, 1),
            expected_current_revision=None,
            expected_catalog_revision=4,
        )


def _ready_pins(admission: AdmissionDatabase, binding_id: str) -> list[str]:
    """The instances a create for ``binding_id`` could pin, in selection order."""
    rows = admission.runtime.foundation.database.query(
        "SELECT i.instance_id FROM engine_instances AS i "
        "JOIN runtime_instance_heads AS h ON h.instance_id = i.instance_id "
        "AND h.current_generation = i.instance_generation "
        "WHERE i.binding_revision_id = %s AND i.instance_state = 'ready' "
        "AND h.current_prepare_intent_id IS NOT NULL "
        "ORDER BY i.instance_id, i.instance_generation",
        (binding_id,),
    )
    return [str(row["instance_id"]) for row in rows]


def _quarantine(admission: AdmissionDatabase, host_node_id: str) -> None:
    admission.runtime.foundation.database.query(
        "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
        (host_node_id,),
    )


@pytest.mark.parametrize(
    ("row_id", "mutation", "create_code"),
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_the_report_and_a_create_answer_the_same_refusal(
    admission_database: AdmissionDatabase,
    row_id: str,
    mutation: Callable[[Any, int], None] | None,
    create_code: str | None,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    if row_id == "receipt-expired":
        _expire_receipt(admission_database)
    elif mutation is not None:
        mutation(db, admission_database.runtime.now())
    store = _store(admission_database)
    # The report is read first: a create reserves the attempt and the slot, so the
    # comparison must be two answers to the same instant, not one answer and then a
    # deployment another create has already filled.
    report = store.admission_readiness(WORKSPACE)
    created = store.create(_job())
    # `status` and `cause` are one answer split in two.
    assert (report.status == "admitting") is (report.cause is None)
    print(
        f"ROW {row_id:<20} create={created.error_code or created.outcome:<26} "
        f"status={report.status.value:<9} cause={report.cause.value if report.cause else None}"
    )
    if create_code is None:
        assert created.outcome == "created", (row_id, created.error_code)
        assert report.status == "admitting", (row_id, report.cause)
        assert report.detail is None
    else:
        assert created.error_code == create_code, (row_id, created.error_code)
        assert report.status == "degraded", (row_id, report.cause)
        assert (
            report.cause is not None
            and report.cause.value == REPORT_CAUSE_BY_CREATE_CODE[create_code]
        ), (row_id, report.cause)
        assert report.detail


def test_the_report_probes_the_instance_a_create_pins_not_a_healthy_sibling(
    admission_database: AdmissionDatabase,
) -> None:
    """Two ready instances, one binding, the pinned one's node quarantined.

    This is the #857 third-round defect: the create pins the smaller instance id,
    whose node is quarantined, so it answers ``engine_resource_disabled``; a report
    that probes every ready instance reaches the healthy sibling and says
    ``admitting``. Both possible verdicts are present in the data, so only the
    selection layer decides which one the report publishes.
    """
    _seed(admission_database)
    _register_ready_instance(
        admission_database,
        binding_id=BINDING,
        instance_id=OTHER_INSTANCE[0],
        host_node_id=OTHER_HOST,
        pool_id=OTHER_POOL,
        gateway_id=OTHER_GATEWAY,
    )
    _quarantine(admission_database, HOST_NODE)
    # Premise: both instances are ready pin candidates for the same binding, and the
    # create selects ``instance-new`` because it sorts first.
    assert _ready_pins(admission_database, BINDING) == ["instance-new", "instance-z"]

    store = _store(admission_database)
    report = store.admission_readiness(WORKSPACE)
    created = store.create(_job())
    print(
        f"ROW {'two-instances':<20} create={created.error_code or created.outcome:<26} "
        f"status={report.status.value:<9} cause={report.cause.value if report.cause else None}"
    )
    # The create is the ground truth here: the report must answer the refusal the
    # create actually produced, not the one a healthy sibling would have produced.
    assert created.outcome == "rejected"
    assert created.error_code == "engine_resource_disabled", created.error_code
    assert report.status == "degraded", report.cause
    assert report.cause is not None and report.cause.value == "engine_resource_disabled"
    # ``instances[]`` names the probe set, so the healthy sibling is not in it: a
    # report that lists it would be reporting an instance admission never selects.
    assert [row.instance_id for row in report.instances] == ["instance-new"]


def test_one_refused_binding_refuses_the_report_though_another_binding_works(
    admission_database: AdmissionDatabase,
) -> None:
    """Two bindings; only the other binding's pin target is disabled.

    The report has no binding argument, so it must answer for the deployment: if
    one binding's pin target is refused, a create for that binding is refused and
    the report cannot say ``admitting`` because the sibling binding works.
    """
    _seed(admission_database)
    _publish_other_binding(admission_database)
    _register_ready_instance(
        admission_database,
        binding_id=OTHER_BINDING,
        instance_id=OTHER_INSTANCE[0],
        host_node_id=OTHER_HOST,
        pool_id=OTHER_POOL,
        gateway_id=OTHER_GATEWAY,
    )
    _quarantine(admission_database, OTHER_HOST)
    assert _ready_pins(admission_database, OTHER_BINDING) == ["instance-z"]

    store = _store(admission_database)
    report = store.admission_readiness(WORKSPACE)
    refused = store.create(_job(model=OTHER_BINDING))
    healthy = store.create(_job(model=BINDING, key="synthetic-idem-0002"))
    print(
        f"ROW {'two-bindings-one-ref':<20} create={refused.error_code or refused.outcome:<26} "
        f"status={report.status.value:<9} cause={report.cause.value if report.cause else None}"
    )
    # The reported list is in binding order, and the first refusal is the cause.
    assert [row.instance_id for row in report.instances] == ["instance-z", "instance-new"]
    assert refused.outcome == "rejected" and refused.error_code == "engine_resource_disabled"
    assert healthy.outcome == "created", healthy.error_code
    assert report.status == "degraded", report.cause
    assert report.cause is not None and report.cause.value == "engine_resource_disabled"


def test_two_usable_bindings_report_admitting(admission_database: AdmissionDatabase) -> None:
    """Both bindings pin a healthy instance; every create would be admitted."""
    _seed(admission_database)
    _publish_other_binding(admission_database)
    _register_ready_instance(
        admission_database,
        binding_id=OTHER_BINDING,
        instance_id=OTHER_INSTANCE[0],
        host_node_id=OTHER_HOST,
        pool_id=OTHER_POOL,
        gateway_id=OTHER_GATEWAY,
    )
    store = _store(admission_database)
    report = store.admission_readiness(WORKSPACE)
    created = store.create(_job(model=OTHER_BINDING))
    print(
        f"ROW {'two-bindings-healthy':<20} create={created.error_code or created.outcome:<26} "
        f"status={report.status.value:<9} cause={report.cause.value if report.cause else None}"
    )
    assert [row.instance_id for row in report.instances] == ["instance-z", "instance-new"]
    assert report.status == "admitting" and report.cause is None
    assert report.detail is None
    assert created.outcome == "created", created.error_code


def test_a_report_never_reserves(admission_database: AdmissionDatabase) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    store = _store(admission_database)
    before = db.query(
        "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id = %s",
        (WORKSPACE,),
    )
    store.admission_readiness(WORKSPACE)
    store.admission_readiness(WORKSPACE)
    assert (
        db.query(
            "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id = %s",
            (WORKSPACE,),
        )
        == before
    )


def test_the_report_lists_the_ready_instances_with_their_receipt_window(
    admission_database: AdmissionDatabase,
) -> None:
    # ``instances[]`` is the operator's lever: it must name the rows admission reads,
    # with the same receipt vocabulary the console report prints. A report that
    # omitted the expired instance would leave a degraded answer without a cause.
    _seed(admission_database)
    _expire_receipt(admission_database)
    report = _store(admission_database).admission_readiness(WORKSPACE)
    row = next(item for item in report.instances if item.instance_id == INSTANCE[0])
    assert row.instance_generation == INSTANCE[1]
    assert row.readiness_state == "expired"
    assert row.remaining_seconds is not None and row.remaining_seconds < 0
    assert row.max_slots >= row.held_slots + row.quarantined_slots
    assert report.quota is not None and report.quota.max_attempts == 2


WORKSPACE_ROWS = (
    (
        "workspace-disabled",
        "UPDATE speech_workspaces SET workspace_state = 'disabled' WHERE workspace_id = %s",
        (WORKSPACE,),
        WORKSPACE,
        "workspace_disabled",
    ),
    # A workspace row without its quota row is the state a registration has not yet
    # provisioned: admission has nothing to charge, so both surfaces must name
    # `workspace_not_configured` instead of a storage or a workspace-state answer.
    (
        "workspace-not-configured",
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES ('workspace-quota-gap', 'test', 'active', 1)",
        (),
        "workspace-quota-gap",
        "workspace_not_configured",
    ),
)


@pytest.mark.parametrize(
    ("row_id", "statement", "parameters", "workspace_id", "code"),
    WORKSPACE_ROWS,
    ids=[row[0] for row in WORKSPACE_ROWS],
)
@pytest.mark.anyio
async def test_a_workspace_refusal_is_answered_by_both_surfaces_over_http(
    admission_database: AdmissionDatabase,
    row_id: str,
    statement: str,
    parameters: tuple[object, ...],
    workspace_id: str,
    code: str,
) -> None:
    # These two refusals are decided before admission is consulted, so they are
    # pinned on the HTTP surface: the create answers its own 4xx code and the
    # report names the same class. A report that named a different workspace state
    # than the create refused on would be the same defect at a different layer.
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    db.query(statement, parameters)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        created = await client.post(
            PREFIX,
            json=_body(workspace_id=workspace_id),
            headers={"Idempotency-Key": "readiness-" + row_id},
        )
        report_response = await client.get(REPORT_PATH, params={"workspace_id": workspace_id})
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    answered, _, _ = _error(created)
    print(
        f"ROW {row_id:<20} create={answered:<26} "
        f"status={report['status']:<9} cause={report['cause']}"
    )
    assert answered == code, (row_id, answered)
    assert report["status"] == "degraded", (row_id, report)
    assert report["cause"] == code, (row_id, report)


def test_an_unknown_workspace_is_reported_by_its_own_name(
    admission_database: AdmissionDatabase,
) -> None:
    # The create for this workspace answers `workspace_not_found` in the service,
    # before admission is consulted; the report must name the same class instead of
    # silently reporting the default workspace.
    _seed(admission_database)
    report = _store(admission_database).admission_readiness("workspace-absent")
    assert report.status == "degraded"
    assert report.cause is not None and report.cause.value == "workspace_not_found"
    assert report.workspace_id == "workspace-absent"
    assert report.instances == ()
