"""The binding readiness projection, with the round trip its one rule requires.

``speech/domain/binding_readiness.py`` decides ``engine_ready``,
``quality_qualified`` and ``admission_state`` once. This test pins the three
semantics that #1295 chose, and it pins them by walking the whole round trip an
operator would: evidence present -> available, evidence broken or removed ->
``unavailable`` and admission blocked, evidence restored -> available again.

Mutations that must turn it red:

- deleting the quality gate for a *failed* run (returning ``available`` whenever
  the engine is ready) -- ``test_a_failed_run_blocks_admission`` fails, and the
  round trip cannot return to unavailable;
- making ``unverified`` block phase 1 (treating "no evidence" as "failed") --
  ``test_absent_evidence_is_flagged_but_does_not_block`` fails, which is the
  distinction #1295 asked to keep explicit;
- re-implementing the void comparison with ``>`` instead of the domain's
  ``void_invalidates_receipt`` -- ``test_a_void_at_the_receipt_instant_removes_readiness``
  fails;
- hard-coding ``admission_state`` (a constant instead of the mapping) -- the
  available/unavailable assertions below disagree with each other and fail.
"""

from __future__ import annotations

import unittest

from speechnexus_backend.speech.domain.binding_readiness import (
    AVAILABLE,
    QUALITY_FAILED,
    QUALITY_QUALIFIED,
    QUALITY_UNVERIFIED,
    UNAVAILABLE,
    EngineFacts,
    QualityEvidence,
    admission_state,
    binding_readiness,
    engine_ready,
    quality_qualified,
)

NOW_US = 1_800_000_000_000_000
EXPIRES_US = NOW_US + 300_000_000


def engine(**overrides: object) -> EngineFacts:
    facts = {
        "prepare_probe_succeeded": True,
        "receipt_expires_at_us": EXPIRES_US,
        "voided_at_us": None,
        "observed_at_us": NOW_US - 1_000_000,
    }
    facts.update(overrides)
    return EngineFacts(**facts)  # type: ignore[arg-type]


def quality(basis: str, **overrides: object) -> QualityEvidence:
    values: dict[str, object] = {"basis": basis}
    if basis != QUALITY_UNVERIFIED:
        values.update({"measured_wer": 0.07, "threshold_wer": 0.10, "observed_at_us": NOW_US})
    values.update(overrides)
    return QualityEvidence(**values)  # type: ignore[arg-type]


class EngineReadinessTests(unittest.TestCase):
    def test_a_prepared_probed_instance_is_ready(self) -> None:
        ready, reason = engine_ready(engine(), now_us=NOW_US)
        self.assertTrue(ready)
        self.assertIsNone(reason)

    def test_without_a_successful_prepare_probe_it_is_not_ready(self) -> None:
        ready, reason = engine_ready(engine(prepare_probe_succeeded=False), now_us=NOW_US)
        self.assertFalse(ready)
        self.assertEqual(reason, "prepare_probe_not_succeeded")

    def test_an_expired_receipt_is_not_ready(self) -> None:
        ready, reason = engine_ready(engine(receipt_expires_at_us=NOW_US), now_us=NOW_US)
        self.assertFalse(ready)
        self.assertEqual(reason, "receipt_expired")

    def test_a_void_at_the_receipt_instant_removes_readiness(self) -> None:
        # Equal instants count as invalidating: the domain rule owns that boundary.
        ready, reason = engine_ready(engine(voided_at_us=NOW_US - 1_000_000), now_us=NOW_US)
        self.assertFalse(ready)
        self.assertEqual(reason, "receipt_voided")

    def test_a_void_older_than_the_receipt_leaves_it_ready(self) -> None:
        ready, _ = engine_ready(engine(voided_at_us=NOW_US - 5_000_000), now_us=NOW_US)
        self.assertTrue(ready)


