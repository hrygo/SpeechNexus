#!/usr/bin/env bash
set -euo pipefail

# Isolated synthetic-only service. It never adopts or removes an unowned
# container, creates production buckets, or installs object-storage policies.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container=speechnexus-test-minio
image=speechnexus-minio:RELEASE.2025-09-07T16-13-09Z
endpoint=http://127.0.0.1:59000
if [[ -n "${SPEECHNEXUS_TEST_MINIO_ENDPOINT:-}" && "$SPEECHNEXUS_TEST_MINIO_ENDPOINT" != "$endpoint" ]]; then
  echo 'isolated MinIO is configured elsewhere; not starting the local container'
  exit 0
fi
if docker container inspect "$container" >/dev/null 2>&1; then
  label="$(docker inspect --format '{{index .Config.Labels "com.speechnexus.test"}}' "$container")"
  if [[ "$label" != asset-minio ]]; then
    echo "refusing to manage unowned container $container" >&2
    exit 1
  fi
fi
case "${1:-ensure}" in
  stop)
    if docker container inspect "$container" >/dev/null 2>&1; then docker stop "$container" >/dev/null; fi
    exit 0
    ;;
  ensure) ;;
  *) echo "usage: $0 [stop]" >&2; exit 2 ;;
esac

build_args=()
if [[ -n "${SPEECHNEXUS_BUILD_CA_FILE:-}" ]]; then
  test -f "$SPEECHNEXUS_BUILD_CA_FILE"
  build_args+=(--secret "id=proxy_ca,src=$SPEECHNEXUS_BUILD_CA_FILE")
fi
# Reuse the reviewed recipe's image. Some Docker/BuildKit exporters assign a
# new image config ID even to cached builds; rebuilding on every ensure would
# make a healthy test container appear stale. Changes require explicit review.
recipe="$(sha256sum "$repo_root/deploy/images/minio.Dockerfile" | cut -d ' ' -f 1)"
cached="$(docker image inspect --format '{{index .Config.Labels "com.speechnexus.minio-recipe"}}' "$image" 2>/dev/null || true)"
if [[ "$cached" != "$recipe" ]]; then
  docker build "${build_args[@]}" --file "$repo_root/deploy/images/minio.Dockerfile" \
    --label "com.speechnexus.minio-recipe=$recipe" --tag "$image" "$repo_root"
fi
if docker container inspect "$container" >/dev/null 2>&1; then
  actual="$(docker inspect --format '{{.Image}}' "$container")"
  expected="$(docker image inspect --format '{{.Id}}' "$image")"
  if [[ "$actual" != "$expected" ]]; then
    echo 'owned MinIO container uses a different build; stop and review it before replacing' >&2
    exit 1
  fi
else
  # Synthetic test credentials only. Real deployments provision dedicated keys.
  # A bounded tmpfs keeps CI/local test content off persistent volumes.
  docker run --detach --name "$container" --label com.speechnexus.test=asset-minio \
    --publish 127.0.0.1:59000:9000 \
    --env MINIO_ROOT_USER=speechnexus_test \
    --env MINIO_ROOT_PASSWORD=synthetic_asset_test_secret \
    --env GOMEMLIMIT=256MiB --env GOMAXPROCS=1 \
    --tmpfs /data:rw,noexec,nosuid,size=256m,uid=10001,gid=10001,mode=0700 \
    --memory 512m --cpus 1 --cap-drop ALL --security-opt no-new-privileges \
    "$image" >/dev/null
fi
if [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]]; then
  docker start "$container" >/dev/null
fi
for _ in $(seq 1 40); do
  if docker exec "$container" python -c \
    'import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:9000/minio/health/live",timeout=1).close()' \
    >/dev/null 2>&1; then
    echo "ok   isolated MinIO ready on $endpoint"
    exit 0
  fi
  sleep 1
done
echo "isolated MinIO did not become ready; inspect $container locally" >&2
exit 1
