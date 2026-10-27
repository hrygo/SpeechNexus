"""Operator command that records missing publication evidence for stored bindings.

There is no default evidence: every mapping row must name both identities, and
only publications that already exist are accepted. Dry-run is read-only; apply
is one transaction that rolls back on any conflict or unresolved remainder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from speechnexus_backend.publish_binding import BindingPublishSettings
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingEvidenceConflictError,
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_postgres import (
    BackfillIncompleteError,
    PostgresBindingEvidenceBackfill,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

EXIT_PREFLIGHT_BLOCKED = 10
EXIT_MAPPING_REJECTED = 11
EXIT_UNAVAILABLE = 12
_FIELDS = frozenset(
    {
        "kind",
        "object_id",
        "revision",
        "revision_id",
        "capacity_manifest_id",
        "qualification_report_id",
        "capacity_manifest_digest",
        "qualification_report_digest",
    }
)


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _mapping(path: Path) -> list[BindingPublicationEvidence]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise ValueError("mapping is not readable JSON") from None
    if not isinstance(payload, list) or not payload:
        raise ValueError("mapping must be a non-empty JSON array")
    recorded_at_us = time.time_ns() // 1_000
    records: dict[tuple[str, int], BindingPublicationEvidence] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"mapping entry {index} is not an object")
        if set(item) - _FIELDS:
            raise ValueError(f"mapping entry {index} has unknown fields")
        if item.get("kind", "binding") != "binding":
            raise ValueError(f"mapping entry {index} does not name a binding")
        raw_revision = item.get("revision")
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, str | int):
            raise ValueError(f"mapping entry {index} revision must be decimal text or integer")
        try:
            record = BindingPublicationEvidence(
                object_id=_text(item, "object_id"),
                revision=revision_number(str(raw_revision)),
                revision_id=_text(item, "revision_id"),
                capacity_manifest_id=_text(item, "capacity_manifest_id"),
                qualification_report_id=_text(item, "qualification_report_id"),
                evidence_source="backfill",
                recorded_at_us=recorded_at_us,
                capacity_manifest_digest=_optional_text(item, "capacity_manifest_digest"),
                qualification_report_digest=_optional_text(item, "qualification_report_digest"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"mapping entry {index} is invalid: {error}") from None
        key = (record.object_id, record.revision)
        previous = records.get(key)
        if previous is not None and not previous.same_facts(record):
            raise BindingEvidenceConflictError(
                f"mapping repeats binding_id={record.object_id} revision={record.revision} "
                "with different evidence"
            )
        records[key] = record
    return list(records.values())


def _export(records: Sequence[BindingPublicationEvidence]) -> str:
    return json.dumps(
        [
            {
                "kind": record.kind,
                "object_id": record.object_id,
                "revision": str(record.revision),
                "revision_id": record.revision_id,
                "capacity_manifest_id": record.capacity_manifest_id,
                "qualification_report_id": record.qualification_report_id,
                "evidence_source": record.evidence_source,
                "recorded_at_us": record.recorded_at_us,
                "capacity_manifest_digest": record.capacity_manifest_digest,
                "qualification_report_digest": record.qualification_report_digest,
            }
            for record in records
        ],
        indent=2,
        sort_keys=True,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Preflight, dry-run or apply binding publication evidence mappings"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="Report counts and unresolved rows")
    mode.add_argument("--dry-run", action="store_true", help="Validate a mapping without writing")
    mode.add_argument("--apply", action="store_true", help="Apply a mapping in one transaction")
    mode.add_argument("--export", action="store_true", help="Print recorded evidence as JSON")
    parser.add_argument("--mapping", type=Path, help="Explicit JSON evidence mapping")
    parser.add_argument("--schema", default=None, help="Override SPEECHNEXUS_DATABASE_SCHEMA")
    parser.add_argument("--timeout-seconds", type=float, default=60)
    options = parser.parse_args(argv)

    if not os.environ.get("SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL", "").strip():
        parser.error(
            "Set SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL to the provisioned "
            "publication writer DSN"
        )
    if options.mapping is None and not (options.preflight or options.export):
        parser.error("--mapping is required for --dry-run and --apply")
    try:
        settings = BindingPublishSettings()
    except ValidationError as error:
        parser.error(f"invalid configuration: {error.error_count()} field(s) failed validation")

    try:
        records = _mapping(options.mapping) if options.mapping is not None else None
    except BindingEvidenceConflictError as error:
        parser.exit(EXIT_MAPPING_REJECTED, f"mapping rejected: {error}\n")
    except ValueError as error:
        parser.exit(EXIT_MAPPING_REJECTED, f"mapping invalid: {error}\n")

    pool = speech_pool(
        settings.publication_writer_database_url.get_secret_value(),
        schema=options.schema or settings.database_schema,
        max_size=1,
        name="backfill-binding-evidence",
    )
    pool.open()
    try:
        backfill = PostgresBindingEvidenceBackfill(pool, timeout_seconds=options.timeout_seconds)
        if options.export:
            print(_export(backfill.export()))
            return
        if options.preflight:
            snapshot = backfill.snapshot()
            print(
                f"preflight publications={snapshot.publications} "
                f"evidence={snapshot.evidence} unresolved={len(snapshot.unresolved)}"
            )
            for item in snapshot.unresolved:
                print(
                    f"unresolved object_id={item.object_id} revision={item.revision} "
                    f"revision_id={item.revision_id}"
                )
            unresolved = len(snapshot.unresolved)
            if records is not None:
                plan = backfill.plan(records)
                print(
                    f"preflight mapping_insert={plan.insert} mapping_skip={plan.skip} "
                    f"unresolved_after={plan.unresolved_after}"
                )
                unresolved = plan.unresolved_after
            if unresolved:
                print(
                    "preflight blocked: publications without evidence require --apply "
                    "before this stage is a production candidate",
                    file=sys.stderr,
                )
                raise SystemExit(EXIT_PREFLIGHT_BLOCKED)
            return
        if records is None:
            parser.error("--mapping is required")
        if options.dry_run:
            plan = backfill.plan(records)
            print(
                f"dry-run insert={plan.insert} skip={plan.skip} "
                f"unresolved_before={plan.unresolved_before} "
                f"unresolved_after={plan.unresolved_after}"
            )
            if plan.unresolved_after:
                print(
                    "dry-run rejected: unresolved publications remain after this mapping",
                    file=sys.stderr,
                )
                raise SystemExit(EXIT_MAPPING_REJECTED)
            return
        result = backfill.apply(records)
        print(
            f"applied inserted={result.inserted} skipped={result.skipped} "
            f"evidence_before={result.evidence_before} evidence_after={result.evidence_after} "
            f"unresolved={result.unresolved}"
        )
    except BindingEvidenceConflictError as error:
        parser.exit(EXIT_MAPPING_REJECTED, f"mapping rejected: {error}\n")
    except BackfillIncompleteError as error:
        parser.exit(EXIT_MAPPING_REJECTED, f"backfill rejected: {error}\n")
    except ValueError as error:
        parser.exit(EXIT_MAPPING_REJECTED, f"mapping rejected: {error}\n")
    except PublicationUnavailableError:
        parser.exit(
            EXIT_UNAVAILABLE,
            "backfill unavailable: the write may or may not have committed; "
            "run --preflight before retrying the same mapping\n",
        )
    finally:
        pool.close()


if __name__ == "__main__":
    main()
