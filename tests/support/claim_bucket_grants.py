"""The bucket-table ACL a dispatch-capable test role needs.

``coordination_runtime_grants`` grants these in production (the bucket set is seeded
coordination state, so its role gets ``SELECT`` plus the three ``UPDATE`` columns a
lease takes and returns). Fixtures that mint a role by hand and then drive
``claim_due`` would otherwise fail with ``42501`` on the first ``bucket.fence`` read.

Derived from ``claim_bucket_schema`` rather than retyped: the table name and the
column list have one definition point, and a new bucket column must not need a
second edit here.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.claim_bucket_schema import (
    CLAIM_BUCKET_TABLE,
    CLAIM_BUCKET_UPDATE_COLUMNS,
)


def claim_bucket_grants(schema: str, role: str) -> tuple[str, ...]:
    """Render the two statements that let ``role`` take and return a bucket lease."""

    return (
        f'GRANT SELECT ON "{schema}".{CLAIM_BUCKET_TABLE} TO "{role}"',
        f"GRANT UPDATE ({', '.join(CLAIM_BUCKET_UPDATE_COLUMNS)}) "
        f'ON "{schema}".{CLAIM_BUCKET_TABLE} TO "{role}"',
    )
