"""Asset policy, publication and failure behavior without services or media models."""

from __future__ import annotations

import io
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from speechnexus_backend.speech.application.assets import (
    AssetMetadata,
    AssetObjects,
    AssetService,
    DeletionTicket,
    TextAssetInspector,
    prepared_body,
)
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetRecord,
    Inspection,
    MediaFacts,
    UploadIntent,
    UploadResult,
    VerifiedObject,
    begin_asset_write,
    byte_range,
    counter,
    digest,
    expiry_blocker,
    identifier,
    publish_asset,
    reserve_asset,
)

SHA = "sha256:" + "a" * 64
INTENT = UploadIntent("workspace-default", "text_input", 4, SHA, SHA)
POLICY = AssetPolicy(60)
INSPECTION = Inspection(facts=MediaFacts("text/plain; charset=utf-8"))
RESERVED = reserve_asset(
    INTENT,
    INSPECTION,
    POLICY,
    asset_id="asset-test",
    object_key="private/assets/asset-test/" + "a" * 32,
    now_us=1,
)
WRITING = begin_asset_write(RESERVED, "writer-test", 2)
PROOF = VerifiedObject(
    WRITING.asset_id,
    WRITING.object_key,
    "storage-version-test",
    WRITING.bytes,
    WRITING.sha256,
    "writer-test",
)
READY = publish_asset(WRITING, PROOF, now_us=3, workspace_active=True)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1, 2**53])
def test_counters_reject_ambiguous_or_unsafe_values(value: Any) -> None:
    with pytest.raises(ValueError):
        counter(value)


@pytest.mark.parametrize("value", ["ab", "Aaa", "a/b", "../a", "a" * 129, 123, "中文"])
def test_identity_is_bounded_ascii_not_a_storage_path(value: Any) -> None:
    with pytest.raises(ValueError):
        identifier(value)


