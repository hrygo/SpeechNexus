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
  ceiling is 4096 code points, so 4097 characters are refused with the generic
  400 ``invalid_job_request`` even though the contract promises
  ``TtsJobInput.text.maxLength: 200000``. The same 400 also answers a body over
  the receive bound and an unparsable document, so those causes are not
  distinguishable by code.

Each case asserts the public status code, the stable error code and the exact row
deltas across the public Job, intent and admission tables. Rows come from the
same production transaction, so a refusal that still reserved capacity or wrote a
Job would fail here. This module pins current behaviour instead of inventing a
new code: #690 owns the text-ceiling contract correction, #702 the missing-quota
503 semantics, and #703 the undeclared receive bound with its 413.

Known boundaries, recorded rather than fixed here:

* the shared ``_counts`` helper covers the ten public and admission tables, but
  not ``workspace_quotas`` or ``speech_workspaces``. A refusal path that only
  committed a quota ``UPDATE`` and no row would therefore go unnoticed; the
  same-key replay case checks ``held_attempts`` directly for that reason. Adding
  those tables widens every delta assertion and belongs to a follow-up.
* case 4 deliberately pins the current deployment default
  ``job_receive_max_bytes = 262144``. Raising that default fails its premise
  assertion with the observed value, which is a scenario change to re-derive,
  not a flake to delete.
* ``PLAN_TEXT_CEILING`` and ``CONTRACT_TEXT_CEILING`` are bound to #690, and case
  4's 400 ``invalid_job_request`` is bound to #703: converge the contract on 4096
  and declare a 413 and both cases must be updated in the same change.
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
CONTRACT_TEXT_CEILING = 200_000
# ``settings.job_receive_max_bytes`` in backend/src/speechnexus_backend/settings.py.
RECEIVE_BOUND = 262_144


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


def _deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {table: after[table] - before[table] for table in before}


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
    _grant_capacity(db)
    body = _body()
    before = _counts(db)
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
        counts = _counts(db)
        assert _deltas(before, counts) == dict.fromkeys(before, 1)
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
        assert _counts(db) == counts


