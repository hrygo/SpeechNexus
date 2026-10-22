#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

./scripts/dev-env/check.sh
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/speechnexus-postgres.service" \
  "$HOME/.config/systemd/user/speechnexus-minio.service"
bash -n scripts/dev-env/*.sh
node --check scripts/dev-env/browser-smoke.mjs
uv run ruff check .
uv run ruff format --check .
uv run mypy scripts/dev-env/*.py
uv run python scripts/dev-env/validate-contracts.py
uv run python scripts/dev-env/media-smoke.py
pnpm exec playwright install --dry-run chromium >/dev/null
node scripts/dev-env/browser-smoke.mjs

if find tests -type f -name 'test_*.py' -print -quit 2>/dev/null | grep -q .; then
  uv run pytest
else
  printf 'skip pytest (no Python tests yet)\n'
fi

git diff --check
printf 'SpeechNexus verification: passed\n'