@pytest.mark.parametrize("value", ["a" * 64, "sha256:" + "A" * 64, SHA + "\n", 123])
def test_digest_is_explicit_lowercase_sha256(value: Any) -> None:
    with pytest.raises(ValueError):
        digest(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"default_ttl_seconds": 59},
        {"default_ttl_seconds": 31536001},
        {"max_bytes": 0},
        {"max_bytes": 1024**3 + 1},
        {"max_text_characters": 1000001},
    ],
)
def test_configured_policy_limits(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(POLICY, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "other"},
        {"bytes": True},
        {"requested_ttl_seconds": 59},
        {"expected_sha256": "bad"},
        {"key_digest": "bad"},
        {"workspace_id": "bad/workspace"},
    ],
)
def test_upload_intent_rejects_invalid_facts(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(INTENT, **changes)


def test_original_request_fingerprint_does_not_depend_on_mutable_effective_ttl() -> None:
    assert replace(INTENT, requested_ttl_seconds=60).request_digest != INTENT.request_digest
    assert replace(INTENT, key_digest="sha256:" + "b" * 64).request_digest == INTENT.request_digest
    assert replace(INTENT, expected_sha256=SHA).request_digest != INTENT.request_digest
    assert replace(INTENT, bytes=0).content_rejection() == "asset_empty"
    assert (
        replace(INTENT, expected_sha256="sha256:" + "b" * 64).content_rejection()
        == "asset_digest_mismatch"
    )
    assert INTENT.content_rejection() is None
    explicit = replace(INTENT, requested_ttl_seconds=120)
    new = reserve_asset(
        explicit,
        INSPECTION,
        AssetPolicy(None),
        asset_id=RESERVED.asset_id,
        object_key=RESERVED.object_key,
        now_us=10,
    )
    assert new.expires_at_us == 120000010
    with pytest.raises(AssetError, match="asset_retention_not_configured"):
        reserve_asset(
            INTENT,
            INSPECTION,
            AssetPolicy(None),
            asset_id=RESERVED.asset_id,
            object_key=RESERVED.object_key,
            now_us=10,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at_us": 0},
        {"purpose": "other"},
        {"status": "other"},
        {"phase": "other"},
        {"object_key": "private/other"},
        {"writer_id": "bad/writer"},
        {"phase": "writing"},
        {"status": "ready"},
        {"version": 2**63},
    ],
)
def test_records_cannot_invent_identity_or_ready_publication(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(RESERVED, **changes)


@pytest.mark.parametrize(
    "changes", [{"version_id": "null"}, {"version_id": ""}, {"version_id": 123}, {"bytes": 0}]
)
def test_object_proof_requires_a_real_pinned_version(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(PROOF, **changes)


def test_audio_requires_measured_facts_and_text_must_not_invent_them() -> None:
    audio = MediaFacts("audio/wav", 1000, 16000, 1)
    audio.require_for("asr_input")
    for media, purpose in [
        (MediaFacts("audio/wav"), "asr_input"),
        (audio, "text_input"),
        (MediaFacts(""), "text_input"),
        (replace(audio, channels=9), "voice_reference"),
    ]:
        with pytest.raises(ValueError):
            media.require_for(purpose)  # type: ignore[arg-type]
    for kwargs in [
        {},
        {"facts": INSPECTION.facts, "error_code": "invalid"},
        {"available": False, "facts": INSPECTION.facts},
    ]:
        with pytest.raises(ValueError):
            Inspection(**kwargs)  # type: ignore[arg-type]


def test_publication_obeys_original_writer_expiry_and_workspace_state() -> None:
    assert not RESERVED.content_available(2) and READY.content_available(3)
    assert not READY.content_available(READY.expires_at_us)
    with pytest.raises(AssetError, match="asset_unavailable"):
        begin_asset_write(RESERVED, "writer-test", RESERVED.expires_at_us)
    with pytest.raises(AssetError, match="operation_in_progress"):
        begin_asset_write(WRITING, "writer-other", 3)
    with pytest.raises(ValueError, match="original"):
        publish_asset(
            WRITING, replace(PROOF, writer_id="writer-other"), now_us=3, workspace_active=True
        )
    with pytest.raises(AssetError, match="asset_publication_conflict"):
        publish_asset(replace(WRITING, status="deleted"), PROOF, now_us=3, workspace_active=True)
    assert publish_asset(READY, PROOF, now_us=4, workspace_active=True) == READY
    disabled = publish_asset(WRITING, PROOF, now_us=3, workspace_active=False)
    assert disabled.status == "staging" and disabled.phase == "settled"
    expired = publish_asset(WRITING, PROOF, now_us=WRITING.expires_at_us, workspace_active=True)
    assert expired.status == "expired" and expired.phase == "settled"
    rejected = reserve_asset(
        INTENT,
        Inspection(error_code="invalid_asset_text"),
        POLICY,
        asset_id=RESERVED.asset_id,
        object_key=RESERVED.object_key,
        now_us=1,
    )
    assert rejected.status == "rejected" and rejected.phase == "settled"
    assert expiry_blocker(WRITING, has_references=False) == "asset_upload_unknown"
    assert expiry_blocker(READY, has_references=True) == "asset_referenced"
    assert expiry_blocker(READY, has_references=False) is None
    with pytest.raises(AssetError, match="asset_inspection_unavailable"):
        reserve_asset(
            INTENT,
            Inspection(available=False),
            POLICY,
            asset_id=RESERVED.asset_id,
            object_key=RESERVED.object_key,
            now_us=1,
        )


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, (0, 10, False)),
        ("bytes=2-4", (2, 3, True)),
        ("bytes=2-", (2, 8, True)),
        ("bytes=-3", (7, 3, True)),
        ("bytes=-30", (0, 10, True)),
        ("bytes=0-100", (0, 10, True)),
    ],
)
def test_single_range_bounds(header: str | None, expected: tuple[int, int, bool]) -> None:
    assert byte_range(header, 10) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=",
        "bytes=-",
        "bytes=-0",
        "bytes=10-",
        "bytes=4-2",
        "bytes=1-2,3-4",
        "other=0-1",
        "bytes=" + "9" * 130 + "-",
        "bytes= -1",
    ],
)
def test_unavailable_ranges_are_explicit(header: str) -> None:
    with pytest.raises(AssetError, match="range_not_satisfiable"):
        byte_range(header, 10)


@pytest.mark.parametrize(
    "body,code",
    [
        (b"\xff", "invalid_asset_text"),
        (b"a\0", "invalid_asset_text"),
        (b"abcde", "asset_text_too_long"),
        (b"a\xc2", "invalid_asset_text"),
    ],
)
def test_text_inspection_rejects_content_and_restores_private_stream(
    body: bytes, code: str
) -> None:
    stream = io.BytesIO(body)
    result = TextAssetInspector().inspect(
        "text_input", stream, replace(POLICY, max_text_characters=4)
    )
    assert result.error_code == code and stream.tell() == 0


def test_text_inspector_counts_characters_across_chunk_boundaries() -> None:
    text = "a" * 65535 + "中"
    stream = io.BytesIO(text.encode())
    inspector = TextAssetInspector()
    assert inspector.inspect("text_input", stream, POLICY).facts == INSPECTION.facts
    assert stream.tell() == 0
    assert not inspector.inspect("asr_input", stream, POLICY).available
    with prepared_body(io.BytesIO(b"x" * 1100000), POLICY) as (body, size, sha):
        assert size == 1100000 and sha.startswith("sha256:") and body.read(1) == b"x"
        private = body
    assert private.closed
    with (
        pytest.raises(AssetError, match="asset_too_large"),
        prepared_body(io.BytesIO(b"abc"), replace(POLICY, max_bytes=2)),
    ):
        pytest.fail("bounded stream must not yield")


