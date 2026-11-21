"""statement manifest:形状校验、摘要只覆盖 canonical statement、variant 不得改参数形状."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from speechnexus_portable_sql import (
    ManifestError,
    digest,
    load_manifest,
    load_profile_registry,
)
from speechnexus_portable_sql.manifest import Manifest, ProfileVariant, Statement

MANIFEST = Path("packages/portable-sql/statements/manifest.json")


def write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def base_statement() -> dict[str, object]:
    return {
        "id": "authority.probe",
        "version": 1,
        "params": [{"name": "meta_id", "kind": "text", "nullable": False}],
        "result": [{"name": "at", "kind": "timestamp", "nullable": False}],
        "transaction": "read_only",
        "lock_order": [],
        "idempotency": "read_only_replay",
        "error_semantics": "validate_or_rollback",
        "sql": "SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id = ?",
    }


def base_payload() -> dict[str, object]:
    return {"revision": "0002", "statements": [base_statement()], "profiles": []}


def test_the_committed_manifest_loads() -> None:
    manifest = load_manifest(MANIFEST)
    statement = manifest.statement("authority.probe")
    assert statement is not None
    assert statement.transaction == "read_only"
    assert statement.params == (("meta_id", "text", False),)
    assert len(statement.result) == 4
    assert len(digest(manifest)) == 64


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"kind": "blob"}, "unknown kind"),
        ({"nullable": "no"}, "explicit nullable"),
    ],
)
def test_bad_column_shapes_are_rejected(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    payload = base_payload()
    statement = base_statement()
    statement["params"] = [{"name": "meta_id", **{"kind": "text", "nullable": False}, **change}]
    payload["statements"] = [statement]
    with pytest.raises(ManifestError, match=message):
        load_manifest(write(tmp_path, payload))


def test_duplicate_statement_ids_are_rejected(tmp_path: Path) -> None:
    payload = base_payload()
    payload["statements"] = [base_statement(), base_statement()]
    with pytest.raises(ManifestError, match="unique"):
        load_manifest(write(tmp_path, payload))


def test_unknown_transaction_boundary_is_rejected(tmp_path: Path) -> None:
    payload = base_payload()
    payload["statements"] = [{**base_statement(), "transaction": "autocommit"}]
    with pytest.raises(ManifestError, match="transaction boundary"):
        load_manifest(write(tmp_path, payload))


def test_missing_result_null_shape_is_rejected(tmp_path: Path) -> None:
    payload = base_payload()
    payload["statements"] = [{**base_statement(), "result": [{"name": "at", "kind": "timestamp"}]}]
    with pytest.raises(ManifestError, match="nullable"):
        load_manifest(write(tmp_path, payload))


def test_digest_covers_shape_and_lock_order() -> None:
    manifest = load_manifest(MANIFEST)
    statement = manifest.statements[0]
    reshaped = Manifest(
        manifest.revision,
        (
            Statement(
                statement.statement_id,
                statement.version,
                statement.params,
                statement.result[:1],
                statement.transaction,
                statement.lock_order,
                statement.idempotency,
                statement.error_semantics,
                statement.sql,
            ),
        ),
        (),
    )
    relocked = Manifest(
        manifest.revision,
        (
            Statement(
                statement.statement_id,
                statement.version,
                statement.params,
                statement.result,
                statement.transaction,
                ("schema_meta",),
                statement.idempotency,
                statement.error_semantics,
                statement.sql,
            ),
        ),
        (),
    )
    assert digest(reshaped) != digest(manifest)
    assert digest(relocked) != digest(manifest)


def test_profile_variants_do_not_change_the_canonical_digest() -> None:
    manifest = load_manifest(MANIFEST)
    variant = ProfileVariant(
        profile_id="postgresql-16",
        statement_id="authority.probe",
        sql="SELECT CURRENT_TIMESTAMP FROM schema_meta WHERE meta_id = ?",
        rationale="equivalent spelling",
        evidence="docs/architecture/database-portability.md",
    )
    with_variant = Manifest(manifest.revision, manifest.statements, (variant,))
    assert digest(with_variant) == digest(manifest)


def test_profile_registry_lists_only_declared_profiles() -> None:
    registry = load_profile_registry(Path("packages/portable-sql/statements/profiles.json"))
    assert registry["postgresql-16"]["family"] == "postgresql"
    assert registry["postgresql-16"]["first_release"] is True
