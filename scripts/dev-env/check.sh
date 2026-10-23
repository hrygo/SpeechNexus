#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime="$repo_root/.runtime"
postgres_root="$runtime/postgres"
postgres_bin="$postgres_root/usr/lib/postgresql/16/bin"
postgres_lib="$postgres_root/usr/lib/aarch64-linux-gnu:$postgres_root/usr/lib/postgresql/16/lib"
failures=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'ok   %s\n' "$label"
  else
    printf 'FAIL %s\n' "$label"
    failures=$((failures + 1))
  fi
}

check "uv" uv --version
check "Node 26.12.1" bash -c '[[ "$(node --version)" == "v26.12.1" ]]'
check "pnpm 12.14.0" bash -c '[[ "$(pnpm --version)" == "12.14.0" ]]'
check "user session persistence" bash -c \
  '[[ "$(loginctl show-user "$USER" -p Linger --value)" == "yes" ]]'
check "credential file permissions" bash -c \
  '[[ "$(stat -c %a .runtime/config/minio.env)" == "600" ]]'
check "coordination MySQL credential permissions" bash -c \
  '[[ "$(stat -c %a .runtime/config/mysql.env)" == "600" ]]'
check "runtime configuration ignored" git -C "$repo_root" check-ignore -q \
  .runtime/config/minio.env
check "development listeners loopback only" bash -c \
  '! ss -ltnH | awk "{print \$4}" | grep -Eq "(0\.0\.0\.0|\[::\]):(55432|55434|59000)$"'
check "PostgreSQL 16.15" env LD_LIBRARY_PATH="$postgres_lib" \
  "$postgres_bin/postgres" --version
check "PostgreSQL protocol" env LD_LIBRARY_PATH="$postgres_lib" \
  "$postgres_bin/pg_isready" -h 127.0.0.1 -p 55432 -U speechnexus -d postgres
check "development schema access" env LD_LIBRARY_PATH="$postgres_lib" \
  "$postgres_bin/psql" -h 127.0.0.1 -p 55432 -U speechnexus -d speechnexus_dev -Atqc \
  'select 1'
check "test schema access" env LD_LIBRARY_PATH="$postgres_lib" \
  "$postgres_bin/psql" -h 127.0.0.1 -p 55432 -U speechnexus -d speechnexus_test -Atqc \
  'select 1'
check "MinIO health" curl -fsS --max-time 2 http://127.0.0.1:59000/minio/health/live
check "coordination MySQL" "$repo_root/scripts/dev-env/mysql-service.sh" status
check "Python 3.14.9" bash -c \
  '[[ "$(uv run --directory "$1" python --version)" == "Python 3.14.9" ]]' _ "$repo_root"
check "contract toolchain" uv run --directory "$repo_root" python -c \
  'import jsonschema, openapi_spec_validator, psycopg; print("ready")'
check "media libraries" uv run --directory "$repo_root" python -c \
  'import av, imageio_ffmpeg, numpy, soundfile; print("ready")'

if uv run --directory "$repo_root" python - <<'PY' >/dev/null 2>&1
from pathlib import Path

from minio import Minio

values = {}
for name in ("minio.env", "dev.env"):
    for line in (Path(".runtime/config") / name).read_text().splitlines():
        key, value = line.split("=", 1)
        values[key] = value
client = Minio(
    "127.0.0.1:59000",
    access_key=values["MINIO_ROOT_USER"],
    secret_key=values["MINIO_ROOT_PASSWORD"],
    secure=False,
)
buckets = {bucket.name for bucket in client.list_buckets()}
assert {values["S3_DEV_BUCKET"], values["S3_TEST_BUCKET"]} <= buckets
PY
then
  printf 'ok   object storage buckets\n'
else
  printf 'FAIL object storage buckets\n'
  failures=$((failures + 1))
fi

read -r available_kb < <(awk '/MemAvailable/ {print $2}' /proc/meminfo)
read -r free_kb < <(df -Pk "$repo_root" | awk 'NR==2 {print $4}')
printf 'info available_memory_gib=%s free_disk_gib=%s\n' \
  "$((available_kb / 1024 / 1024))" "$((free_kb / 1024 / 1024))"
printf 'info database_dev=%s database_test=%s coordination_mysql=%s object_store=%s\n' \
  'postgresql://speechnexus@127.0.0.1:55432/speechnexus_dev' \
  'postgresql://speechnexus@127.0.0.1:55432/speechnexus_test' \
  '127.0.0.1:55434' \
  'http://127.0.0.1:59000'

if ((failures > 0)); then
  printf '%d development environment check(s) failed.\n' "$failures" >&2
  exit 1
fi
printf 'SpeechNexus development environment is ready.\n'
