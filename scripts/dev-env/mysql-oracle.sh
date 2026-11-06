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
# Test paths are resolved from the repository root, not the caller's CWD.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

urlencode() {
  local LC_ALL=C
  local input="$1"
  local output=""
  local char hex i j
  for ((i = 0; i < ${#input}; i++)); do
    char="${input:i:1}"
    case "$char" in
      [A-Za-z0-9._~-]) output+="$char" ;;
      *)
        hex="$(printf '%s' "$char" | od -An -tx1 | tr -d ' \n' | tr 'a-f' 'A-F')"
        for ((j = 0; j < ${#hex}; j += 2)); do
          output+="%${hex:j:2}"
        done
        ;;
    esac
  done
  printf '%s' "$output"
}

resolve_test_path() {
  local requested="$1"
  local candidate
  case "$requested" in
    /*) candidate="$requested" ;;
    *) candidate="$repo_root/$requested" ;;
  esac
  case "$candidate" in
    "$repo_root"/*) ;;
    *)
      echo "test path must be inside the repository: $requested" >&2
      return 2
      ;;
  esac
  case "/$candidate/" in
    *"/../"*)
      echo "test path must not traverse outside the repository: $requested" >&2
      return 2
      ;;
  esac
  case "$candidate" in
    *.py) ;;
    *)
      echo "test path must name a .py test module: $requested" >&2
      return 2
      ;;
  esac
  if [[ -d "$candidate" ]]; then
    echo "test path is a directory, not a module: $requested" >&2
    return 2
  fi
  if [[ ! -f "$candidate" ]]; then
    echo "test module not found: $requested" >&2
    echo "resolved from repository root: $candidate" >&2
    echo "the cancel/abort oracle was introduced by PR #435 (already merged); update this checkout or pass an existing test path" >&2
    return 2
  fi
  printf '%s\n' "$candidate"
}

main() {
  local requested="${1:-}"
  local test_path relative_test_path env_file password encoded_password
  if [[ -z "$requested" ]]; then
    echo "usage: $(basename "$0") <test path> [extra pytest args]" >&2
    exit 2
  fi
  shift || true

  if ! test_path="$(resolve_test_path "$requested")"; then
    exit 2
  fi
  relative_test_path="${test_path#"$repo_root"/}"

  # Start the pinned local container unless the caller points the DSN elsewhere.
  "$repo_root/scripts/dev-env/mysql-service.sh"

  if [[ -z "${SPEECHNEXUS_TEST_MYSQL_URL:-}" ]]; then
    env_file="$repo_root/.runtime/config/mysql.env"
    if [[ ! -f "$env_file" ]]; then
      echo "missing $env_file; start the local MySQL or export SPEECHNEXUS_TEST_MYSQL_URL" >&2
      exit 1
    fi
    password="$(sed -n 's/^MYSQL_ROOT_PASSWORD=//p' "$env_file")"
    password="${password%%$'\n'*}"
    password="${password%$'\r'}"
    if [[ -z "$password" ]]; then
      echo "empty MYSQL_ROOT_PASSWORD in $env_file; refusing to build an unauthenticated DSN" >&2
      exit 1
    fi
    encoded_password="$(urlencode "$password")"
    export SPEECHNEXUS_TEST_MYSQL_URL="mysql://root:${encoded_password}@127.0.0.1:55434/"
  fi

  if [[ -z "${SPEECHNEXUS_TEST_DATABASE_URL:-}" ]]; then
    # Deliberately inert: the selected mysql parameters never open it. It only
    # satisfies the strict dual-DSN fixture; do not read it as PostgreSQL evidence.
    export SPEECHNEXUS_TEST_DATABASE_URL="postgresql://inert:inert@127.0.0.1:1/inert"
    echo "scope: mysql only (PostgreSQL is not exercised; the inert DSN is not PostgreSQL evidence)" >&2
  else
    echo "scope: dual database DSNs configured (PostgreSQL + MySQL); this run still selects mysql parameters only" >&2
  fi

  cd "$repo_root"
  uv run pytest "$relative_test_path" -k mysql "$@" -q
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
