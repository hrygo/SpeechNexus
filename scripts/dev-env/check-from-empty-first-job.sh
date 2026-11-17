#!/usr/bin/env bash
# ---- usage-start
# Re-runnable check for the "empty database → POST /v1/nexus/jobs returns 202"
# path documented in docs/operations/compose-minimal-from-zero.md §13.
#
# It drives only the parts of that path that live above the Compose topology:
# publish one binding through the public admin routes, upload one inspected
# asset, register one instance through the one-shot registration profile, then
# create the Job. The topology itself (postgres, minio, migration, the non-owner
# roles, api, gateway) comes from compose-minimal.md §0–§7; this script refuses
# to run against a stack whose probes are not 200.
#
# What a PASS proves. Each phase asserts the outcome it is supposed to see, and
# two of them are counter-examples kept executable instead of being a one-off
# manual note:
#   * without the publication step the create call must be 422 model_not_published
#   * with a workspace quota whose policy_revision differs from the recipe the
#     registration must fail and name quota_policy_conflict
# `--phase all` additionally starts by asserting the database is untouched
# (publications, binding_revisions, workspace_quotas, host_nodes,
# engine_instances and held_slots are all 0), so a green `all` means "from an
# empty database to 202". Phase create only passes on a real 202; the capacity
# refusal is the separate phase create-expect-429, so a used stack cannot pass
# this script by falling into the 429 branch.
#
# Nothing here fabricates a row: the asset comes from POST /v1/nexus/assets,
# the binding from the three admin routes, and host_nodes / workspace_quotas /
# engine_instances from `speechnexus-register-engine`. The engine behind the
# registration probe is a local /health-only stand-in (see --engine-stub): it
# proves the registration path, not that a model can transcribe. This script
# does not build images, run migrations, or edit deploy/compose.yaml.
#
# Phases: require-empty probe counterexample-unpublished publish asset register
#         create create-expect-429 counterexample-policy
#
# Usage:
#   scripts/dev-env/check-from-empty-first-job.sh --phase all
#   scripts/dev-env/check-from-empty-first-job.sh --phase require-empty
#   scripts/dev-env/check-from-empty-first-job.sh --phase counterexample-unpublished
#   scripts/dev-env/check-from-empty-first-job.sh --phase create --asset-id asset-…
#   scripts/dev-env/check-from-empty-first-job.sh --phase create-expect-429 --asset-id asset-…
#
# --require-empty runs the empty-start assertion before any single phase.
#
# Exit codes: 0 every assertion of the selected phases holds; 1 an assertion
# failed or a prerequisite probe is not 200; 2 the selected phase needs an input
# this run cannot derive (missing --asset-id, unreadable compose env, no venv).
# ---- usage-end

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

API_URL="http://127.0.0.1:8000"
GATEWAY_URL="http://127.0.0.1:8010"
ENV_FILE="deploy/compose.env"
COMPOSE_FILE="deploy/compose.yaml"
PROJECT="${COMPOSE_PROJECT_NAME:-}"
STATE_FILE=".runtime/from-empty-state.json"
PHASE="all"
BINDING_ID="model-compose-example"
UNPUBLISHED_BINDING_ID="model-unpublished-example"
CAPACITY_MANIFEST_ID="capacity-manifest-compose-example"
QUALIFICATION_REPORT_ID="qualification-compose-example"
ENGINE_STUB_PORT="18100"
ENGINE_STUB_PID=""
ASSET_ID=""
KEEP_STUB="0"
REQUIRE_EMPTY="0"
HTTP_BODY=""


usage() {
  awk '/^# ---- usage-start/{on=1;next} /^# ---- usage-end/{on=0} on' "${BASH_SOURCE[0]}" \
    | sed 's/^# \{0,1\}//'
}

fail() {
  printf 'FAIL %s\n' "$*" >&2
  if declare -F stop_engine_stub >/dev/null; then
    stop_engine_stub
  fi
  exit 1
}

