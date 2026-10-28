"""A gateway submit and binding disable serialize in the same PostgreSQL schema."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_submit_http import LEASE, LiveGateway, contract_command
from tests.integration.test_gateway_submit_http import gateway as gateway
from tests.integration.test_gateway_submit_ledger import identity_for
from tests.integration.test_production_jobs import database as database
from tests.integration.test_publications_storage import DEFINITION, create_binding

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_binding_barrier_grants,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


def test_gateway_submit_waits_for_disable_then_never_dispatches(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    db = foundation.database
    create_binding(db.admin, DEFINITION)
    db.query("UPDATE speech_binding_revisions SET revision_id='binding-test'")
    db.query(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','tts-quality','1','binding-test',1)"
    )
    for statement in gateway_binding_barrier_grants(db.schema, foundation.gateway_role):
        db.query(statement)
    gateway.ledger._enforce_binding_barrier = True
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.blocker() as disable:
            disable.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:catalog:' || current_schema() || ':binding:' || %s,0))",
                ("tts-quality",),
            )
            disable.execute(
                "INSERT INTO speech_publication_retirements "
                "(kind,object_id,revision,revision_id,retired_at_us,mode,reason) "
                "VALUES ('binding','tts-quality','1','binding-test',2,'disable','synthetic')"
            )
            future = executor.submit(gateway.submit, identity, contract_command())
            end = time.monotonic() + 2
            while time.monotonic() < end:
                rows = db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid)) > 0",
                    (foundation.gateway_role,),
                )
                if rows[0]["n"]:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("gateway submit did not wait for the binding lock")
        response = future.result(timeout=5)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "policy_or_resource_disabled"
    assert gateway.engine.dispatch_count == 0
    assert (
        db.query("SELECT count(*) AS n FROM submit_intents WHERE submit_intent_id='intent-http'")[
            0
        ]["n"]
        == 0
    )


def test_existing_submit_remains_replayable_after_disable(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    db = foundation.database
    create_binding(db.admin, DEFINITION)
    db.query("UPDATE speech_binding_revisions SET revision_id='binding-test'")
    db.query(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','tts-quality','1','binding-test',1)"
    )
    for statement in gateway_binding_barrier_grants(db.schema, foundation.gateway_role):
        db.query(statement)
    gateway.ledger._enforce_binding_barrier = True
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    command = contract_command()
    assert gateway.submit(identity, command).status_code == 200
    db.query(
        "INSERT INTO speech_publication_retirements "
        "(kind,object_id,revision,revision_id,retired_at_us,mode,reason) "
        "VALUES ('binding','tts-quality','1','binding-test',2,'disable','synthetic')"
    )
    replay = gateway.submit(identity, command)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert gateway.engine.dispatch_count == 1
    assert (
        db.query("SELECT count(*) AS n FROM submit_intents WHERE submit_intent_id='intent-http'")[
            0
        ]["n"]
        == 1
    )


@pytest.mark.parametrize("different_request", [False, True])
def test_submit_waiting_for_disable_recovers_concurrent_original_identity(
    foundation: Foundation, gateway: LiveGateway, different_request: bool
) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    from speechnexus_gateway.domain.canonical import command_digest
    from speechnexus_gateway.domain.submission import (
        SubmissionIntent,
        SubmitRequest,
        derived_output_stream_id,
    )
    from speechnexus_gateway.infrastructure.submit_sql import create_submit_in_transaction

    db = foundation.database
    create_binding(db.admin, DEFINITION)
    db.query("UPDATE speech_binding_revisions SET revision_id='binding-test'")
    db.query(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','tts-quality','1','binding-test',1)"
    )
    for statement in gateway_binding_barrier_grants(db.schema, foundation.gateway_role):
        db.query(statement)
    gateway.ledger._enforce_binding_barrier = True
    command = contract_command()
    identity = replace(
        identity_for(LEASE, submit_intent_id="intent-http"), command_digest=command_digest(command)
    )
    request = SubmitRequest(
        identity, command, derived_output_stream_id(identity.operation_id, identity.attempt_id)
    )
    now = datetime.now(UTC)
    intent = SubmissionIntent(
        identity=identity,
        recorded_at=now,
        updated_at=now,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.blocker() as disable:
            disable.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'speechnexus:catalog:' || current_schema() || ':binding:' || %s,0))",
                ("tts-quality",),
            )
            disable.execute(
                "INSERT INTO speech_publication_retirements "
                "(kind,object_id,revision,revision_id,retired_at_us,mode,reason) "
                "VALUES ('binding','tts-quality','1','binding-test',2,'disable','synthetic')"
            )
            with db.blocker() as original:
                with original.cursor() as cursor:
                    create_submit_in_transaction(
                        cursor, intent, request.request_document, clock=lambda: now
                    )
                changes = {"fencing_token": 2} if different_request else {}
                future = executor.submit(gateway.submit, identity, command, **changes)
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    if db.query(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                        "AND cardinality(pg_blocking_pids(pid)) > 0",
                        (foundation.gateway_role,),
                    )[0]["n"]:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("gateway submit did not wait for the binding lock")
            # The original commit is now visible before the disable lock is released.
        response = future.result(timeout=5)
    if different_request:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "submit_intent_conflict"
    else:
        assert response.status_code == 200, response.text
        assert response.json()["idempotent_replay"] is True
    assert gateway.engine.dispatch_count == 0
    assert (
        db.query(
            "SELECT count(*) AS n FROM submit_intents WHERE submit_intent_id=%s", ("intent-http",)
        )[0]["n"]
        == 1
    )
