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
uv run mypy
uv run lint-imports
uv run python scripts/dev-env/validate-contracts.py
uv run pytest --cov --cov-report=term-missing
uv run python scripts/dev-env/media-smoke.py
pnpm exec playwright install --dry-run chromium >/dev/null
node scripts/dev-env/browser-smoke.mjs
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
uv build --all-packages --out-dir dist/python
docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --quiet
./scripts/dev-env/service-smoke.sh

git diff --check
printf 'SpeechNexus verification: passed\n'