pass() {
  printf 'PASS %s\n' "$*"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:?--phase needs a value}"; shift 2 ;;
    --url) API_URL="${2:?--url needs a value}"; shift 2 ;;
    --gateway-url) GATEWAY_URL="${2:?--gateway-url needs a value}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a value}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --state-file) STATE_FILE="${2:?--state-file needs a value}"; shift 2 ;;
    --binding-id) BINDING_ID="${2:?--binding-id needs a value}"; shift 2 ;;
    --unpublished-binding-id) UNPUBLISHED_BINDING_ID="${2:?--unpublished-binding-id needs a value}"; shift 2 ;;
    --capacity-manifest-id) CAPACITY_MANIFEST_ID="${2:?--capacity-manifest-id needs a value}"; shift 2 ;;
    --qualification-report-id) QUALIFICATION_REPORT_ID="${2:?--qualification-report-id needs a value}"; shift 2 ;;
    --engine-stub-port) ENGINE_STUB_PORT="${2:?--engine-stub-port needs a value}"; shift 2 ;;
    --asset-id) ASSET_ID="${2:?--asset-id needs a value}"; shift 2 ;;
    --keep-engine-stub) KEEP_STUB="1"; shift ;;
    --require-empty) REQUIRE_EMPTY="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" ;;
  esac
done

COMPOSE=(docker compose)
if [ -n "$PROJECT" ]; then
  COMPOSE+=(-p "$PROJECT")
fi
COMPOSE+=(--env-file "$ENV_FILE" -f "$COMPOSE_FILE")

[ -r "$ENV_FILE" ] || { usage >&2; printf 'compose env file not readable: %s\n' "$ENV_FILE" >&2; exit 2; }

HTTP_BODY="$(mktemp)"
trap 'rm -f "$HTTP_BODY"' EXIT

python_bin() {
  if [ -x .venv/bin/python ]; then
    printf '%s\n' .venv/bin/python
  else
    printf '%s\n' python3
  fi
}

new_idempotency_key() {
  python3 -c 'import uuid; print(uuid.uuid4())'
}

# --- state: values a later phase needs from an earlier one -------------------

state_put() {
  local key="$1" value="$2"
  mkdir -p "$(dirname "$STATE_FILE")"
  python3 - "$STATE_FILE" "$key" "$value" <<'PY'
import json, sys
from pathlib import Path
path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
document = json.loads(path.read_text()) if path.exists() else {}
document[key] = value
path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY
}

state_get() {
  python3 - "$STATE_FILE" "${1:-}" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
key = sys.argv[2]
if not path.exists():
    raise SystemExit(0)
document = json.loads(path.read_text())
print(document.get(key, "") if key else json.dumps(document, sort_keys=True))
PY
}

# --- HTTP helpers ------------------------------------------------------------

# http_post <path> <json-body-file>
# Prints the status code; the response body lands in the global $HTTP_BODY
# (a command substitution runs in a subshell, so the path has to be set here).
http_post() {
  local path="$1" body="$2"
  curl -sS -o "$HTTP_BODY" -w '%{http_code}' -X POST "$API_URL$path" \
    -H "content-type: application/json" \
    -H "Idempotency-Key: $(new_idempotency_key)" \
    --data-binary "@$body"
}

expect_status() {
  local want="$1" got="$2" label="$3"
  if [ "$want" != "$got" ]; then
    printf '  response body: %s\n' "$(cat $HTTP_BODY 2>/dev/null || true)" >&2
    fail "$label: expected HTTP $want, got $got"
  fi
}

expect_error_code() {
  local want="$1" label="$2"
  local got
  got="$(python3 - $HTTP_BODY "$want" <<'PY'
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_text()
want = sys.argv[2]
try:
    document = json.loads(raw)
except ValueError:
    print("<not-json>")
    raise SystemExit(0)
print(document.get("error", {}).get("code", "<no-error-code>"))
PY
)"
  if [ "$got" != "$want" ]; then
    printf '  response body: %s\n' "$(cat $HTTP_BODY || true)" >&2
    fail "$label: expected error code $want, got $got"
  fi
  pass "$label: error code $want"
}

