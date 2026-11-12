"""Domain decisions: digests, replay table, release preconditions, time bounds."""

from __future__ import annotations

import hashlib

import pytest
import rfc8785

from speechnexus_backend.speech.domain.quarantine_release import (
    EVIDENCE_VERSION,
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseIntent,
    ReleaseLeaseFacts,
    ReleaseLedger,
    decide_replay,
    decode_released_intent_ids,
    evidence_matches_stored,
    idempotency_key_digest,
    prepare_evidence,
    require_observed_at,
    require_releasable,
)

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
LEASE = "lease-stage-main"
OBSERVED = "2026-11-13T00:00:00.123456Z"
OBSERVED_US = 1_794_528_000_123_456
REASON = "container record verified by the on-call operator"


def evidence(**overrides: object) -> ReleaseEvidence:
    values: dict[str, object] = {
        "lease_id": LEASE,
        "evidence_kind": "operator_verified_instance_terminated",
        "evidence_digest": DIGEST,
        "observed_at": OBSERVED,
        "reason": REASON,
        "idempotency_key": "operator-poll-0001",
    }
    values.update(overrides)
    return prepare_evidence(**values)  # type: ignore[arg-type]


def ledger(*, key: str | None = "operator-poll-0001", digest: str | None = None) -> ReleaseLedger:
    stored = evidence()
    return ReleaseLedger(
        LEASE,
        "operator_verified_instance_terminated",
        DIGEST,
        OBSERVED_US,
        REASON,
        OBSERVED_US + 1,
        ("submit-stage-main",),
        idempotency_key_digest(key),
        digest or stored.request_digest,
    )


def intent(**overrides: object) -> ReleaseIntent:
    values: dict[str, object] = {
        "submit_intent_id": "submit-stage-main",
        "execution_state": "unknown_quarantined",
        "cleanup_state": "quarantined",
        "recorded_at_us": OBSERVED_US - 10,
        "dispatched_at_us": OBSERVED_US - 5,
        "version": 1,
    }
    values.update(overrides)
    return ReleaseIntent(**values)  # type: ignore[arg-type]


def facts(**overrides: object) -> ReleaseLeaseFacts:
    values: dict[str, object] = {
        "lease_state": "quarantined",
        "cleanup_state": "quarantined",
        "quarantine_observed_at_us": OBSERVED_US - 20,
        "intents": (intent(),),
    }
    values.update(overrides)
    return ReleaseLeaseFacts(**values)  # type: ignore[arg-type]


def test_request_digest_is_the_declared_canonical_value() -> None:
    expected = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps(
                [
                    EVIDENCE_VERSION,
                    {
                        "lease_id": LEASE,
                        "evidence_kind": "operator_verified_instance_terminated",
                        "evidence_digest": DIGEST,
                        "observed_at_us": OBSERVED_US,
                        "reason": REASON,
                    },
                ]
            )
        ).hexdigest()
    )

    assert evidence().request_digest == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_digest", OTHER),
        ("observed_at", "2026-11-13T00:00:00.123457Z"),
        ("reason", REASON + "."),
        ("lease_id", "lease-stage-other"),
    ],
)
def test_request_digest_covers_every_field(field: str, value: object) -> None:
    assert evidence(**{field: value}).request_digest != evidence().request_digest


def test_reason_is_compared_without_trimming() -> None:
    assert evidence(reason=REASON + " ").request_digest != evidence().request_digest


def test_absent_key_stays_absent() -> None:
    assert idempotency_key_digest(None) is None
    assert evidence(idempotency_key=None).idempotency_key_digest is None
    assert evidence().idempotency_key_digest == (
        "sha256:" + hashlib.sha256(b"operator-poll-0001").hexdigest()
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_kind": "instance_terminated_by_controller"},
        {"evidence_digest": "sha256:short"},
        {"evidence_digest": "md5:" + "a" * 32},
        {"observed_at": "not-a-time"},
        {"reason": ""},
        {"reason": "  "},
        {"reason": "x" * 2001},
        {"idempotency_key": "short"},
        {"idempotency_key": "x" * 129},
    ],
)
def test_invalid_evidence_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        evidence(**overrides)


