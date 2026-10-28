"""Create and enable versioning for the two private file-speech buckets."""

from __future__ import annotations

import os

from minio import Minio
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig


def main() -> None:
    endpoint = os.environ["MINIO_ENDPOINT"]
    client = Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=endpoint.startswith("https://"),
    )
    buckets = {
        os.environ["SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET"],
        os.environ["SPEECHNEXUS_OBJECT_STORAGE_BUCKET"],
    }
    for bucket in sorted(buckets):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
        status = client.get_bucket_versioning(bucket)
        if status.status != ENABLED:
            raise RuntimeError(f"bucket versioning was not enabled: {bucket}")
        print(f"bucket ready: {bucket} (versioning enabled)")


if __name__ == "__main__":
    main()