# current_head_revision <binding_id> — the published head, read over the admin
# list route (no SQL): empty when the family has never been published.
current_head_revision() {
  local binding_id="$1" body
  body="$(mktemp)"
  if ! curl -sS -o "$body" "$API_URL/v1/nexus/admin/model-bindings"; then
    rm -f "$body"
    fail "cannot read GET /v1/nexus/admin/model-bindings"
  fi
  python3 - "$body" "$binding_id" <<'PY'
import json, sys
from pathlib import Path
document = json.loads(Path(sys.argv[1]).read_text())
for entry in document.get("data", []):
    if entry.get("binding_id") == sys.argv[2]:
        print(entry.get("current_revision") or "")
        break
PY
  rm -f "$body"
}

response_field() {

  python3 - $HTTP_BODY "$1" <<'PY'
import json, sys
from pathlib import Path
document = json.loads(Path(sys.argv[1]).read_text())
value = document
for part in sys.argv[2].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else value)
PY
}

sql_scalar() {
  "${COMPOSE[@]}" exec -T postgres psql -U speechnexus -d speechnexus -Atc "$1"
}

# --- binding draft body ------------------------------------------------------
#
# Every digest below is read from the repository's own pins, never typed here:
# artifact_set comes from deploy/engines/file-asr-audio/artifact-pin.json and
# engine from the vLLM runtime pin. Nothing is verified against mounted model
# files, which is why this script cannot claim artifact integrity — see the
# "未验证项" list in docs/operations/compose-minimal-from-zero.md §14.
write_binding_draft() {
  local binding_id="$1" out="$2"
  local py
  py="$(python_bin)"
  "$py" - "$binding_id" "$CAPACITY_MANIFEST_ID" "$out" <<'PY'
import json, sys
from pathlib import Path

binding_id, manifest_id, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
pin = json.loads(Path("deploy/engines/file-asr-audio/artifact-pin.json").read_text())
runtime = json.loads(
    Path("deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/runtime-pin.json").read_text()
)
definition = json.loads(Path("deploy/engines/file-asr-audio/binding-definition.json").read_text())
recipe = json.loads(Path("deploy/.runtime/private/transcription-recipe.json").read_text())

artifacts = [entry["artifact"] for entry in pin["model"]["files"]]
artifacts.append(pin["vad"]["artifact"])
body = {
    "binding_id": binding_id,
    "logical_model": recipe["model_id"],
    "tasks": definition["tasks"],
    "engine": {
        "kind": "vllm",
        "image_digest": runtime["image"],
        "runtime_version": runtime["version"],
    },
    "artifact_set": artifacts,
    "execution_recipe": {
        "recipe_revision": definition["execution_recipe"]["recipe_revision"],
        "capacity_manifest_id": manifest_id,
        "stage_roles": definition["execution_recipe"]["stage_roles"],
        "requires_complete_reservation": definition["execution_recipe"].get(
            "requires_complete_reservation", True
        ),
    },
    "parameter_domains": definition["parameter_domains"],
    "execution_defaults": {
        "asr": {
            "language": "auto",
            "turn_detection": {
                "mode": "server_vad",
                "silence_duration_ms": recipe["asr"]["silence_duration_ms"],
            },
            "finalization": "full_segment",
            "preview_interval_ms": recipe["asr"]["preview_interval_ms"],
            "max_segment_ms": 20000,
            "rollback_tokens": 5,
            "final_deadline_ms": recipe["asr"]["final_deadline_ms"],
            "auxiliary_deadline_ms": recipe["asr"]["auxiliary_deadline_ms"],
            "alignment": "off",
            "diarization": "off",
            "vocabulary_set_revision": None,
            "temperature": recipe["asr"]["temperature"],
            "timeout_seconds": 120,
        }
    },
    "release_tier": "experimental",
}
out.write_text(json.dumps(body, indent=2) + "\n")
PY
}

# --- phases ------------------------------------------------------------------

phase_probe() {
  local url status
  for url in "$API_URL/healthz" "$API_URL/readyz" "$GATEWAY_URL/healthz" "$GATEWAY_URL/readyz"; do
    status="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    [ "$status" = "200" ] || fail "probe $url returned ${status:-<no answer>}; finish compose-minimal.md §0–§7 first"
    pass "probe $url 200"
  done
}