def test_replay_table() -> None:
    stored = evidence()
    same_key_same_evidence = evidence()
    assert decide_replay(ledger(), same_key_same_evidence) == "replay"
    assert decide_replay(ledger(), evidence(reason=REASON + ".")) == "idempotency_conflict"
    assert decide_replay(ledger(key="operator-poll-0002"), stored) == "replay", (
        "a different key with the same evidence is a read-only replay"
    )
    assert decide_replay(ledger(key=None), stored) == "replay"
    assert decide_replay(ledger(key="operator-poll-0002"), evidence(reason="other")) == (
        "quarantine_release_conflict"
    )


def test_no_key_request_replays_a_keyed_ledger_row() -> None:
    assert (
        decide_replay(ledger(key="operator-poll-0001"), evidence(idempotency_key=None)) == "replay"
    )


def test_ledger_rejects_more_than_one_intent_and_unsorted_ids() -> None:
    with pytest.raises(ValueError):
        ReleaseLedger(
            LEASE,
            "operator_verified_instance_terminated",
            DIGEST,
            OBSERVED_US,
            REASON,
            OBSERVED_US + 1,
            ("submit-b", "submit-a"),
            None,
            evidence().request_digest,
        )


def test_evidence_cannot_be_observed_after_budget_release() -> None:
    with pytest.raises(ValueError):
        ReleaseLedger(
            LEASE,
            "operator_verified_instance_terminated",
            DIGEST,
            OBSERVED_US,
            REASON,
            OBSERVED_US - 1,
            (),
            None,
            evidence().request_digest,
        )


def test_require_releasable_accepts_one_unknown_quarantined_intent() -> None:
    assert require_releasable(facts()).submit_intent_id == "submit-stage-main"


@pytest.mark.parametrize(
    "overrides",
    [
        {"lease_state": "reserved"},
        {"lease_state": "executing"},
        {"lease_state": "released"},
        {"cleanup_state": "pending"},
        {"quarantine_observed_at_us": None},
        {"intents": ()},
        {"intents": (intent(execution_state="aborted"),)},
        {"intents": (intent(cleanup_state="complete"),)},
    ],
)
def test_require_releasable_refuses_every_other_shape(overrides: dict[str, object]) -> None:
    with pytest.raises(QuarantineReleaseRejectedError) as raised:
        require_releasable(facts(**overrides))

    assert raised.value.code == "quarantine_release_conflict"


def test_observed_at_bounds_use_recording_dispatch_and_server_time() -> None:
    require_observed_at(evidence(observed_at=_stamp(OBSERVED_US - 5)), intent(), OBSERVED_US)
    require_observed_at(evidence(), intent(), OBSERVED_US)
    with pytest.raises(QuarantineReleaseRejectedError) as early:
        require_observed_at(evidence(observed_at=_stamp(OBSERVED_US - 11)), intent(), OBSERVED_US)
    assert early.value.code == "invalid"
    with pytest.raises(QuarantineReleaseRejectedError) as late:
        require_observed_at(evidence(), intent(), OBSERVED_US - 1)
    assert late.value.code == "invalid"
    with pytest.raises(QuarantineReleaseRejectedError):
        require_observed_at(evidence(), intent(dispatched_at_us=OBSERVED_US + 1), OBSERVED_US)


def _stamp(value_us: int) -> str:
    from datetime import UTC, datetime, timedelta

    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_decode_released_intent_ids_refuses_malformed_history() -> None:
    assert decode_released_intent_ids('["submit-stage-main"]') == ("submit-stage-main",)
    for broken in ("{}", '"submit"', "[1]", "not json"):
        with pytest.raises(ValueError):
            decode_released_intent_ids(broken)


def test_evidence_matches_stored_compares_all_four_fields() -> None:
    stored = evidence()
    assert evidence_matches_stored(
        stored,
        evidence_kind="operator_verified_instance_terminated",
        evidence_digest=DIGEST,
        observed_at_us=OBSERVED_US,
        reason=REASON,
    )
    assert not evidence_matches_stored(
        stored,
        evidence_kind="instance_terminated_by_controller",
        evidence_digest=DIGEST,
        observed_at_us=OBSERVED_US,
        reason=None,
    )
    assert not evidence_matches_stored(
        stored, evidence_kind=None, evidence_digest=None, observed_at_us=None, reason=None
    )
