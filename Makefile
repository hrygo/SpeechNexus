.PHONY: install dev-bootstrap dev-up dev-down dev-status dev-check api-dev gateway-dev web-dev \
	browser-install browser-smoke media-smoke contracts-check contracts-compat contracts-sdk-test contracts-generate contracts-drift \
	service-smoke stack-config mysql-service \
	format lint typecheck test integration-test coordination-test python-build build verify

install:
	uv sync --locked --all-packages --all-groups
	pnpm install --frozen-lockfile

dev-bootstrap:
	./scripts/dev-env/bootstrap.sh

dev-up:
	systemctl --user start speechnexus-postgres.service speechnexus-minio.service
	./scripts/dev-env/mysql-service.sh
	./scripts/dev-env/check.sh

dev-down:
	systemctl --user stop speechnexus-minio.service speechnexus-postgres.service
	./scripts/dev-env/mysql-service.sh stop

dev-status:
	./scripts/dev-env/check.sh

dev-check: dev-status

mysql-service:
	./scripts/dev-env/mysql-service.sh

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

contracts-generate:
	uv run python scripts/dev-env/generate-contract-bindings.py

contracts-drift:
	uv run python scripts/dev-env/generate-contract-bindings.py --check

contracts-check:
	uv run python scripts/dev-env/validate-contracts.py

contracts-compat:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-openai-contract-compat.py --base "$$CONTRACT_BASE_REF"

contracts-sdk-test:
	pnpm test:contracts-sdk

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

integration-test:
	@test -n "$$SPEECHNEXUS_TEST_DATABASE_URL" && test -n "$$SPEECHNEXUS_TEST_MYSQL_URL" || \
		{ echo "Both isolated PostgreSQL and MySQL test URLs are required" >&2; exit 2; }
	./scripts/dev-env/mysql-service.sh
	@test -n "$$SPEECHNEXUS_TEST_MINIO_ENDPOINT" && test -n "$$SPEECHNEXUS_TEST_MINIO_ACCESS_KEY" && test -n "$$SPEECHNEXUS_TEST_MINIO_SECRET_KEY" || \
		{ echo "Isolated MinIO endpoint and test keys are required" >&2; exit 2; }
	./scripts/dev-env/minio-test-service.sh
	uv run pytest -m integration tests/integration -q

coordination-test:
	@test -n "$$SPEECHNEXUS_TEST_DATABASE_URL" && test -n "$$SPEECHNEXUS_TEST_MYSQL_URL" || \
		{ echo "Both isolated PostgreSQL and MySQL test URLs are required" >&2; exit 2; }
	./scripts/dev-env/mysql-service.sh
	uv run pytest tests/contract/test_coordination_transactions.py \
		tests/integration/test_coordination_transactions.py \
		tests/integration/test_production_jobs.py \
		tests/integration/test_production_owners.py \
		tests/integration/test_production_claims.py \
		tests/integration/test_production_leases.py \
		tests/integration/test_lease_release.py \
		tests/integration/test_runtime_intent_storage.py \
		tests/integration/test_production_runtime_intents.py \
		tests/integration/test_runtime_observations.py \
		tests/integration/test_runtime_creation_final_clock.py \
		tests/integration/test_coordination_foundation.py -q

python-build:
	uv build --all-packages --out-dir dist/python

build: python-build
	pnpm build

verify:
	./scripts/dev-env/verify.sh