# Assert the database is untouched, so a PASS means "from an empty database".
# Every count below is written by a product entry point (publish, asset upload,
# registration, create), so a non-zero row means the stack was already used.
phase_require_empty() {
  local problems="" value
  for pair in \
    "speech_publications:publications" \
    "speech_binding_revisions:binding_revisions" \
    "workspace_quotas:workspace_quotas" \
    "host_nodes:host_nodes" \
    "engine_instances:engine_instances"; do
    value="$(sql_scalar "select count(*) from speech.${pair%%:*}")"
    if [ "$value" != "0" ]; then
      problems="$problems ${pair##*:}=$value"
    fi
  done
  value="$(sql_scalar "select coalesce(sum(held_slots),0) from speech.engine_instances")"
  if [ "$value" != "0" ]; then
    problems="$problems held_slots=$value"
  fi
  if [ -n "$problems" ]; then
    fail "the stack is not empty ($problems): a PASS here has to mean 'from an empty database', so start from fresh PostgreSQL and MinIO volumes (docker compose … down -v) and re-run"
  fi
  pass "empty start: publications=0 binding_revisions=0 workspace_quotas=0 host_nodes=0 engine_instances=0 held_slots=0"
}

phase_counterexample_unpublished() {
  phase_probe
  local work
  work="$(mktemp -d)"
  write_binding_draft "$UNPUBLISHED_BINDING_ID" "$work/draft.json"
  local status
  status="$(http_post /v1/nexus/admin/model-bindings "$work/draft.json")"
  expect_status 201 "$status" "draft-only revision for $UNPUBLISHED_BINDING_ID"
  pass "draft-only binding $UNPUBLISHED_BINDING_ID created (never published)"

  printf '{"kind":"asr","model":"%s","input":{"audio_asset_id":"asset-000000000000"}}\n' \
    "$UNPUBLISHED_BINDING_ID" > "$work/job.json"
  status="$(http_post /v1/nexus/jobs "$work/job.json")"
  expect_status 422 "$status" "create Job without the publication step"
  expect_error_code model_not_published "create Job without the publication step"
  pass "counterexample holds: skipping publication is a named 422, not a 500"
  rm -rf "$work"
}

phase_publish() {
  phase_probe
  local work head_revision revision revision_id status
  work="$(mktemp -d)"
  write_binding_draft "$BINDING_ID" "$work/draft.json"

  status="$(http_post /v1/nexus/admin/model-bindings "$work/draft.json")"
  expect_status 201 "$status" "draft revision for $BINDING_ID"
  revision="$(response_field revision)"
  [ -n "$revision" ] || fail "draft response carried no revision"
  pass "draft revision $revision for $BINDING_ID"

  head_revision="$(current_head_revision "$BINDING_ID")"
  if [ -n "$head_revision" ]; then
    head_arg="\"$head_revision\""
  else
    head_arg="null"
  fi
  printf '{"expected_revision":"%s","expected_current_revision":%s,"capacity_manifest_id":"%s","qualification_report_id":"%s"}\n' \
    "$revision" "$head_arg" "$CAPACITY_MANIFEST_ID" "$QUALIFICATION_REPORT_ID" > "$work/publication.json"
  status="$(http_post "/v1/nexus/admin/model-bindings/$BINDING_ID/publications" "$work/publication.json")"
  expect_status 201 "$status" "publication of $BINDING_ID revision $revision"
  revision_id="$(response_field revision_id)"
  [ -n "$revision_id" ] || fail "publication response carried no revision_id"
  pass "published $BINDING_ID revision $revision as $revision_id"

  printf '{"expected_current_revision":"%s"}\n' "$revision" > "$work/activation.json"
  status="$(http_post "/v1/nexus/admin/model-bindings/$BINDING_ID/revisions/$revision/activations" "$work/activation.json")"
  expect_status 200 "$status" "activation of $BINDING_ID revision $revision"
  pass "activated $BINDING_ID revision $revision"

  state_put binding_id "$BINDING_ID"
  state_put binding_revision "$revision"
  state_put binding_revision_id "$revision_id"

  rebind_private_inputs "$revision_id"
  rm -rf "$work"
}

