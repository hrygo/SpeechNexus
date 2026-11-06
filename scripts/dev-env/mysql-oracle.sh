#!/usr/bin/env bash
set -euo pipefail

# Run the InnoDB (mysql) parameters of one integration module against the
# project-managed coordination MySQL.
#
# The shared dual-database fixtures require both SPEECHNEXUS_TEST_DATABASE_URL
# and SPEECHNEXUS_TEST_MYSQL_URL even when only the mysql parameters are
# selected. -k mysql never opens the PostgreSQL connection, so an explicitly
# inert placeholder is used unless the caller exports an isolated PostgreSQL
# test DSN. This gate therefore proves the MySQL/InnoDB half only; the full
# dual-database gate needs both real DSNs and no -k filter.
#
# Usage: mysql-oracle.sh <test path> [extra pytest args]

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
test_path="${1:-}"
shift || true

if [[ -z "$test_path" ]]; then
  echo "usage: $(basename "$0") <test path> [extra pytest args]" >&2
  exit 2
fi
if [[ ! -f "$test_path" ]]; then
  echo "test module not found: $test_path" >&2
  echo "the cancel/abort oracle is added by its own slice; this checkout does not carry it yet" >&2
  exit 2
fi

# Start the pinned local container unless the caller points the DSN elsewhere.
"$repo_root/scripts/dev-env/mysql-service.sh"

if [[ -z "${SPEECHNEXUS_TEST_MYSQL_URL:-}" ]]; then
  env_file="$repo_root/.runtime/config/mysql.env"
  if [[ ! -f "$env_file" ]]; then
    echo "missing $env_file; start the local MySQL or export SPEECHNEXUS_TEST_MYSQL_URL" >&2
    exit 1
  fi
  password="$(sed -n 's/^MYSQL_ROOT_PASSWORD=//p' "$env_file")"
  export SPEECHNEXUS_TEST_MYSQL_URL="mysql://root:${password}@127.0.0.1:55434/"
fi

if [[ -z "${SPEECHNEXUS_TEST_DATABASE_URL:-}" ]]; then
  # Deliberately inert: the selected mysql parameters never open it. It only
  # satisfies the strict dual-DSN fixture; do not read it as PostgreSQL evidence.
  export SPEECHNEXUS_TEST_DATABASE_URL="postgresql://inert:inert@127.0.0.1:1/inert"
  echo "note: PostgreSQL is not exercised; the inert DSN only satisfies the dual-DSN fixture" >&2
fi

uv run pytest "$test_path" -k mysql "$@" -q
