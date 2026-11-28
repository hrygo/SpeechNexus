"""The synchronous admission refuses on the binding readiness verdict it reads.

``speech/domain/binding_readiness.py`` owns the decision and
``tests/unit/speech/domain/test_binding_readiness.py`` pins its semantics. This
test pins the *wiring*: the synchronous transcription path actually reads the
prepare/receipt/void facts for the current generation and turns an unavailable
verdict into a refusal that names the object.

It never touches PostgreSQL: a recording pool returns the four rows the snapshot
reads, so the round trip (ready -> voided -> released) runs offline.

Mutations that must turn it red:

- dropping the refusal (returning facts while ``blocks_admission`` is true): the
  voided case would return facts and
  ``test_a_voided_receipt_refuses_with_the_object_named`` fails;
- reading the receipt without the void (treating any stored receipt as ready):
  the same test fails, because the voided generation is admitted;
- refusing unconditionally (ignoring the verdict): the released case fails, so the
  gate cannot be satisfied by refusing everything.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any, cast

from speechnexus_backend.speech.application.transcription import (
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.transcription import TranscriptFormat
from speechnexus_backend.speech.infrastructure.transcription_admission import (
    PostgresTranscriptionAdmissionFacts,
)

INSTANCE = "instance-new"
GENERATION = 11
# The fake admission recipe pins these identities; the live snapshot must agree
# with them or ``read`` refuses for an unrelated reason.
BINDING_REVISION = "binding-test"
MANIFEST = "manifest-test"
POLICY = "policy-test"
SERVICE_CLASS = "realtime"
POOL = "tts-realtime"
TARGET_NODE = "host-test"
GATEWAY = "gateway-test"


def _binding_field(name: str) -> str:
    from tests.fake.admission import APPROVED

    return str(getattr(APPROVED.stages[0].runtime_plan.binding, name))


APPROVED_TEMPLATE = _binding_field("approved_template_id")
APPROVED_CONFIG = _binding_field("config_digest")
NOW_US = 1_800_000_000_000_000
EXPIRES_US = NOW_US + 300_000_000


def _clock_value() -> datetime:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW_US / 1_000_000, tz=UTC)


def receipt_document(*, verified_at_us: int, expires_at_us: int) -> str:
    """A stored receipt in the codec's own shape (no hand-written JSON)."""
    from dataclasses import replace

    from tests.fake.runtime_observations import READINESS

    from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
        readiness_to_document,
    )

    return readiness_to_document(
        replace(
            READINESS,
            instance_id=INSTANCE,
            instance_generation=GENERATION,
            verified_at_us=verified_at_us,
            expires_at_us=expires_at_us,
        )
    )


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Connection:
    def __init__(self, replies: list[list[dict[str, Any]]]) -> None:
        self._replies = list(replies)
        self.queries: list[str] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Cursor:
        self.queries.append(query)
        if not self._replies:
            raise AssertionError("unexpected query")
        return _Cursor(self._replies.pop(0))

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Pool:
    def __init__(self, replies: list[list[dict[str, Any]]]) -> None:
        self._replies = replies
        self.connections: list[_Connection] = []

    def connection(self, timeout: float) -> _Connection:
        # The snapshot opens with a SET TRANSACTION statement; give it a reply slot
        # so the four fact queries line up with their intended rows.
        connection = _Connection([[], *self._replies, [{"now": _clock_value()}]])
        self.connections.append(connection)
        return connection


QUOTA = [{"version": 3, "policy_revision": POLICY}]
POOL_ROW = [
    {"version": 5, "service_class": SERVICE_CLASS, "owner_id": "scheduler-test", "fencing_token": 7}
]


def instance_row() -> list[dict[str, Any]]:
    return [
        {
            "current_generation": GENERATION,
            "instance_generation": GENERATION,
            "version": 7,
            "host_node_id": TARGET_NODE,
            "pool_id": POOL,
            "gateway_id": GATEWAY,
            "binding_revision_id": BINDING_REVISION,
            "manifest_id": MANIFEST,
            "approved_template_id": APPROVED_TEMPLATE,
            "config_digest": APPROVED_CONFIG,
        }
    ]