# The recipe and the ASR endpoint pin the binding by name; without this the Job
# can still be created (202) but every execution is rejected later. Both are
# private inputs, and the recipe digest is recalculated over the changed body.
rebind_private_inputs() {
  local revision_id="$1" py
  py="$(python_bin)"
  "$py" - "$revision_id" <<'PY'
import hashlib, json, sys
from pathlib import Path

revision_id = sys.argv[1]
try:
    import rfc8785
except ModuleNotFoundError:
    raise SystemExit(
        "rfc8785 is missing: run `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups` first"
    )

recipe_path = Path("deploy/.runtime/private/transcription-recipe.json")
document = json.loads(recipe_path.read_text(encoding="utf-8"))
document["binding_revision_id"] = revision_id
if "binding_publication_id" in document:
    document["binding_publication_id"] = revision_id
document["runtime_plan"]["binding"]["binding_revision_id"] = revision_id
body = {key: value for key, value in document.items() if key != "recipe_digest"}
document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
recipe_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
recipe_path.chmod(0o600)

for endpoint in ("gateway-file-asr-endpoint.json",):
    path = Path("deploy/.runtime/private") / endpoint
    if not path.exists():
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    data["target"]["binding_revision_id"] = revision_id
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
print(f"private inputs rebound to {revision_id}")
PY
  pass "private recipe/endpoint rebound to $revision_id (recipe digest recalculated)"
  "${COMPOSE[@]}" up -d api gateway >/dev/null 2>&1
  local url status
  for url in "$API_URL/readyz" "$GATEWAY_URL/readyz"; do
    for _ in $(seq 1 60); do
      status="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
      [ "$status" = "200" ] && break
      sleep 1
    done
    [ "$status" = "200" ] || fail "$url did not return to 200 after the rebuild"
  done
  pass "api and gateway restarted with the rebound private inputs"
}

phase_asset() {
  phase_probe
  local work status
  work="$(mktemp -d)"
  python3 - "$work/synthetic-input.wav" <<'PY'
import io, math, struct, sys, wave
from pathlib import Path

rate, seconds = 16000, 1
buffer = io.BytesIO()
with wave.open(buffer, "wb") as handle:
    handle.setnchannels(1)
    handle.setsampwidth(2)
    handle.setframerate(rate)
    handle.writeframes(
        b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(rate * seconds))
    )
Path(sys.argv[1]).write_bytes(buffer.getvalue())
PY
  status="$(curl -sS -o "$HTTP_BODY" -w '%{http_code}' -X POST "$API_URL/v1/nexus/assets" \
    -H "Idempotency-Key: $(new_idempotency_key)" \
    -F "purpose=asr_input" \
    -F "file=@$work/synthetic-input.wav;type=audio/wav")"
  expect_status 201 "$status" "asset upload"
  local asset_id asset_status
  asset_id="$(response_field id)"
  asset_status="$(response_field status)"
  [ -n "$asset_id" ] || fail "asset response carried no id"
  [ "$asset_status" = "ready" ] || fail "asset $asset_id is '$asset_status', not 'ready': is the asset-inspector profile assembled?"
  pass "asset $asset_id ready ($(response_field media_type), $(response_field duration_ms) ms)"
  rm -rf "$work"
  state_put asset_id "$asset_id"
  ASSET_ID="$asset_id"
}

start_engine_stub() {
  local probe="http://127.0.0.1:$ENGINE_STUB_PORT/health" status
  status="$(curl -s -o /dev/null -w '%{http_code}' "$probe" 2>/dev/null || true)"
  if [ "$status" = "200" ]; then
    pass "engine health already answering $probe; reusing it and leaving it running"
    ENGINE_STUB_PID=""
    return 0
  fi
  python3 - "$ENGINE_STUB_PORT" >/dev/null 2>&1 <<'PY' &
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])


class Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"status": "ok", "service": "stub-engine-health-only"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", port), Health).serve_forever()
PY
  ENGINE_STUB_PID="$!"
  local url="http://127.0.0.1:$ENGINE_STUB_PORT/health" status
  for _ in $(seq 1 50); do
    status="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    [ "$status" = "200" ] && break
    sleep 0.1
  done
  [ "$status" = "200" ] || fail "engine stub did not answer $url"
  pass "engine stub answering $url (health probe only; it cannot transcribe)"
}

stop_engine_stub() {
  if [ -n "$ENGINE_STUB_PID" ] && [ "$KEEP_STUB" != "1" ]; then
    kill "$ENGINE_STUB_PID" 2>/dev/null || true
    wait "$ENGINE_STUB_PID" 2>/dev/null || true
    ENGINE_STUB_PID=""
  fi
}

phase_register() {
  phase_probe
  start_engine_stub
  local out outcome
  out="$(mktemp)"
  set +e
  "${COMPOSE[@]}" --profile engine-registration run --rm engine-register >"$out" 2>&1
  local code=$?
  set -e
  outcome="$(python3 - "$out" <<'PY'
import json, sys
from pathlib import Path
raw = Path(sys.argv[1]).read_text()
start = raw.find("{")
if start < 0:
    print("<no-json>")
else:
    print(json.loads(raw[start:]).get("outcome", "<no-outcome>"))
PY
)"
  if [ "$code" -ne 0 ]; then
    printf '  registration output: %s\n' "$(tail -3 "$out")" >&2
    rm -f "$out"
    fail "engine-register exited $code"
  fi
  case "$outcome" in
    registered|already_ready) pass "engine-register outcome: $outcome" ;;
    *) printf '  registration output: %s\n' "$(tail -3 "$out")" >&2; rm -f "$out"; fail "engine-register outcome: $outcome" ;;
  esac
  rm -f "$out"

  local quota node instance
  quota="$(sql_scalar "select workspace_id||'|'||max_attempts||'|'||policy_revision from speech.workspace_quotas")"
  node="$(sql_scalar "select host_node_id||'|'||node_state from speech.host_nodes")"
  instance="$(sql_scalar "select instance_id||'|'||instance_generation||'|'||instance_state||'|'||binding_revision_id||'|'||manifest_id from speech.engine_instances")"
  [ -n "$quota" ] || fail "registration wrote no workspace_quotas row"
  [ -n "$node" ] || fail "registration wrote no host_nodes row"
  [ -n "$instance" ] || fail "registration wrote no engine_instances row"
  pass "workspace_quotas: $quota"
  pass "host_nodes: $node"
  pass "engine_instances: $instance"

  stop_engine_stub

  local published
  published="$(state_get binding_revision_id)"
  if [ -n "$published" ]; then
    case "$instance" in
      *"|$published|"*) pass "instance binding identity matches the published revision $published" ;;
      *) fail "instance binding identity does not match the published revision $published" ;;
    esac
  fi
}

phase_create() {
  phase_probe
  local asset_id="${ASSET_ID:-$(state_get asset_id)}" status
  [ -n "$asset_id" ] || { printf 'run --phase asset first, or pass --asset-id\n' >&2; exit 2; }

  # This phase only passes on a real 202. The refusal a used stack produces is a
  # separate assertion (phase create-expect-429), never a silent alternative:
  # otherwise a green run would not prove that the happy path was reached.
  local held
  held="$(sql_scalar "select coalesce(sum(held_slots),0) from speech.engine_instances")"
  if [ "$held" != "0" ]; then
    fail "create expects a free capacity slot but $held slot(s) are already held: assert the capacity refusal with --phase create-expect-429, or start from an empty stack"
  fi

  local work
  work="$(mktemp -d)"
  printf '{"kind":"asr","model":"%s","input":{"audio_asset_id":"%s"}}\n' "$BINDING_ID" "$asset_id" > "$work/job.json"
  status="$(http_post /v1/nexus/jobs "$work/job.json")"
  expect_status 202 "$status" "create Job on a fully provisioned stack"
  local job_id
  job_id="$(response_field id)"
  [ -n "$job_id" ] || fail "create response carried no Job id"
  pass "Job $job_id queued ($(response_field status))"
  state_put job_id "$job_id"
  rm -rf "$work"
}

