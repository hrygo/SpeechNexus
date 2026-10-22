#!/usr/bin/env bash

# Source this file from the repository root:
#   source scripts/dev-env/activate.sh

_speechnexus_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_speechnexus_env_file="$_speechnexus_repo_root/.runtime/config/dev.env"
if [[ ! -f "$_speechnexus_env_file" ]]; then
  echo "Run './scripts/dev-env/bootstrap.sh' first." >&2
  return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
source "$_speechnexus_env_file"
set +a
unset _speechnexus_repo_root _speechnexus_env_file
