#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

api_pid=""
gateway_pid=""
api_log="$(mktemp)"
gateway_log="$(mktemp)"

cleanup() {
  if [[ -n "$api_pid" ]]; then
    kill "$api_pid" 2>/dev/null || true
    wait "$api_pid" 2>/dev/null || true
  fi
  if [[ -n "$gateway_pid" ]]; then
    kill "$gateway_pid" 2>/dev/null || true
    wait "$gateway_pid" 2>/dev/null || true
  fi
  rm -f "$api_log" "$gateway_log"
}
trap cleanup EXIT

SPEECHNEXUS_PORT=18000 ./scripts/dev-env/run-api.sh >"$api_log" 2>&1 &
api_pid=$!
SPEECHNEXUS_GATEWAY_PORT=18010 ./scripts/dev-env/run-gateway.sh >"$gateway_log" 2>&1 &
gateway_pid=$!

wait_for_url() {
  local label="$1"
  local url="$2"
  local log="$3"
  for _ in $(seq 1 120); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      printf 'ok   %s\n' "$label"
      return 0
    fi
    sleep 0.25
  done
  printf 'FAIL %s\n' "$label" >&2
  tail -40 "$log" >&2
  return 1
}

wait_for_url "API readiness" "http://127.0.0.1:18000/readyz" "$api_log"
wait_for_url "gateway readiness" "http://127.0.0.1:18010/readyz" "$gateway_log"

curl -fsS "http://127.0.0.1:18000/healthz" \
  | uv run python -c 'import json, sys; body=json.load(sys.stdin); assert body["status"] == "ok"'
curl -fsS "http://127.0.0.1:18010/healthz" \
  | uv run python -c 'import json, sys; body=json.load(sys.stdin); assert body["status"] == "ok"'

printf 'service smoke: passed\n'