async def test_text_above_the_plan_ceiling_is_refused_without_rows(
    admission_database: AdmissionDatabase,
) -> None:
    """4096 code points are accepted and 4097 are refused by the admission plan.

    The refusal is the generic 400 ``invalid_job_request``: the public surface
    cannot tell the plan ceiling apart from the contract's 200000-character
    promise, the receive bound (#703), or a malformed body. #690 owns converging
    the contract on this ceiling.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _grant_capacity(db)
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _counts(db)
        at_limit = _body(input={"text": "中" * PLAN_TEXT_CEILING})
        accepted = await client.post(
            PREFIX, json=at_limit, headers={"Idempotency-Key": "synthetic-at-plan-ceiling"}
        )
        assert accepted.status_code == 202, accepted.text
        after_limit = _counts(db)
        assert _deltas(before, after_limit) == dict.fromkeys(before, 1)

        over_limit = _body(input={"text": "中" * (PLAN_TEXT_CEILING + 1)})
        # The body stays far below the receive bound, so only the plan's own text
        # ceiling can explain this refusal.
        assert _wire_bytes(over_limit) < _settings().job_receive_max_bytes
        refused = await client.post(
            PREFIX, json=over_limit, headers={"Idempotency-Key": "synthetic-over-plan-ceiling"}
        )
        assert refused.status_code == 400, refused.text
        code, kind, retryable = _error(refused)
        assert (code, kind, retryable) == ("invalid_job_request", "invalid_request_error", False)
        assert _counts(db) == after_limit


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
    unnamed = _body()
    del unnamed["workspace_id"]
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _counts(db)
        refused = await client.post(
            PREFIX, json=unnamed, headers={"Idempotency-Key": "synthetic-unnamed-workspace"}
        )
        assert refused.status_code == 503, refused.text
        assert _error(refused) == ("job_unavailable", "service_unavailable_error", False)
        assert _counts(db) == before

        # A workspace row without its quota row fails identically when named.
        db.query(
            "INSERT INTO speech_workspaces "
            "(workspace_id, display_name, workspace_state, created_at_us) "
            "VALUES ('workspace-quota-gap', 'test', 'active', 1)"
        )
        gap = await client.post(
            PREFIX,
            json=_body(workspace_id="workspace-quota-gap"),
            headers={"Idempotency-Key": "synthetic-quota-gap"},
        )
        assert gap.status_code == 503, gap.text
        assert _error(gap) == ("job_unavailable", "service_unavailable_error", False)
        assert _counts(db) == before

        # Naming a workspace that does not exist at all stays the client error.
        unknown = await client.post(
            PREFIX,
            json=_body(workspace_id="workspace-unknown"),
            headers={"Idempotency-Key": "synthetic-unknown-workspace"},
        )
        assert unknown.status_code == 422, unknown.text
        assert _error(unknown) == ("workspace_not_found", "invalid_request_error", False)
        assert _counts(db) == before

        db.query(
            "INSERT INTO workspace_quotas "
            "(workspace_id, max_attempts, held_attempts, policy_revision) "
            "VALUES ('workspace-default', 8, 0, 'policy-test')"
        )
        provisioned = await client.post(
            PREFIX, json=unnamed, headers={"Idempotency-Key": "synthetic-unnamed-workspace"}
        )
        assert provisioned.status_code == 202, provisioned.text
        assert provisioned.json()["workspace_id"] == "workspace-default"
        after = _counts(db)
        assert _deltas(before, after) == dict.fromkeys(before, 1)


async def test_body_over_the_receive_bound_is_refused_before_any_write(
    admission_database: AdmissionDatabase,
) -> None:
    """A wire body above ``job_receive_max_bytes`` is refused before parse and write.

    The text is exactly at the plan ceiling and inside the contract ceiling, so
    the document itself would be admissible. Only the extra ``padding`` member --
    unknown to the contract, sized past the deployment's receive bound -- pushes
    the serialised body over that bound. That makes the bound falsifiable: with
    the bound in place the receive layer answers 400 ``invalid_job_request``
    before parsing, while an assembly without it lets the same body reach
    contract validation and answers 422 ``unsupported_parameter``. Loosening
    ``max_bytes`` at the one assembly point (the Job router in ``app.py``)
    therefore turns this case red.

    Two deliberate couplings; neither is a flake:

    * ``RECEIVE_BOUND`` mirrors the deployment default. Raising
      ``job_receive_max_bytes`` fails the premise assertion below with the
      observed value: the contract-legal-but-oversized scenario disappears, so
      re-derive the case instead of deleting the guard.
    * ``PLAN_TEXT_CEILING`` / ``CONTRACT_TEXT_CEILING`` move with #690, and the
      400 code asserted here moves with #703's declared bound and 413.

    Refusal is pre-write: no Job, intent, submission, operation, attempt,
    request, lease or outbox row appears.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
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
        before = _counts(db)
        refused = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-oversized-body"}
        )
        assert refused.status_code == 400, refused.text
        code, kind, retryable = _error(refused)
        assert (code, kind, retryable) == ("invalid_job_request", "invalid_request_error", False)
        # A parsed body would be 422 for the unknown member instead of 400, and a
        # written one would move at least one of the ten deltas.
        assert _counts(db) == before


async def test_same_key_replay_returns_the_original_job_without_new_rows(
    admission_database: AdmissionDatabase,
) -> None:
    """One key replays the stored Job; a fresh key is a second admitted Job.

    Replay is decided before any reservation, so it must neither reserve a second
    attempt nor write the public rows again. A different key with the same body is
    a genuinely new Job, which is what keeps the replay assertion meaningful.
    """
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    _grant_capacity(db)
    body = _body()
    headers = {"Idempotency-Key": "synthetic-replay-key"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(admission_database)), base_url="http://test"
    ) as client:
        before = _counts(db)
        created = await client.post(PREFIX, json=body, headers=headers)
        assert created.status_code == 202, created.text
        original = created.json()
        after_create = _counts(db)
        assert _deltas(before, after_create) == dict.fromkeys(before, 1)

        replay = await client.post(PREFIX, json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == original["id"]
        assert replay.headers["Location"] == created.headers["Location"]
        assert _counts(db) == after_create
        assert db.query("SELECT held_attempts FROM workspace_quotas") == [{"held_attempts": 1}]

        fresh = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-replay-key-2"}
        )
        assert fresh.status_code == 202, fresh.text
        assert fresh.json()["id"] != original["id"]
        assert _deltas(after_create, _counts(db)) == dict.fromkeys(before, 1)
