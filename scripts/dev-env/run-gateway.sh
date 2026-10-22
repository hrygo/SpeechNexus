#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
# shellcheck disable=SC1091
source scripts/dev-env/activate.sh

export SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL="${DATABASE_URL_DEV:?missing DATABASE_URL_DEV}"

exec uv run speechnexus-gateway "$@"
