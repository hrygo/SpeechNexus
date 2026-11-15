#!/usr/bin/env bash
# Install the exact locked environment, retrying only transient index failures.
#
# A single 403/429/5xx from a package index otherwise cancels a whole job after
# a few seconds; on a full CI route that discards roughly nine minutes of
# integration work and leaves the change without a usable verdict. Lock drift,
# resolution errors and any other non-network failure must fail immediately:
# retrying them hides a real defect and wastes more runner time.
set -euo pipefail

attempts="${UV_SYNC_ATTEMPTS:-3}"
delay="${UV_SYNC_RETRY_DELAY:-5}"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

transient='Failed to download|Failed to fetch|HTTP status (client|server) error \((403|429|5[0-9][0-9])|timed out|connection reset|Connection refused|Connection timed out|error sending request|error decoding response body'

for ((attempt = 1; attempt <= attempts; attempt++)); do
    if uv sync --locked --all-packages --all-groups >"$log" 2>&1; then
        cat "$log"
        exit 0
    fi
    cat "$log"
    if ! grep -qEi "$transient" "$log"; then
        echo "::error::uv sync failed with a non-transient error; not retrying"
        exit 1
    fi
    if ((attempt == attempts)); then
        echo "::error::uv sync failed after ${attempts} transient network attempts"
        exit 1
    fi
    echo "::warning::Transient dependency index failure; retry ${attempt}/${attempts} in $((attempt * delay))s"
    sleep "$((attempt * delay))"
done
