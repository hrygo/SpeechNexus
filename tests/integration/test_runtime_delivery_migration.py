"""Actual seven-to-eight upgrade preserves original rows/ACLs and adopts unknowns."""

from typing import Any, cast

import pytest
from sqlalchemy.exc import ProgrammingError
from tests.integration.test_coordination_foundation import DIGEST, UpgradeSandbox, seed
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


def test_upgrade_failure_is_atomic_then_legacy_runtime_adoption_is_conservative(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0007_admission_commands")

    class SeedDatabase:
        def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
            sandbox.admin.execute(statement, parameters)

    seed(cast(JobDatabase, SeedDatabase()))
    for event, delivered in (("legacy-runtime-pending", None), ("legacy-runtime-ack", 1)):
        sandbox.admin.execute(
            "INSERT INTO coordination_outbox "
            "(event_id,event_digest,event_kind,target_node_id,intent_id,delivered_at_us) "
            "VALUES (%s,%s,'runtime','host-test','runtime-test',%s)",
            (event, DIGEST, delivered),
        )
    before = {
        name: sandbox.admin.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
        for name in (
            "coordination_jobs",
            "coordination_outbox",
            "runtime_instance_intents",
            "host_nodes",
            "engine_instances",
            "runtime_instance_heads",
        )
    }
    role = sandbox.schema + "_old_reader"
    sandbox.admin.execute(f'CREATE ROLE "{role}"')
    try:
        sandbox.admin.execute(
            "GRANT SELECT (request_document),UPDATE (version) "
            f'ON runtime_instance_intents TO "{role}"'
        )
        acl = "SELECT relacl FROM pg_class WHERE oid='runtime_instance_intents'::regclass"
        cols = (
            "SELECT attname,attacl FROM pg_attribute "
            "WHERE attrelid='runtime_instance_intents'::regclass "
            "AND attnum>0 AND NOT attisdropped ORDER BY attname"
        )
        old_acl = sandbox.admin.execute(acl).fetchone()
        old_cols = sandbox.admin.execute(cols).fetchall()
        # Force the second DDL statement to fail, after CREATE TABLE, to prove
        # that the new table and all adoption effects roll back together.
        sandbox.admin.execute("CREATE TABLE idx_runtime_delivery_due (sentinel BIGINT)")
        with pytest.raises(ProgrammingError):
            sandbox.upgrade()
        assert sandbox.version() == "0007_admission_commands"
        assert sandbox.admin.execute(
            "SELECT to_regclass('runtime_intent_deliveries') AS target"
        ).fetchone() == {"target": None}
        assert {
            name: sandbox.admin.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
            for name in before
        } == before
        sandbox.admin.execute("DROP TABLE idx_runtime_delivery_due")
        sandbox.upgrade()
        rows = sandbox.admin.execute("SELECT * FROM runtime_intent_deliveries").fetchall()
        assert len(rows) == 1
        adopted = rows[0]
        assert adopted["event_id"] == "legacy-runtime-pending"
        assert (
            adopted["delivery_phase"] == "unknown"
            and adopted["last_failure_code"] == "legacy_adoption_required"
        )
        assert adopted["submission_started_at_us"] is None and adopted["acknowledged_at_us"] is None
        assert adopted["claim_holder_id"] is None and adopted["claim_expires_at_us"] == 0
        assert adopted["created_at_us"] == adopted["updated_at_us"] > 0
        sandbox.upgrade()
        assert sandbox.version() == "0011_binding_parameter_defaults"
        assert sandbox.admin.execute("SELECT * FROM runtime_intent_deliveries").fetchall() == rows
        assert {
            name: sandbox.admin.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
            for name in before
        } == before
        assert sandbox.admin.execute(acl).fetchone() == old_acl
        assert sandbox.admin.execute(cols).fetchall() == old_cols
        assert sandbox.admin.execute(
            "SELECT has_table_privilege(%s,'runtime_intent_deliveries','SELECT') AS allowed",
            (role,),
        ).fetchone() == {"allowed": False}
    finally:
        sandbox.admin.execute(f'DROP OWNED BY "{role}"')
        sandbox.admin.execute(f'DROP ROLE "{role}"')