# The capacity refusal is its own assertion: the example recipe registers one
# slot (max_slots=1), so a queued Job holds it until the lease is released or
# admission's deadline expires.
phase_create_expect_429() {
  phase_probe
  local asset_id="${ASSET_ID:-$(state_get asset_id)}" status
  [ -n "$asset_id" ] || { printf 'run --phase asset first, or pass --asset-id\n' >&2; exit 2; }
  local work held
  work="$(mktemp -d)"
  held="$(sql_scalar "select coalesce(sum(held_slots),0) from speech.engine_instances")"
  printf '{"kind":"asr","model":"%s","input":{"audio_asset_id":"%s"}}\n' "$BINDING_ID" "$asset_id" > "$work/job.json"
  status="$(http_post /v1/nexus/jobs "$work/job.json")"
  expect_status 429 "$status" "create Job while $held slot(s) are already held"
  expect_error_code job_capacity_exhausted "create Job while $held slot(s) are already held"
  pass "capacity refusal holds with $held held slot(s): the next create is 429 job_capacity_exhausted"
  rm -rf "$work"
}

phase_counterexample_policy() {
  phase_probe
  local recipe="deploy/.runtime/private/transcription-recipe.json" backup
  [ -f "$recipe" ] || { printf 'missing private recipe: %s\n' "$recipe" >&2; exit 2; }
  backup="$(mktemp)"
  cp "$recipe" "$backup"

  local py
  py="$(python_bin)"
  "$py" - <<'PY'
import hashlib, json, sys
from pathlib import Path

import rfc8785

path = Path("deploy/.runtime/private/transcription-recipe.json")
document = json.loads(path.read_text(encoding="utf-8"))
document["policy_revision_id"] = "policy-revision-mismatch"
body = {key: value for key, value in document.items() if key != "recipe_digest"}
document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
  start_engine_stub
  local out code
  out="$(mktemp)"
  set +e
  "${COMPOSE[@]}" --profile engine-registration run --rm engine-register >"$out" 2>&1
  code=$?
  set -e
  stop_engine_stub
  cp "$backup" "$recipe"
  chmod 600 "$recipe"
  rm -f "$backup"
  if [ "$code" -eq 0 ]; then
    printf '  registration output: %s\n' "$(tail -3 "$out")" >&2
    rm -f "$out"
    fail "registration accepted a policy_revision that differs from the existing workspace quota"
  fi
  grep -q 'quota_policy_conflict' "$out" || {
    printf '  registration output: %s\n' "$(tail -5 "$out")" >&2
    rm -f "$out"
    fail "registration failed (exit $code) without naming quota_policy_conflict"
  }
  pass "counterexample holds: policy_revision mismatch is exit $code and names quota_policy_conflict"
  printf '  %s\n' "$(grep -m1 'quota_policy_conflict' "$out")"
  rm -f "$out"
}

run_phase() {
  case "$1" in
    probe) phase_probe ;;
    counterexample-unpublished) phase_counterexample_unpublished ;;
    publish) phase_publish ;;
    asset) phase_asset ;;
    register) phase_register ;;
    create) phase_create ;;
    create-expect-429) phase_create_expect_429 ;;
    require-empty) phase_require_empty ;;
    counterexample-policy) phase_counterexample_policy ;;
    *) usage >&2; fail "unknown phase: $1" ;;
  esac
}

if [ "$PHASE" = "all" ]; then
  for step in require-empty probe counterexample-unpublished publish asset register create counterexample-policy; do
    printf '\n== phase %s ==\n' "$step"
    run_phase "$step"
  done
else
  if [ "$REQUIRE_EMPTY" = "1" ] && [ "$PHASE" != "require-empty" ]; then
    printf '\n== phase require-empty ==\n'
    phase_require_empty
  fi
  run_phase "$PHASE"
fi

printf '\nRESULT PASS\n'
