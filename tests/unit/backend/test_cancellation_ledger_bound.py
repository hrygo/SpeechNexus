"""The ledger's attempt bound must stay one number across the three places that assert it.

``speech_job_cancellation_reconciliations.attempts`` is bounded in three places that
have to agree: the table's ``CHECK``, the claim's clamp
``LEAST(attempts + 1, _MAX_ATTEMPTS)``, and the readiness read that names a saturated
row (``ATTEMPT_CEILING``, added by #1038). The two constants are still separate
literals, so this test is the executable form of "change one, change both". A drift is
a behaviour bug, not a cosmetic one:

* a claim bound **below** the CHECK bound saturates ``attempts`` early, so the read
  face never says ``retrying at attempt ceiling`` for a row that is already spent --
  the silent state #1038 removed;
* a claim bound **above** it makes ``LEAST`` emit 65+, which the ``CHECK`` rejects: the
  claim ``UPDATE`` fails and the row stops being retried at all.

The DDL is checked the same way: it must interpolate the constant rather than repeat
the number, because a second literal there would drift while this test stayed green.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import (
    job_cancellation_fence_schema as fence_schema,
)
from speechnexus_backend.speech.infrastructure import (
    job_cancellation_reconciliation_postgres as ledger_store,
)


def test_the_claim_clamp_and_the_read_share_one_ceiling() -> None:
    """One bound, two readers: the claim clamps at it and the probe names it."""
    assert ledger_store._MAX_ATTEMPTS == fence_schema.ATTEMPT_CEILING, (
        "the claim's _MAX_ATTEMPTS and the readiness ATTEMPT_CEILING must be the same "
        "number: below it the read never names a spent row, above it the claim UPDATE "
        "violates the table CHECK"
    )


def test_the_table_check_interpolates_that_ceiling() -> None:
    """The DDL must not carry a second copy of the number."""
    ddl = " ".join(fence_schema.JOB_CANCELLATION_FENCE_DDL)
    assert f"CHECK (attempts BETWEEN 0 AND {fence_schema.ATTEMPT_CEILING})" in ddl
    if fence_schema.ATTEMPT_CEILING != 64:  # the historical literal must not linger
        assert "CHECK (attempts BETWEEN 0 AND 64)" not in ddl


def test_the_debt_reader_is_granted_the_counter_it_compares() -> None:
    """The scope can only be computed if the reader may select the column."""
    grants = " ".join(
        fence_schema.cancellation_debt_reader_grants("speech", "cancellation_debt_reader")
    )
    assert "settled_at_us, last_outcome, next_check_at_us, attempts" in grants, (
        "the read face compares attempts against the ceiling, so the reader needs it"
    )
