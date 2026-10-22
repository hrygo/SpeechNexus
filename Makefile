.PHONY: install dev-bootstrap dev-up dev-down dev-status dev-check api-dev gateway-dev web-dev \
	browser-install browser-smoke media-smoke contracts-check service-smoke stack-config \
	format lint typecheck test python-build build verify

install:
	uv sync --locked --all-packages --all-groups
	pnpm install --frozen-lockfile

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

api-dev:
	./scripts/dev-env/run-api.sh

gateway-dev:
	./scripts/dev-env/run-gateway.sh

web-dev:
	pnpm dev

browser-install:
	pnpm install --frozen-lockfile
	pnpm exec playwright install chromium

browser-smoke:
	node scripts/dev-env/browser-smoke.mjs

media-smoke:
	uv run python scripts/dev-env/media-smoke.py

contracts-check:
	uv run python scripts/dev-env/validate-contracts.py

service-smoke:
	./scripts/dev-env/service-smoke.sh

stack-config:
	docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --quiet

format:
	uv run ruff format .
	pnpm format

lint:
	uv run ruff check .
	uv run mypy
	uv run lint-imports
	pnpm lint

typecheck:
	pnpm typecheck

test:
	uv run pytest --cov --cov-report=term-missing
	pnpm test

python-build:
	uv build --all-packages --out-dir dist/python

build: python-build
	pnpm build

verify:
	./scripts/dev-env/verify.sh
