#!/usr/bin/env python3
"""Write the gateway's private endpoint's instance generation from the ledger.

``gateway-file-asr-endpoint.json`` names which ``(instance_id, instance_generation)``
the execution gateway should submit to. The generation is a fact only the
coordination database holds: ``runtime_instance_heads.current_generation`` is the one
definition point, and it advances when the controller prepares a new generation. A
hand-typed number in the endpoint file therefore has two failure modes and no alarm
for either (#1291):

* it lags the ledger, so every submit is refused with ``stale_instance_generation``
  while the read side keeps reporting the instance as valid; or
* it leads the ledger, so the gateway submits to a generation the coordinator has
  not accepted.

Both are silent until the receipt runs out, which is the failure #1253/#1286 already
spent a round on. This script removes the literal from the operator's hands:

    # after a generation change, before the gateway is restarted
    SPEECHNEXUS_DATABASE_URL=... python3 deploy/cluster/render-endpoint.py \
        --file .runtime/cluster/compute/gateway-file-asr-endpoint.json

    # in any check: exit 1 when the file disagrees with the ledger
    ... --file ... --check

``--check`` is the drift detector: it reads the ledger and the file and answers
whether they agree, so a stale file is a failed check rather than a puzzling
``stale_instance_generation`` in the gateway log. It never writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA = "speech"

_GENERATION_QUERY = "SELECT current_generation FROM runtime_instance_heads WHERE instance_id = %s"


class LedgerError(RuntimeError):
    """The ledger cannot answer, so no generation may be written."""


def generation_from_head(head: Mapping[str, Any] | None) -> int:
    """The ledger's generation, or refuse.

    A missing row is not "generation 1": it means the instance has never been
    registered with this database, and writing a number there would point the
    gateway at a generation nobody prepared.
    """
    if head is None:
        raise LedgerError("the ledger has no head row for this instance")
    generation = head.get("current_generation")
    if not isinstance(generation, int) or generation < 1:
        raise LedgerError(f"the ledger's current_generation is not a generation: {generation!r}")
    return generation


def with_generation(document: Mapping[str, Any], generation: int) -> dict[str, Any]:
    """The same endpoint document, targeting ``generation``.

    Only this one field is derived. The address, port and binding revision are
    deployment inputs that the operator owns; they are carried through untouched so
    that a re-render cannot silently re-point the gateway.
    """
    target = document.get("target")
    if not isinstance(target, Mapping):
        raise LedgerError("the endpoint document has no target object")
    rewritten = dict(document)
    rewritten["target"] = {**target, "instance_generation": generation}
    return rewritten


def disagreements(document: Mapping[str, Any], generation: int) -> list[str]:
    """What the file says versus what the ledger says; empty when they agree."""
    target = document.get("target")
    if not isinstance(target, Mapping):
        return ["the endpoint document has no target object"]
    recorded = target.get("instance_generation")
    if recorded == generation:
        return []
    return [
        f"target.instance_generation is {recorded!r}, the ledger's current_generation "
        f"for target.instance_id={target.get('instance_id')!r} is {generation}"
    ]


def read_ledger_generation(*, database_url: str, schema: str, instance_id: str) -> int:
    """Read the head row. Kept thin: the rules live in the pure functions above."""
    import psycopg  # imported here so the pure functions stay importable without a driver

    with psycopg.connect(database_url, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("SELECT set_config('search_path', %s, false)", (f"{schema},pg_catalog",))
        row = conn.execute(_GENERATION_QUERY, (instance_id,)).fetchone()
    return generation_from_head(row)


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive the gateway endpoint's instance generation from the ledger"
    )
    parser.add_argument("--file", required=True, type=Path, help="the endpoint JSON to read")
    parser.add_argument(
        "--check",
        action="store_true",
        help="answer whether the file agrees with the ledger; never write",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="coordination DSN; defaults to SPEECHNEXUS_DATABASE_URL",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help=f"coordination schema; defaults to SPEECHNEXUS_DATABASE_SCHEMA or {DEFAULT_SCHEMA!r}",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    options = _arguments(sys.argv[1:] if argv is None else argv)
    database_url = options.database_url or os.environ.get("SPEECHNEXUS_DATABASE_URL")
    if not database_url:
        print(
            "no coordination DSN: pass --database-url or SPEECHNEXUS_DATABASE_URL",
            file=sys.stderr,
        )
        return 2
    schema = options.schema or os.environ.get("SPEECHNEXUS_DATABASE_SCHEMA") or DEFAULT_SCHEMA
    document = json.loads(options.file.read_text(encoding="utf-8"))
    target = document.get("target")
    if not isinstance(target, Mapping) or not target.get("instance_id"):
        print(f"{options.file}: no target.instance_id to look up", file=sys.stderr)
        return 2
    try:
        generation = read_ledger_generation(
            database_url=database_url,
            schema=schema,
            instance_id=str(target["instance_id"]),
        )
    except LedgerError as error:
        print(f"{options.file}: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # any driver error means "cannot answer"
        print(f"{options.file}: the ledger is unreadable: {error}", file=sys.stderr)
        return 1
    problems = disagreements(document, generation)
    if options.check:
        for problem in problems:
            print(f"{options.file}: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"{options.file}: instance_generation {generation} agrees with the ledger")
        return 0
    rewritten = with_generation(document, generation)
    options.file.write_text(
        json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    previous = target.get("instance_generation")
    print(f"{options.file}: instance_generation {previous!r} -> {generation} from the ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
