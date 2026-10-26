.PHONY: install dev-bootstrap dev-up dev-down dev-status dev-check api-dev gateway-dev web-dev \
	browser-install browser-smoke media-smoke contracts-check contracts-compat contracts-http-changes contracts-speech-changes contracts-coordination-requests contracts-coordination-results contracts-realtime-changes contracts-sdk-test contracts-generate contracts-drift \
	service-smoke stack-config mysql-service acceptance-map-check operation-coverage-check \
	format lint typecheck test integration-test workbench-llm-qualification coordination-test python-build build verify

install:
	uv sync --locked --all-packages --all-groups
	pnpm install --frozen-lockfile

# This is an acceptance audit, not a CI bypass; known mapping gaps fail the target.
acceptance-map-check:
	@test -n "$(ACCEPTANCE_EVIDENCE_ROOT)" || { echo "Set ACCEPTANCE_EVIDENCE_ROOT to the local evidence directory"; exit 1; }
	uv run python scripts/dev-env/check-acceptance-map.py --evidence-root "$(ACCEPTANCE_EVIDENCE_ROOT)"

# Operation coverage is a wiring audit; missing consumer/test legs fail the target.
operation-coverage-check:
	uv run python scripts/dev-env/check-operation-coverage.py

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

contracts-http-changes:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-http-contract-changes.py --base "$$CONTRACT_BASE_REF"

contracts-speech-changes:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-http-contract-changes.py --base "$$CONTRACT_BASE_REF" --surface speech

contracts-coordination-requests:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-coordination-request-changes.py --base "$$CONTRACT_BASE_REF"

contracts-coordination-results:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-coordination-request-changes.py --base "$$CONTRACT_BASE_REF" --surface result

contracts-realtime-changes:
	@test -n "$$CONTRACT_BASE_REF" || \
		{ echo "CONTRACT_BASE_REF is required" >&2; exit 2; }
	uv run python scripts/dev-env/check-realtime-contract-changes.py --base "$$CONTRACT_BASE_REF"

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

workbench-llm-qualification:
	@test -n "$$SPEECHNEXUS_WORKBENCH_TEST_DSN" && test -n "$$SPEECHNEXUS_TEST_LLM_ENDPOINT" && test -n "$$SPEECHNEXUS_TEST_LLM_MODEL" || \
		{ echo "An isolated Workbench PG and an approved real intranet LLM endpoint/model are required" >&2; exit 2; }
	pnpm build
	uv run pytest -m qualification tests/integration/test_workbench_minutes_live.py \
		tests/integration/test_workbench_real_runtime_minutes_browser.py -q

coordination-test:
	@test -n "$$SPEECHNEXUS_TEST_DATABASE_URL" && test -n "$$SPEECHNEXUS_TEST_MYSQL_URL" || \
		{ echo "Both isolated PostgreSQL and MySQL test URLs are required" >&2; exit 2; }
	./scripts/dev-env/mysql-service.sh
	uv run pytest tests/contract/test_coordination_transactions.py \
		tests/integration/test_coordination_transactions.py \
		tests/integration/test_production_jobs.py \
		tests/integration/test_job_recovery_unknown.py \
		tests/integration/test_production_owners.py \
		tests/integration/test_production_claims.py \
		tests/integration/test_production_admission.py \
		tests/integration/test_production_leases.py \
		tests/integration/test_lease_release.py \
		tests/integration/test_runtime_intent_storage.py \
		tests/integration/test_admission_command_storage.py \
		tests/integration/test_production_runtime_intents.py \
		tests/integration/test_runtime_controller_client.py \
		tests/integration/test_runtime_observations.py \
		tests/integration/test_runtime_creation_final_clock.py \
		tests/integration/test_coordination_foundation.py -q

python-build:
	uv build --all-packages --out-dir dist/python
	uv build --wheel --no-sources deploy/engines/vllm-asr-lifecycle --out-dir dist/python

build: python-build
	pnpm build

verify:
	./scripts/dev-env/verify.sh