class QualitySemanticsTests(unittest.TestCase):
    def test_a_recorded_run_within_threshold_qualifies(self) -> None:
        qualified, detail = quality_qualified(quality(QUALITY_QUALIFIED))
        self.assertTrue(qualified)
        self.assertIsNone(detail)

    def test_absent_evidence_is_not_qualified_and_says_so(self) -> None:
        qualified, detail = quality_qualified(quality(QUALITY_UNVERIFIED))
        self.assertFalse(qualified)
        self.assertEqual(detail, "quality_evidence_absent")

    def test_a_failed_run_is_not_qualified_and_blocks(self) -> None:
        qualified, detail = quality_qualified(
            quality(QUALITY_FAILED, measured_wer=0.31, threshold_wer=0.10)
        )
        self.assertFalse(qualified)
        self.assertEqual(detail, "quality_below_threshold")

    def test_unverified_quality_cannot_carry_a_measurement(self) -> None:
        # The three shapes are exclusive; an "unverified" claim with a number in
        # it would let a constant masquerade as evidence.
        with self.assertRaises(ValueError):
            QualityEvidence(basis=QUALITY_UNVERIFIED, measured_wer=0.02)


class AdmissionStateMappingTests(unittest.TestCase):
    def test_ready_engine_and_qualified_quality_is_available(self) -> None:
        self.assertEqual(
            admission_state(engine=True, quality=True, quality_basis=QUALITY_QUALIFIED),
            AVAILABLE,
        )

    def test_ready_engine_with_absent_evidence_stays_available(self) -> None:
        # Phase 1: flagged, not blocking.
        self.assertEqual(
            admission_state(engine=True, quality=False, quality_basis=QUALITY_UNVERIFIED),
            AVAILABLE,
        )

    def test_ready_engine_with_a_failed_run_is_unavailable(self) -> None:
        self.assertEqual(
            admission_state(engine=True, quality=False, quality_basis=QUALITY_FAILED),
            UNAVAILABLE,
        )

    def test_an_unready_engine_is_unavailable_whatever_quality_is(self) -> None:
        for basis in (QUALITY_QUALIFIED, QUALITY_UNVERIFIED, QUALITY_FAILED):
            self.assertEqual(
                admission_state(engine=False, quality=True, quality_basis=basis),
                UNAVAILABLE,
            )


class RoundTripTests(unittest.TestCase):
    def build(self, *, engine_facts: EngineFacts, evidence: QualityEvidence):
        return binding_readiness(engine=engine_facts, quality=evidence, now_us=NOW_US)

    def test_available_then_unavailable_then_available_again(self) -> None:
        # 1. Evidence present: admission stands.
        before = self.build(engine_facts=engine(), evidence=quality(QUALITY_QUALIFIED))
        self.assertEqual(before.admission_state, AVAILABLE)
        self.assertFalse(before.blocks_admission)

        # 2. Threshold broken (the run measured outside it): must refuse.
        broken = self.build(
            engine_facts=engine(),
            evidence=quality(QUALITY_FAILED, measured_wer=0.44, threshold_wer=0.10),
        )
        self.assertEqual(broken.admission_state, UNAVAILABLE)
        self.assertTrue(broken.blocks_admission)
        self.assertEqual(broken.quality_basis, QUALITY_FAILED)

        # 3. Evidence removed entirely: the projection degrades to "unverified",
        #    which is still available but no longer claims qualification.
        removed = self.build(engine_facts=engine(), evidence=quality(QUALITY_UNVERIFIED))
        self.assertEqual(removed.admission_state, AVAILABLE)
        self.assertFalse(removed.quality_qualified)

        # 4. Evidence restored: same answer as step 1.
        restored = self.build(engine_facts=engine(), evidence=quality(QUALITY_QUALIFIED))
        self.assertEqual(restored.admission_state, AVAILABLE)
        self.assertTrue(restored.quality_qualified)

    def test_removing_the_engine_evidence_also_blocks_and_recovers(self) -> None:
        blocked = self.build(
            engine_facts=engine(voided_at_us=NOW_US), evidence=quality(QUALITY_QUALIFIED)
        )
        self.assertEqual(blocked.admission_state, UNAVAILABLE)
        self.assertEqual(blocked.engine_reason, "receipt_voided")

        recovered = self.build(
            engine_facts=engine(voided_at_us=None), evidence=quality(QUALITY_QUALIFIED)
        )
        self.assertEqual(recovered.admission_state, AVAILABLE)

    def test_the_projection_always_carries_the_facts_that_decided_it(self) -> None:
        projection = self.build(
            engine_facts=engine(prepare_probe_succeeded=False),
            evidence=quality(QUALITY_UNVERIFIED),
        )
        self.assertEqual(projection.engine_reason, "prepare_probe_not_succeeded")
        self.assertEqual(projection.quality_detail, "quality_evidence_absent")
        self.assertEqual(projection.observed_at_us, NOW_US)


if __name__ == "__main__":
    unittest.main()
