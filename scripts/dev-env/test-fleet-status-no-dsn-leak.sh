#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
temp_dir=$(mktemp -d "$script_dir/.fleet-status-test.XXXXXX")
trap 'rm -rf "$temp_dir"' EXIT

cat >"$temp_dir/psql" <<'FAKE_PSQL'
#!/usr/bin/env bash
exit 2
FAKE_PSQL
chmod +x "$temp_dir/psql"

failed=0

run_case() {
  local label="$1" dsn="$2" marker="$3" rc

  if PATH="$temp_dir:$PATH" "$script_dir/fleet-status.sh" --db "$dsn" >"$temp_dir/stdout" 2>"$temp_dir/stderr"; then
    rc=0
  else
    rc=$?
  fi

  if [[ "$rc" -eq 2 ]]; then
    printf 'PASS %s: exit code 2\n' "$label"
  else
    printf 'FAIL %s: exit code was %s, expected 2\n' "$label" "$rc"
    failed=1
  fi

  if grep -Fq -- "$marker" "$temp_dir/stdout" || grep -Fq -- "$marker" "$temp_dir/stderr"; then
    printf 'FAIL %s: synthetic marker leaked to output\n' "$label"
    failed=1
  else
    printf 'PASS %s: synthetic marker absent from stdout/stderr\n' "$label"
  fi

  if grep -Fqx 'fleet-status: cannot query database' "$temp_dir/stderr"; then
    printf 'PASS %s: fixed connection error is present\n' "$label"
  else
    printf 'FAIL %s: fixed connection error is missing\n' "$label"
    failed=1
  fi
}

run_case 'URI userinfo password' \
  'postgresql://synthetic-user:synthetic-userinfo-password-314@127.0.0.1:1/none' \
  'synthetic-userinfo-password-314'
run_case 'URI query password' \
  'postgresql://nobody@127.0.0.1:1/none?password=synthetic-query-password-314' \
  'synthetic-query-password-314'
run_case 'keyword DSN password' \
  'host=127.0.0.1 port=1 dbname=none user=nobody password=synthetic-keyword-password-314' \
  'synthetic-keyword-password-314'

exit "$failed"
