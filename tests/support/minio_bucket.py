"""Create an isolated test bucket without a check-then-act race.

Module fixtures that need a fixed bucket name used to call `bucket_exists` and then
`make_bucket`. A single pytest process makes that safe. Two workers inside one
integration partition do not: both can observe the bucket missing, and the loser
then fails with `BucketAlreadyOwnedByYou` even though the bucket is exactly the
one it needs. Accepting that answer keeps the "bucket exists" outcome and still
propagates every other S3 error.
"""

from __future__ import annotations

from minio import Minio
from minio.error import S3Error

_ALREADY_EXISTS = frozenset({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"})


def ensure_bucket(client: Minio, name: str) -> None:
    """Create `name` unless it exists; another worker winning the race is success."""
    try:
        client.make_bucket(name)
    except S3Error as error:
        if error.code not in _ALREADY_EXISTS:
            raise