def recipe() -> Any:
    """The real recipe shape from the fake admission fixtures (no PostgreSQL)."""
    from dataclasses import replace

    from tests.fake.admission import APPROVED

    from speechnexus_backend.speech.infrastructure.transcription_recipe import (
        AsrParameters,
        TranscriptionRecipe,
    )

    return TranscriptionRecipe(
        model_id="model-probe",
        binding_publication_id=BINDING_REVISION,
        stage_id="stage-main",
        instance_id=INSTANCE,
        reserved_slots=1,
        asr=AsrParameters(400, 1_000, 0.0, 5_000, 1_000),
        approved=replace(APPROVED, admission_expires_at_us=NOW_US + 60_000_000),
    )


class BindingReadinessGateTests(unittest.TestCase):
    def read(self, receipt: list[dict[str, Any]]) -> Any:
        from speechnexus_backend.speech.domain.transcription import TranscriptionPlan

        plan = TranscriptionPlan(
            request_id="request-probe-1",
            workspace_id="workspace-test",
            model="model-probe",
            binding_publication_id=BINDING_REVISION,
            audio=b"\x00" * 1024,
            audio_media_type="audio/wav",
            audio_sample_rate_hz=16_000,
            audio_channels=1,
            response_format=TranscriptFormat.JSON,
            language="auto",
            timeout_seconds=30,
        )
        pool = _Pool([QUOTA, POOL_ROW, instance_row(), receipt])
        facts = PostgresTranscriptionAdmissionFacts(cast(Any, pool), timeout_seconds=3)
        return facts.read(plan, recipe())

    def test_a_ready_receipt_admits_and_carries_the_verdict(self) -> None:
        facts = self.read(
            [
                {
                    "readiness_document": receipt_document(
                        verified_at_us=NOW_US - 1_000_000, expires_at_us=EXPIRES_US
                    ),
                    "readiness_voided_at_us": None,
                }
            ]
        )
        self.assertTrue(facts.readiness is not None)
        assert facts.readiness is not None
        self.assertEqual(facts.readiness.admission_state, "available")
        self.assertTrue(facts.readiness.engine_ready)
        self.assertEqual(facts.readiness.quality_basis, "unverified")

    def test_a_voided_receipt_refuses_with_the_object_named(self) -> None:
        with self.assertRaises(TranscriptionUnavailableError) as raised:
            self.read(
                [
                    {
                        "readiness_document": receipt_document(
                            verified_at_us=NOW_US - 1_000_000, expires_at_us=EXPIRES_US
                        ),
                        "readiness_voided_at_us": NOW_US - 500_000,
                    }
                ]
            )
        message = str(raised.exception)
        self.assertIn("unavailable", message)
        self.assertIn(f"instance={INSTANCE}", message)
        self.assertIn(f"generation={GENERATION}", message)
        self.assertIn("receipt_voided", message)

    def test_a_missing_prepare_intent_refuses(self) -> None:
        # The LEFT-free join returns nothing: no successful prepare for this
        # generation, so the binding cannot be ready.
        with self.assertRaises(TranscriptionUnavailableError) as raised:
            self.read([])
        self.assertIn("prepare_probe_not_succeeded", str(raised.exception))

    def test_releasing_the_void_admits_again(self) -> None:
        released = self.read(
            [
                {
                    "readiness_document": receipt_document(
                        verified_at_us=NOW_US + 1_000_000, expires_at_us=EXPIRES_US * 2
                    ),
                    "readiness_voided_at_us": NOW_US - 500_000,
                }
            ]
        )
        assert released.readiness is not None
        # A later receipt (its verified_at_us is past the void) is not invalidated
        # by the older void: the same rule the void module owns decides this.
        self.assertEqual(released.readiness.admission_state, "available")


if __name__ == "__main__":
    unittest.main()
