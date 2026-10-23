#!/usr/bin/env bash
set -euo pipefail

# Ensure the isolated coordination MySQL used by the dual-database tests.
#
# The pinned MySQL 8.4.8 container joins PostgreSQL and MinIO as a project
# development service, with two host-specific differences: it is loopback-only
# on 127.0.0.1:55434, and docker owns its lifecycle instead of a user systemd
# unit (the user manager has no docker socket access on this host). The
# `unless-stopped` restart policy brings it back with the docker daemon, state
# stays under .runtime/, and running this script twice is safe.
#
# Usage: mysql-service.sh [status|stop|--local]
#   (default)  ensure the local container unless SPEECHNEXUS_TEST_MYSQL_URL
#              points at a different isolated server
#   --local    ensure the local container regardless of the configured DSN,
#              for the host-wide environment gate

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime="$repo_root/.runtime"
container="speechnexus-coordination-mysql"
port=55434
image="mysql@sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf"
env_file="$runtime/config/mysql.env"

password() {
  sed -n 's/^MYSQL_ROOT_PASSWORD=//p' "$env_file"
}

ready() {
  docker exec "$container" mysqladmin ping --host=127.0.0.1 --user=root \
    --password="$(password)" --silent >/dev/null 2>&1
}

case "${1:-ensure}" in
  status)
    [[ -f "$env_file" ]] && ready
    exit
    ;;
  stop)
    command -v docker >/dev/null || exit 0
    if docker container inspect "$container" >/dev/null 2>&1; then
      docker stop "$container" >/dev/null
      echo "stopped $container"
    fi
    exit 0
    ;;
  ensure) ;;
  --local)
    local_only=1
    ;;
  *)
    echo "usage: $(basename "$0") [status|stop|--local]" >&2
    exit 2
    ;;
esac

# The caller may point the gate at a different isolated server (CI, another
# host); only the project's own endpoint is started here.
if [[ "${local_only:-0}" != 1 && -n "${SPEECHNEXUS_TEST_MYSQL_URL:-}" &&
  "$SPEECHNEXUS_TEST_MYSQL_URL" != *"127.0.0.1:$port"* ]]; then
  echo "coordination MySQL is configured elsewhere; not starting the local container"
  exit 0
fi

command -v docker >/dev/null || {
  echo "docker is required to run the isolated coordination MySQL." >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "The docker daemon is not reachable for this user." >&2
  exit 1
}

mkdir -p "$runtime/config"
# Initialise the data directory as this user; the image then takes ownership and
# runs mysqld as its own unprivileged account.
if [[ ! -d "$runtime/data/mysql" ]]; then
  mkdir -p "$runtime/data/mysql"
  chmod 0700 "$runtime/data/mysql"
fi
if [[ ! -f "$env_file" ]]; then
  umask 077
  printf 'MYSQL_ROOT_PASSWORD=%s\n' \
    "${SPEECHNEXUS_MYSQL_ROOT_PASSWORD:-speechnexus_test}" > "$env_file"
fi
chmod 0600 "$env_file"

if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker pull "$image"
fi

# Adopt only a container this project created; anything else on that name is
# replaced so the tests never run against an unmanaged or unversioned server.
if docker container inspect "$container" >/dev/null 2>&1; then
  label="$(docker inspect --format '{{index .Config.Labels "speechnexus.role"}}' "$container")"
  current="$(docker inspect --format '{{.Config.Image}}' "$container")"
  if [[ "$label" != "coordination-mysql" || "$current" != "$image" ]]; then
    docker rm --force "$container" >/dev/null
  fi
fi

if ! docker container inspect "$container" >/dev/null 2>&1; then
  docker run --detach --name "$container" \
    --label speechnexus.role=coordination-mysql \
    --restart unless-stopped \
    --publish "127.0.0.1:$port:3306" \
    --env "MYSQL_ROOT_PASSWORD=$(password)" \
    --env MYSQL_ROOT_HOST=% \
    --volume "$runtime/data/mysql:/var/lib/mysql" \
    --memory 2g --cpus 2 \
    "$image" --default-storage-engine=InnoDB \
    --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_bin >/dev/null
elif [[ "$(docker inspect --format '{{.State.Running}}' "$container")" != "true" ]]; then
  docker start "$container" >/dev/null
fi

for _ in $(seq 1 120); do
  if ready; then
    printf 'ok   coordination MySQL ready on 127.0.0.1:%s\n' "$port"
    exit 0
  fi
  sleep 1
done

echo "coordination MySQL did not become ready; inspect docker logs $container" >&2
exit 1