def setup_service() -> tuple[AssetService, Mock, Mock]:
    metadata, objects = Mock(spec=AssetMetadata), Mock(spec=AssetObjects)
    metadata.reserve.return_value = UploadResult("created", RESERVED)
    metadata.begin_write.return_value = UploadResult("created", WRITING)
    metadata.publish.return_value = UploadResult("created", READY)
    metadata.get.return_value = (READY, 3)
    metadata.object.return_value = PROOF
    objects.put_verified.return_value = PROOF
    return (
        AssetService(
            cast(AssetMetadata, metadata), cast(AssetObjects, objects), TextAssetInspector(), POLICY
        ),
        metadata,
        objects,
    )


def upload(service: AssetService, **kwargs: Any) -> UploadResult:
    return service.upload(
        io.BytesIO(b"text"), workspace_id="workspace-default", purpose="text_input", **kwargs
    )


def test_service_puts_only_after_confirmed_write_start_and_releases_body() -> None:
    service, metadata, objects = setup_service()
    assert upload(service).asset == READY
    body = objects.put_verified.call_args.args[1]
    assert body.closed
    assert metadata.reserve.call_args.kwargs["object_key"].startswith("private/assets/asset-")
    metadata.begin_write.return_value = UploadResult("unknown", WRITING)
    objects.put_verified.reset_mock()
    assert upload(service).outcome == "unknown"
    objects.put_verified.assert_not_called()
    metadata.reserve.return_value = UploadResult("conflict", READY)
    assert upload(service).outcome == "conflict"
    with pytest.raises(AssetError, match="invalid_idempotency_key"):
        upload(service, idempotency_key="short")


def test_service_unknown_put_never_issues_a_second_write() -> None:
    service, metadata, objects = setup_service()
    objects.put_verified.side_effect = AssetError("synthetic_lost_reply")
    assert upload(service).outcome == "unknown"
    metadata.reserve.return_value = UploadResult("replayed", WRITING)
    metadata.get.return_value = (WRITING, 3)
    objects.inspect_original.return_value = None
    assert upload(service).outcome == "unknown"
    assert objects.put_verified.call_count == 1
    objects.inspect_original.return_value = PROOF
    assert upload(service).asset == READY
    assert objects.put_verified.call_count == 1


@pytest.mark.parametrize(
    "record,outcome",
    [
        (READY, "replayed"),
        (RESERVED, "in_progress"),
        (
            replace(RESERVED, status="rejected", phase="settled", error_code="invalid_asset_text"),
            "rejected",
        ),
    ],
)
def test_recovery_uses_only_original_facts(record: AssetRecord, outcome: str) -> None:
    service, metadata, objects = setup_service()
    metadata.get.return_value = (record, 3)
    assert service.reconcile(record.asset_id).outcome == outcome
    objects.put_verified.assert_not_called()


def test_reads_reject_unknown_unpublished_expired_and_missing_bindings() -> None:
    service, metadata, _ = setup_service()
    for record, now, code in [
        (None, 3, "asset_not_found"),
        (RESERVED, 3, "asset_unavailable"),
        (READY, READY.expires_at_us, "asset_unavailable"),
        (replace(READY, status="deleted"), 3, "asset_unavailable"),
    ]:
        metadata.get.return_value = (record, now)
        with pytest.raises(AssetError, match=code):
            service.download("asset-test")
    metadata.get.return_value = (None, 3)
    with pytest.raises(AssetError, match="asset_not_found"):
        service.reconcile("asset-test")
    metadata.get.return_value = (READY, 3)
    metadata.object.return_value = None
    with pytest.raises(AssetError, match="asset_unavailable"):
        service.download("asset-test")


def test_content_is_bounded_and_reader_closes_after_early_consumer_exit() -> None:
    service, _, objects = setup_service()
    reader = io.BytesIO(b"text")
    objects.open.return_value = reader
    iterator = service.content(service.download("asset-test"))
    assert next(iterator) == b"text"
    iterator.close()
    assert reader.closed
    objects.open.return_value = io.BytesIO(b"t")
    with pytest.raises(AssetError, match="asset_content_incomplete"):
        list(service.content(service.download("asset-test")))


def test_maintenance_requires_confirmed_pinned_delete_and_is_bounded() -> None:
    service, metadata, objects = setup_service()
    metadata.pending.return_value = ["asset-test"]
    metadata.get.side_effect = AssetError("asset_storage_unavailable")
    metadata.expired.return_value = ["asset-test"]
    metadata.prepare_expiry.return_value = DeletionTicket(
        "ledger-test", "asset-test", "blocked", PROOF
    )
    service.maintain()
    objects.delete_version.assert_not_called()
    metadata.prepare_expiry.return_value = DeletionTicket(
        "ledger-test", "asset-test", "queued", PROOF
    )
    objects.delete_version.side_effect = AssetError("asset_cleanup_unconfirmed")
    service.maintain()
    metadata.complete_deletion.assert_not_called()
    objects.delete_version.side_effect = None
    service.maintain()
    metadata.complete_deletion.assert_called_once_with("ledger-test")
    metadata.prepare_expiry.return_value = DeletionTicket(
        "ledger-test", "asset-test", "queued", None
    )
    service.maintain()
    with pytest.raises(ValueError, match="bounded"):
        service.maintain(101)
