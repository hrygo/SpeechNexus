.PHONY: dev-bootstrap dev-up dev-down dev-status dev-check browser-install browser-smoke media-smoke contracts-check verify

dev-bootstrap:
	./scripts/dev-env/bootstrap.sh

dev-up:
	systemctl --user start speechnexus-postgres.service speechnexus-minio.service
	./scripts/dev-env/check.sh

dev-down:
	systemctl --user stop speechnexus-minio.service speechnexus-postgres.service

dev-status:
	./scripts/dev-env/check.sh

dev-check: dev-status

browser-install:
	pnpm install --frozen-lockfile
	pnpm exec playwright install chromium

browser-smoke:
	node scripts/dev-env/browser-smoke.mjs

media-smoke:
	uv run python scripts/dev-env/media-smoke.py

contracts-check:
	uv run python scripts/dev-env/validate-contracts.py

verify:
	./scripts/dev-env/verify.sh
