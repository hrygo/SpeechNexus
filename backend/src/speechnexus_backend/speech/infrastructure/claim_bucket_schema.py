"""Fixed-bucket claim table, produced from the canonical shape.

The table is defined once in ``migrations/0003_portable_claim_buckets.sql`` and this
module is the product-side copy the alembic revision and the test schemas share.
Rows are **seeded**, never inserted by the runtime: the bucket set is fixed
(``speechnexus_portable_sql.claims.BUCKETS``), so a runtime identity only takes and
returns a lease on an existing row. That is why the grant declaration gives the
speech role ``SELECT`` plus ``UPDATE (claim_owner_id, lease_until, version)`` and no
``INSERT``/``DELETE``.

``bucket_no`` and ``version`` are ``DECIMAL(38,0)`` rather than ``BIGINT``: the
canonical logical schema (``packages/portable-sql/schema/logical.json``) uses
``decimal(38,0)`` for every counter, and the claim statements bind ``Decimal`` values
from ``speechnexus_portable_sql.claims``.
"""

from __future__ import annotations

CLAIM_BUCKET_TABLE = "coordination_claim_buckets"

#: The only columns a runtime bucket lease changes; the grant is column-level so a
#: role cannot rewrite ``bucket_no`` (the row's identity) in the same UPDATE.
CLAIM_BUCKET_UPDATE_COLUMNS: tuple[str, ...] = ("claim_owner_id", "lease_until", "version")

#: ``IF NOT EXISTS`` mirrors the rest of this migration tree: several integration
#: tests stamp ``alembic_version`` back and re-enter ``upgrade head`` on a schema
#: that already has the object.
CLAIM_BUCKET_DDL: tuple[str, ...] = (
    f"""CREATE TABLE IF NOT EXISTS {CLAIM_BUCKET_TABLE} (
    bucket_no DECIMAL(38,0) NOT NULL,
    claim_owner_id VARCHAR(128),
    lease_until TIMESTAMP(6),
    version DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (bucket_no)
)""",
)

__all__ = [
    "CLAIM_BUCKET_DDL",
    "CLAIM_BUCKET_TABLE",
    "CLAIM_BUCKET_UPDATE_COLUMNS",
]
