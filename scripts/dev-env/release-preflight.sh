#!/usr/bin/env bash

# Release preflight: turns the DEL-120 C0-C12 checklist into a runnable gate.
#
# Defaults are read-only: no database writes, no write requests and no service
# restarts. C5/C6 are the only checks that can write, and only when
# --allow-write is passed explicitly. Missing inputs or unavailable
# environments produce SKIP with a reason; they never become PASS.
#
# Output is one line per check:
#   [PASS|FAIL|SKIP] <ID> <one sentence> | <actual value or skip reason>
#
# Exit codes: 0 no FAIL, 1 at least one FAIL, 2 usage/configuration error.
# SKIP does not fail the process, but the summary keeps it visible.

set -uo pipefail

# Keep git reads from refreshing the index; this gate must not touch the worktree.
export GIT_OPTIONAL_LOCKS=0

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)
cd "$ROOT" || {
  printf 'release-preflight: cannot enter repository root %s\n' "$ROOT" >&2
  exit 2
}

ALL_IDS="C0 C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 C11 C12"
ONLY_IDS=""
ALLOW_WRITE=0
EXPECT_HEAD="${SN_EXPECT_HEAD:-}"
CI_EXPECTED_JOBS="${SN_EXPECT_CI_JOBS:-14}"
PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
FAILED_IDS=""
SKIPPED_IDS=""
PREFLIGHT_TMP=""
ENV_LOAD_NOTE=""

usage() {
  cat <<'EOF'
Usage: scripts/dev-env/release-preflight.sh [options]

Options:
  --only C5,C7       run only the selected checklist IDs (C0-C12)
  --allow-write      allow C5/C6 to send write requests (default: SKIP)
  --expect-head SHA  require HEAD to equal the complete approved SHA
  -h, --help         show this help

Environment:
  SPEECHNEXUS_TOOLCHAIN_ENV  toolchain environment file (default native-20261027-v1)
  SN_PREFLIGHT_ENV           optional env file with SN_* values; when set it is the
                             only default env file sourced (no credentials are printed)
  SN_API_BASE                public API base, e.g. http://127.0.0.1:58000
  SN_SAMPLE_WAV              ASR input WAV used by C5
  SN_ASR_MODEL               ASR model id used by C5
  SN_TTS_MODEL               TTS model id used by C6
  SN_TTS_VOICE               published TTS voice used by C6
  SN_GATEWAY_BASE            gateway base for C2 (default http://127.0.0.1:58030)
  SN_PG_CONTAINER            PostgreSQL container for C3 (default del109-pg)
  SN_QUALIFICATION_REPORT    qualification report for C7
  SN_EXPECT_HEAD             same as --expect-head
  SN_EXPECT_CI_JOBS          minimum CI job count for C1 (default 14)

C5/C6 are SKIP unless --allow-write is passed and the values above are readable
from an existing env file. C12 is always SKIP because independent acceptance is
not something a script can grant.
EOF
}

usage_error() {
  printf 'release-preflight: %s\n' "$*" >&2
  usage >&2
  exit 2
}

add_only() {
  local raw="$1"
  local part
  local parts
  local old_ifs="$IFS"
  IFS=','
  read -r -a parts <<< "$raw"
  IFS="$old_ifs"

  [[ ${#parts[@]} -gt 0 ]] || usage_error "--only needs at least one ID"
  for part in "${parts[@]}"; do
    part=$(printf '%s' "$part" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')
    [[ -n "$part" ]] || usage_error "--only contains an empty ID"
    case "$part" in
      C0|C1|C2|C3|C4|C5|C6|C7|C8|C9|C10|C11|C12) ;;
      *) usage_error "unknown checklist ID: $part" ;;
    esac
    case " $ONLY_IDS " in
      *" $part "*) ;;
      *) ONLY_IDS="${ONLY_IDS} ${part}" ;;
    esac
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --only)
      [[ $# -ge 2 ]] || usage_error "--only needs an argument"
      add_only "$2"
      shift 2
      ;;
    --only=*)
      add_only "${1#*=}"
      shift
      ;;
    --allow-write)
      ALLOW_WRITE=1
      shift
      ;;
    --expect-head)
      [[ $# -ge 2 ]] || usage_error "--expect-head needs an argument"
      EXPECT_HEAD="$2"
      shift 2
      ;;
    --expect-head=*)
      EXPECT_HEAD="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage_error "unknown argument: $1"
      ;;
  esac
done

TOOLCHAIN_ENV="${SPEECHNEXUS_TOOLCHAIN_ENV:-/opt/homebrew/share/delegra-managed/speechnexus/native-20261027-v1/environment.sh}"
if [[ -r "$TOOLCHAIN_ENV" ]]; then
  # shellcheck disable=SC1090
  . "$TOOLCHAIN_ENV"
fi

selected() {
  local id="$1"
  [[ -z "$ONLY_IDS" ]] && return 0
  case " $ONLY_IDS " in
    *" $id "*) return 0 ;;
    *) return 1 ;;
  esac
}

record() {
  local status="$1"
  local id="$2"
  local description="$3"
  local actual="$4"

  actual=$(printf '%s' "$actual" | tr '\n' ' ')
  [[ -n "$actual" ]] || actual="-"
  printf '[%s] %s %s | %s\n' "$status" "$id" "$description" "$actual"

  case "$status" in
    PASS) PASS_COUNT=$((PASS_COUNT + 1)) ;;
    FAIL)
      FAIL_COUNT=$((FAIL_COUNT + 1))
      FAILED_IDS="${FAILED_IDS} ${id}"
      ;;
    SKIP)
      SKIP_COUNT=$((SKIP_COUNT + 1))
      SKIPPED_IDS="${SKIPPED_IDS} ${id}"
      ;;
  esac
}

cleanup() {
  if [[ -n "$PREFLIGHT_TMP" && -d "$PREFLIGHT_TMP" ]]; then
    rm -rf "$PREFLIGHT_TMP"
  fi
}
trap cleanup EXIT

ensure_tmp() {
  if [[ -z "$PREFLIGHT_TMP" ]]; then
    PREFLIGHT_TMP=$(mktemp -d "${TMPDIR:-/tmp}/speechnexus-preflight.XXXXXX" 2>/dev/null) || return 1
  fi
  return 0
}

load_env_file() {
  local file="$1"
  [[ -r "$file" ]] || return 1
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  return 0
}

if [[ -n "${SN_PREFLIGHT_ENV:-}" ]]; then
  if ! load_env_file "$SN_PREFLIGHT_ENV"; then
    ENV_LOAD_NOTE="SN_PREFLIGHT_ENV 不可读: ${SN_PREFLIGHT_ENV}"
  fi
else
  for candidate in \
    .paperclip-runtime/del109/api.env \
    .paperclip-runtime/del109/api-transcription.env \
    .runtime/config/api.env \
    .runtime/config/release-preflight.env; do
    if [[ -r "$candidate" ]]; then
      load_env_file "$candidate" || true
    fi
  done
fi

if [[ -z "${SN_API_BASE:-}" && -n "${SPEECHNEXUS_HOST:-}" && -n "${SPEECHNEXUS_PORT:-}" ]]; then
  api_host="$SPEECHNEXUS_HOST"
  [[ "$api_host" == "0.0.0.0" ]] && api_host="127.0.0.1"
  SN_API_BASE="http://${api_host}:${SPEECHNEXUS_PORT}"
fi

SN_GATEWAY_BASE="${SN_GATEWAY_BASE:-http://127.0.0.1:58030}"
SN_PG_CONTAINER="${SN_PG_CONTAINER:-del109-pg}"
SN_SAMPLE_WAV="${SN_SAMPLE_WAV:-.paperclip-runtime/del109/audio/speech-12s.wav}"
SN_QUALIFICATION_REPORT="${SN_QUALIFICATION_REPORT:-deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json}"

check_c0() {
  local head branch tree tree_desc tree_count
  local node_v pnpm_v uv_v py_v
  local missing="" issues="" mismatch=""
  local expected_desc="unset"

  head=$(git rev-parse HEAD 2>/dev/null) || {
    record FAIL C0 "基线与工具链" "not a git checkout"
    return
  }
  branch=$(git branch --show-current 2>/dev/null || true)
  if ! tree=$(git status --porcelain 2>/dev/null); then
    record FAIL C0 "基线与工具链" "git status failed HEAD=$head"
    return
  fi
  if [[ -n "$tree" ]]; then
    tree_count=$(printf '%s\n' "$tree" | wc -l | tr -d '[:space:]')
    tree_desc="dirty(${tree_count})"
  else
    tree_desc="clean"
  fi

  if command -v node >/dev/null 2>&1; then
    node_v=$(node --version 2>/dev/null) || node_v="missing"
  else
    node_v="missing"
  fi
  if command -v pnpm >/dev/null 2>&1; then
    pnpm_v=$(pnpm --version 2>/dev/null) || pnpm_v="missing"
  else
    pnpm_v="missing"
  fi
  if command -v uv >/dev/null 2>&1; then
    uv_v=$(uv --version 2>/dev/null | awk '{print $2}') || uv_v="missing"
  else
    uv_v="missing"
  fi
  if command -v python3.14 >/dev/null 2>&1; then
    py_v=$(python3.14 --version 2>&1 | awk '{print $2}') || py_v="missing"
  else
    py_v="missing"
  fi

  [[ "$node_v" == "missing" ]] && missing="${missing} node"
  [[ "$pnpm_v" == "missing" ]] && missing="${missing} pnpm"
  [[ "$uv_v" == "missing" ]] && missing="${missing} uv"
  [[ "$py_v" == "missing" ]] && missing="${missing} python3.14"

  if [[ -n "$EXPECT_HEAD" ]]; then
    expected_desc="$EXPECT_HEAD"
    [[ "$head" == "$EXPECT_HEAD" ]] || issues="${issues} HEAD!=expected"
  fi
  [[ "$tree_desc" == "clean" ]] || issues="${issues} tree=dirty"

  if [[ -n "$issues" ]]; then
    record FAIL C0 "基线与工具链" "HEAD=$head branch=${branch:-detached} tree=$tree_desc expected=$expected_desc${issues}"
    return
  fi
  if [[ -n "$missing" ]]; then
    record SKIP C0 "基线与工具链" "HEAD=$head tree=clean missing=${missing# }"
    return
  fi

  [[ "$node_v" == "v26.12.1" ]] || mismatch="${mismatch} node=$node_v"
  [[ "$pnpm_v" == "12.14.0" ]] || mismatch="${mismatch} pnpm=$pnpm_v"
  [[ "$uv_v" == "0.13.5" ]] || mismatch="${mismatch} uv=$uv_v"
  [[ "$py_v" == "3.14.9" ]] || mismatch="${mismatch} python=$py_v"

  if [[ -n "$mismatch" ]]; then
    record FAIL C0 "基线与工具链" "HEAD=$head tree=clean expected-versions mismatch:${mismatch}"
    return
  fi

  record PASS C0 "基线与工具链" "HEAD=$head branch=${branch:-detached} tree=clean node=$node_v pnpm=$pnpm_v uv=$uv_v python=$py_v expected=$expected_desc"
}

check_c1() {
  local head run_json run_id status conclusion jobs_json total failed expected

  if ! command -v gh >/dev/null 2>&1; then
    record SKIP C1 "候选 CI" "gh 不可用"
    return
  fi
  if ! command -v jq >/dev/null 2>&1; then
    record SKIP C1 "候选 CI" "jq 不可用"
    return
  fi
  head=$(git rev-parse HEAD 2>/dev/null) || {
    record SKIP C1 "候选 CI" "无法读取 HEAD"
    return
  }
  if ! run_json=$(gh run list --commit "$head" --limit 1 --json databaseId,status,conclusion 2>/dev/null); then
    record SKIP C1 "候选 CI" "gh 不可用或未认证"
    return
  fi

  run_id=$(printf '%s' "$run_json" | jq -r '.[0].databaseId // empty' 2>/dev/null)
  status=$(printf '%s' "$run_json" | jq -r '.[0].status // empty' 2>/dev/null)
  conclusion=$(printf '%s' "$run_json" | jq -r '.[0].conclusion // empty' 2>/dev/null)

  if [[ -z "$run_id" ]]; then
    record SKIP C1 "候选 CI" "HEAD=$head 没有 CI run（可能尚未 push）"
    return
  fi
  if [[ "$status" != "completed" ]]; then
    record SKIP C1 "候选 CI" "run=$run_id status=${status:-unknown} 尚未完成"
    return
  fi
  if [[ "$conclusion" != "success" ]]; then
    record FAIL C1 "候选 CI" "run=$run_id conclusion=${conclusion:-unknown} expected=success"
    return
  fi

  if ! jobs_json=$(gh run view "$run_id" --json jobs 2>/dev/null); then
    record SKIP C1 "候选 CI" "无法读取 run $run_id 的 jobs"
    return
  fi
  total=$(printf '%s' "$jobs_json" | jq -r '[.jobs[]?] | length' 2>/dev/null)
  failed=$(printf '%s' "$jobs_json" | jq -r '[.jobs[]? | select(.conclusion != "success")] | length' 2>/dev/null)

  if ! [[ "$total" =~ ^[0-9]+$ ]] || ! [[ "$failed" =~ ^[0-9]+$ ]]; then
    record SKIP C1 "候选 CI" "无法解析 run $run_id 的 job 计数"
    return
  fi
  if ! [[ "$CI_EXPECTED_JOBS" =~ ^[0-9]+$ ]] || [[ "$CI_EXPECTED_JOBS" -lt 1 ]]; then
    record SKIP C1 "候选 CI" "SN_EXPECT_CI_JOBS 不是正整数: $CI_EXPECTED_JOBS"
    return
  fi
  if [[ "$failed" -ne 0 ]]; then
    record FAIL C1 "候选 CI" "run=$run_id total=$total failed=$failed expected=0"
    return
  fi
  if [[ "$total" -lt "$CI_EXPECTED_JOBS" ]]; then
    record FAIL C1 "候选 CI" "run=$run_id total=$total failed=$failed expected>=$CI_EXPECTED_JOBS"
    return
  fi

  record PASS C1 "候选 CI" "run=$run_id total=$total failed=$failed expected>=$CI_EXPECTED_JOBS"
}

check_c2() {
  local url="$SN_GATEWAY_BASE"
  local response rc http body checks ok

  if ! command -v curl >/dev/null 2>&1; then
    record SKIP C2 "网关就绪" "curl 不可用"
    return
  fi
  if ! command -v jq >/dev/null 2>&1; then
    record SKIP C2 "网关就绪" "jq 不可用"
    return
  fi

  response=$(curl -sS --max-time 5 -w $'\n%{http_code}' "${url%/}/readyz" 2>/dev/null)
  rc=$?
  if [[ $rc -ne 0 ]]; then
    case "$rc" in
      7) record SKIP C2 "网关就绪" "$url 未监听（curl exit 7）" ;;
      28) record FAIL C2 "网关就绪" "$url/readyz 超时（curl exit 28）" ;;
      *) record SKIP C2 "网关就绪" "$url 不可访问（curl exit $rc）" ;;
    esac
    return
  fi

  http="${response##*$'\n'}"
  body="${response%$'\n'*}"
  if [[ "$http" != "200" ]]; then
    record FAIL C2 "网关就绪" "http=$http expected=200"
    return
  fi
  if ! printf '%s' "$body" | jq -e '.status=="ready" and (.checks|type=="array") and (.checks|length>0) and ([.checks[].status]|all(.=="ok"))' >/dev/null 2>&1; then
    body_status=$(printf '%s' "$body" | jq -r '.status // "?"' 2>/dev/null)
    bad_checks=$(printf '%s' "$body" | jq -r '[.checks[]? | select(.status != "ok") | .name] | join(",")' 2>/dev/null)
    record FAIL C2 "网关就绪" "http=200 status=${body_status:-?} non_ok_checks=${bad_checks:-none} assertion=FAIL"
    return
  fi

  checks=$(printf '%s' "$body" | jq -r '.checks|length' 2>/dev/null)
  ok=$(printf '%s' "$body" | jq -r '[.checks[]|select(.status=="ok")]|length' 2>/dev/null)
  record PASS C2 "网关就绪" "http=200 status=ready checks=$ok/$checks"
}

check_c3() {
  local container="$SN_PG_CONTAINER"
  local state unknown ready

  if ! command -v docker >/dev/null 2>&1; then
    record SKIP C3 "准入不变量" "docker 不可用"
    return
  fi
  if ! docker inspect "$container" >/dev/null 2>&1; then
    record SKIP C3 "准入不变量" "容器 $container 不存在或不可访问"
    return
  fi
  state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null)
  if [[ "$state" != "running" ]]; then
    record SKIP C3 "准入不变量" "容器 $container 状态 ${state:-unknown}"
    return
  fi

  if ! unknown=$(docker exec -u postgres "$container" psql -X -v ON_ERROR_STOP=1 -d speechnexus -Atc "SELECT count(*) FROM speech.submit_intents WHERE execution_state='unknown_quarantined';" 2>/dev/null); then
    record SKIP C3 "准入不变量" "无法读取 $container 的 unknown_quarantined 计数"
    return
  fi
  if ! ready=$(docker exec -u postgres "$container" psql -X -v ON_ERROR_STOP=1 -d speechnexus -Atc "SELECT count(*) FROM speech.engine_instances WHERE instance_state='ready';" 2>/dev/null); then
    record SKIP C3 "准入不变量" "无法读取 $container 的 ready instance 计数"
    return
  fi

  unknown=$(printf '%s\n' "$unknown" | head -n 1 | tr -d '[:space:]')
  ready=$(printf '%s\n' "$ready" | head -n 1 | tr -d '[:space:]')
  if ! [[ "$unknown" =~ ^[0-9]+$ ]] || ! [[ "$ready" =~ ^[0-9]+$ ]]; then
    record SKIP C3 "准入不变量" "查询未返回整数: unknown=${unknown:-empty} ready=${ready:-empty}"
    return
  fi
  if [[ "$unknown" -ne 0 ]]; then
    record FAIL C3 "准入不变量" "unknown_quarantined=$unknown expected=0 ready_instances=$ready"
    return
  fi
  if [[ "$ready" -lt 1 ]]; then
    record FAIL C3 "准入不变量" "unknown_quarantined=$unknown ready_instances=$ready expected>=1"
    return
  fi

  record PASS C3 "准入不变量" "unknown_quarantined=$unknown expected=0 ready_instances=$ready expected>=1"
}

check_c4() {
  # C4: ensure there is a production path that can write execution state to succeeded.
  # After #284, the successful terminal driver is application/execution_completion.py
  # calling ledger.record_stop(RecordStop(evidence, ExecutionState.SUCCEEDED)).
  # The check requires the SUCCEEDED argument, not merely any record_stop call:
  # abort/recover also call record_stop (with ABORTED), so matching record_stop
  # alone would PASS even when no path can write a succeeded terminal state.
  local hits line rest path lineno content
  local grep_rc
  local calls=""
  local definitions=0
  local call_count=0

  hits=$(git grep -n 'record_stop(.*ExecutionState\.SUCCEEDED' HEAD -- 'execution-gateway/src/**/*.py' 2>/dev/null)
  grep_rc=$?
  if [[ "$grep_rc" -eq 1 ]]; then
    hits=""
  elif [[ "$grep_rc" -ne 0 ]]; then
    record SKIP C4 "ASR 成功终态驱动者" "git grep failed (exit $grep_rc)"
    return
  fi
  if [[ -z "$hits" ]]; then
    record FAIL C4 "ASR 成功终态驱动者" "no matches in execution-gateway/src (expected one production call)"
    return
  fi

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    rest="${line#HEAD:}"
    path="${rest%%:*}"
    rest="${rest#*:}"
    lineno="${rest%%:*}"
    content="${rest#*:}"
    if [[ "$content" =~ (^|[[:space:]])(async[[:space:]]+)?def[[:space:]]+record_stop\( ]]; then
      definitions=$((definitions + 1))
      continue
    fi
    case "$path" in
      */tests/*|*/test_*) continue ;;
    esac
    call_count=$((call_count + 1))
    if [[ -z "$calls" ]]; then
      calls="${path}:${lineno}"
    fi
  done <<< "$hits"

  if [[ "$call_count" -gt 0 ]]; then
    record PASS C4 "ASR 成功终态驱动者" "production_calls=$call_count first=$calls definitions=$definitions"
    return
  fi

  record FAIL C4 "ASR 成功终态驱动者" "definition_only definitions=$definitions production_calls=0"
}

check_c5() {
  local missing="" tmp hdr body rc code timing elapsed text_len

  if [[ "$ALLOW_WRITE" -ne 1 ]]; then
    record SKIP C5 "公开 ASR 真实 E2E" "需要 --allow-write（会写库，最长 130s）"
    return
  fi
  [[ -n "${SN_API_BASE:-}" ]] || missing="${missing} SN_API_BASE"
  [[ -n "${SN_SAMPLE_WAV:-}" ]] || missing="${missing} SN_SAMPLE_WAV"
  [[ -n "${SN_ASR_MODEL:-}" ]] || missing="${missing} SN_ASR_MODEL"
  if [[ -n "$missing" ]]; then
    record SKIP C5 "公开 ASR 真实 E2E" "缺少${missing}${ENV_LOAD_NOTE:+；${ENV_LOAD_NOTE}}"
    return
  fi
  if [[ ! -r "$SN_SAMPLE_WAV" ]]; then
    record SKIP C5 "公开 ASR 真实 E2E" "样本不可读: $SN_SAMPLE_WAV"
    return
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    record SKIP C5 "公开 ASR 真实 E2E" "curl 或 jq 不可用"
    return
  fi
  if ! ensure_tmp; then
    record SKIP C5 "公开 ASR 真实 E2E" "无法创建临时目录"
    return
  fi

  tmp="$PREFLIGHT_TMP"
  hdr="$tmp/asr.headers"
  body="$tmp/asr.json"
  code=$(curl --max-time 130 -sS -D "$hdr" -o "$body" -w '%{http_code} %{time_total}' \
    -F "file=@${SN_SAMPLE_WAV};type=audio/wav" \
    -F "model=${SN_ASR_MODEL}" \
    -F "response_format=json" \
    "${SN_API_BASE%/}/v1/audio/transcriptions" 2>"$tmp/asr.curl.err")
  rc=$?
  timing="${code#* }"
  code="${code%% *}"
  if [[ $rc -ne 0 ]]; then
    case "$rc" in
      7) record SKIP C5 "公开 ASR 真实 E2E" "$SN_API_BASE 未监听（curl exit 7）" ;;
      28) record FAIL C5 "公开 ASR 真实 E2E" "请求超时（curl exit 28）" ;;
      *) record SKIP C5 "公开 ASR 真实 E2E" "curl exit $rc" ;;
    esac
    return
  fi
  elapsed="${timing:-?}"
  if [[ "$code" != "200" ]]; then
    record FAIL C5 "公开 ASR 真实 E2E" "http=$code expected=200 elapsed=${elapsed}s"
    return
  fi
  if ! jq -e '.text | type=="string" and length>0' "$body" >/dev/null 2>&1; then
    record FAIL C5 "公开 ASR 真实 E2E" "http=200 但响应 .text 不是非空字符串"
    return
  fi

  text_len=$(jq -r '.text|length' "$body" 2>/dev/null)
  record PASS C5 "公开 ASR 真实 E2E" "http=200 text_chars=$text_len elapsed=${elapsed}s"
}

check_c6() {
  local missing="" tmp body headers rc code timing elapsed bytes file_desc payload

  if [[ "$ALLOW_WRITE" -ne 1 ]]; then
    record SKIP C6 "公开 TTS 真实 E2E" "需要 --allow-write（会写库/占用会话，最长 60s）"
    return
  fi
  [[ -n "${SN_API_BASE:-}" ]] || missing="${missing} SN_API_BASE"
  [[ -n "${SN_TTS_MODEL:-}" ]] || missing="${missing} SN_TTS_MODEL"
  [[ -n "${SN_TTS_VOICE:-}" ]] || missing="${missing} SN_TTS_VOICE"
  if [[ -n "$missing" ]]; then
    record SKIP C6 "公开 TTS 真实 E2E" "缺少${missing}${ENV_LOAD_NOTE:+；${ENV_LOAD_NOTE}}"
    return
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    record SKIP C6 "公开 TTS 真实 E2E" "curl 或 jq 不可用"
    return
  fi
  if ! command -v file >/dev/null 2>&1; then
    record SKIP C6 "公开 TTS 真实 E2E" "file 命令不可用"
    return
  fi
  if ! ensure_tmp; then
    record SKIP C6 "公开 TTS 真实 E2E" "无法创建临时目录"
    return
  fi

  tmp="$PREFLIGHT_TMP"
  body="$tmp/tts.wav"
  headers="$tmp/tts.headers"
  payload=$(jq -n \
    --arg model "$SN_TTS_MODEL" \
    --arg input "SpeechNexus release check." \
    --arg voice "$SN_TTS_VOICE" \
    '{model:$model,input:$input,voice:$voice,response_format:"wav"}') || {
    record SKIP C6 "公开 TTS 真实 E2E" "无法构造请求 JSON"
    return
  }

  code=$(curl --max-time 60 -sS -D "$headers" -o "$body" -w '%{http_code} %{time_total}' \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "${SN_API_BASE%/}/v1/audio/speech" 2>"$tmp/tts.curl.err")
  rc=$?
  timing="${code#* }"
  code="${code%% *}"
  if [[ $rc -ne 0 ]]; then
    case "$rc" in
      7) record SKIP C6 "公开 TTS 真实 E2E" "$SN_API_BASE 未监听（curl exit 7）" ;;
      28) record FAIL C6 "公开 TTS 真实 E2E" "请求超时（curl exit 28）" ;;
      *) record SKIP C6 "公开 TTS 真实 E2E" "curl exit $rc" ;;
    esac
    return
  fi
  elapsed="${timing:-?}"
  if [[ "$code" != "200" ]]; then
    record FAIL C6 "公开 TTS 真实 E2E" "http=$code expected=200 elapsed=${elapsed}s"
    return
  fi
  if [[ ! -f "$body" ]]; then
    record FAIL C6 "公开 TTS 真实 E2E" "http=200 但没有响应文件"
    return
  fi
  bytes=$(wc -c < "$body" | tr -d '[:space:]')
  if [[ ! "$bytes" =~ ^[0-9]+$ ]] || [[ "$bytes" -le 44 ]]; then
    record FAIL C6 "公开 TTS 真实 E2E" "http=200 bytes=${bytes:-?} expected>44"
    return
  fi
  file_desc=$(file -b "$body" 2>/dev/null || true)
  if ! printf '%s' "$file_desc" | grep -qiE 'WAVE|WAV'; then
    record FAIL C6 "公开 TTS 真实 E2E" "http=200 bytes=$bytes file=${file_desc:-unknown} expected=WAV"
    return
  fi

  record PASS C6 "公开 TTS 真实 E2E" "http=200 bytes=$bytes file=${file_desc} elapsed=${elapsed}s"
}

check_c7() {
  local report="$SN_QUALIFICATION_REPORT"
  local tier status required_passed required_total required_ok

  if ! command -v jq >/dev/null 2>&1; then
    record SKIP C7 "生产资格报告" "jq 不可用"
    return
  fi
  if [[ ! -r "$report" ]]; then
    record SKIP C7 "生产资格报告" "报告不可读: $report"
    return
  fi

  tier=$(jq -r '.release_tier // "?"' "$report" 2>/dev/null)
  status=$(jq -r '.overall_status // "?"' "$report" 2>/dev/null)
  required_passed=$(jq -r '(.required_scenarios_passed // false)|tostring' "$report" 2>/dev/null)
  required_total=$(jq -r '[.scenarios[]? | select(.required==true)] | length' "$report" 2>/dev/null)
  required_ok=$(jq -r '[.scenarios[]? | select(.required==true and .status=="passed")] | length' "$report" 2>/dev/null)

  if ! jq -e '.release_tier=="production" and .overall_status=="pass" and .required_scenarios_passed==true and ([.scenarios[]? | select(.required==true) | .status] | length>0 and all(.=="passed"))' "$report" >/dev/null 2>&1; then
    record FAIL C7 "生产资格报告" "tier=${tier:-?} status=${status:-?} required_passed=${required_passed:-?} required=${required_ok:-?}/${required_total:-?} expected=production/pass/all-required-passed"
    return
  fi

  record PASS C7 "生产资格报告" "tier=$tier status=$status required_passed=$required_passed required=$required_ok/$required_total"
}

VERIFY_RESULT=""
VERIFY_RC=0
VERIFY_TAIL=""

run_evidence_verifier() {
  local binding="$1"
  local output=""
  local rc=0

  output=$(UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.paperclip-runtime/cache/uv}" \
    uv run --no-project \
      --with 'jsonschema>=4.23,<5' \
      --with 'rfc8785>=0.1.4,<0.2' \
      python scripts/dev-env/verify-release-evidence.py \
      --manifest deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json \
      --report deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json \
      --evidence deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json \
      --expect-manifest-id capm-file-asr-qwen3-1p7b-gb10-20261027 \
      --expect-report-id qrep-file-asr-qwen3-1p7b-gb10-20261027 \
      --artifact-pin deploy/engines/file-asr-audio/artifact-pin.json \
      --binding-definition "$binding" \
      --database-snapshot tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json \
      --require-published --require-digests --show-digests 2>&1)
  rc=$?
  VERIFY_RESULT=$(printf '%s\n' "$output" | sed -n 's/^RESULT //p' | tail -n 1)
  VERIFY_RC=$rc
  if [[ -z "$VERIFY_RESULT" ]]; then
    VERIFY_TAIL=$(printf '%s\n' "$output" | tail -n 1 | tr '\n' ' ')
  else
    VERIFY_TAIL=""
  fi
}

check_c8() {
  local missing=""
  local cand_result cand_rc cand_tail shipped_result shipped_rc shipped_tail
  local inputs=(
    deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json
    deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json
    deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json
    deploy/engines/file-asr-audio/artifact-pin.json
    deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json
    deploy/engines/file-asr-audio/binding-definition.json
    tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json
  )
  local input

  if ! command -v uv >/dev/null 2>&1; then
    record SKIP C8 "证据校验" "uv 不可用"
    return
  fi
  for input in "${inputs[@]}"; do
    [[ -r "$input" ]] || missing="${missing} $input"
  done
  if [[ -n "$missing" ]]; then
    record SKIP C8 "证据校验" "输入不可读:${missing}"
    return
  fi

  run_evidence_verifier deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json
  cand_result="$VERIFY_RESULT"
  cand_rc="$VERIFY_RC"
  cand_tail="$VERIFY_TAIL"

  run_evidence_verifier deploy/engines/file-asr-audio/binding-definition.json
  shipped_result="$VERIFY_RESULT"
  shipped_rc="$VERIFY_RC"
  shipped_tail="$VERIFY_TAIL"

  if [[ -z "$cand_result" && -z "$shipped_result" ]]; then
    record SKIP C8 "证据校验" "verifier 未产生 RESULT（candidate rc=$cand_rc ${cand_tail:-}; shipped rc=$shipped_rc ${shipped_tail:-}）"
    return
  fi

  cand_result="${cand_result:-no-RESULT}"
  shipped_result="${shipped_result:-no-RESULT}"
  if [[ "$cand_result" == "PASS" && "$cand_rc" -eq 0 && "$shipped_result" == "PASS" && "$shipped_rc" -eq 0 ]]; then
    record PASS C8 "证据校验" "candidate=PASS shipped=PASS"
    return
  fi

  record FAIL C8 "证据校验" "candidate=${cand_result}(rc=$cand_rc) shipped=${shipped_result}(rc=$shipped_rc) expected=both RESULT PASS"
}

COMPOSE_STYLE=""

run_compose() {
  if [[ "$COMPOSE_STYLE" == "plugin" ]]; then
    POSTGRES_PASSWORD=dummy MINIO_ROOT_PASSWORD=dummy docker compose "$@"
  else
    POSTGRES_PASSWORD=dummy MINIO_ROOT_PASSWORD=dummy docker-compose "$@"
  fi
}

check_c9() {
  local services config service_count missing="" private_missing="" svc
  local required="api gateway node-controller runtime-delivery-worker job-dispatch-worker asset-maintain"

  if ! command -v docker >/dev/null 2>&1; then
    record SKIP C9 "可部署服务集合" "docker 不可用"
    return
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_STYLE="plugin"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_STYLE="standalone"
  else
    record SKIP C9 "可部署服务集合" "docker compose / docker-compose 不可用"
    return
  fi

  if ! services=$(run_compose -f deploy/compose.yaml config --services 2>/dev/null); then
    record FAIL C9 "可部署服务集合" "docker compose config --services 失败"
    return
  fi
  if ! config=$(run_compose -f deploy/compose.yaml config 2>/dev/null); then
    record FAIL C9 "可部署服务集合" "docker compose config 失败"
    return
  fi

  service_count=$(printf '%s\n' "$services" | sed '/^$/d' | wc -l | tr -d '[:space:]')
  for svc in $required; do
    if ! printf '%s\n' "$services" | grep -qx -- "$svc"; then
      missing="${missing} ${svc}"
    fi
  done
  if ! printf '%s' "$config" | grep -q 'SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE'; then
    private_missing="${private_missing} file-asr"
  fi
  if ! printf '%s' "$config" | grep -q 'SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE'; then
    private_missing="${private_missing} file-tts"
  fi

  if [[ -n "$missing" || -n "$private_missing" ]]; then
    missing=${missing# }
    private_missing=${private_missing# }
    record FAIL C9 "可部署服务集合" "services=$service_count missing=${missing:-none} gateway_private=missing:${private_missing:-none}"
    return
  fi

  record PASS C9 "可部署服务集合" "services=$service_count required=6 gateway_private=present"
}

check_c10() {
  local help_out rc retire activate rollback head_opt

  if [[ -x "$ROOT/.venv/bin/speechnexus-publish-binding" ]]; then
    help_out=$("$ROOT/.venv/bin/speechnexus-publish-binding" --help 2>&1)
    rc=$?
  elif command -v uv >/dev/null 2>&1; then
    help_out=$(uv run --no-sync --package speechnexus-backend speechnexus-publish-binding --help 2>&1)
    rc=$?
  else
    record SKIP C10 "发布生命周期能力" "speechnexus-publish-binding 与 uv 均不可用"
    return
  fi

  if [[ $rc -ne 0 ]]; then
    record SKIP C10 "发布生命周期能力" "无法读取 publish --help（rc=$rc: $(printf '%s\n' "$help_out" | tail -n 1)）"
    return
  fi

  retire=no
  activate=no
  rollback=no
  head_opt=no
  printf '%s' "$help_out" | grep -q -- '--retire' && retire=yes
  printf '%s' "$help_out" | grep -q -- '--activate' && activate=yes
  printf '%s' "$help_out" | grep -q -- '--rollback' && rollback=yes
  printf '%s' "$help_out" | grep -q -- '--head' && head_opt=yes

  if [[ "$retire" == "yes" && "$activate" == "yes" && "$rollback" == "yes" ]]; then
    record PASS C10 "发布生命周期能力" "retire=yes activate=yes rollback=yes head=$head_opt"
    return
  fi

  record FAIL C10 "发布生命周期能力" "retire=$retire activate=$activate rollback=$rollback head=$head_opt expected=all-three"
}

check_c11() {
  local matches rc line_count file_count first_file

  if command -v rg >/dev/null 2>&1; then
    matches=$(rg -n 'reconciliation_required' docs/operations 2>/dev/null)
    rc=$?
    if [[ $rc -gt 1 ]]; then
      record SKIP C11 "对账恢复流程" "rg 执行失败（exit $rc）"
      return
    fi
  elif command -v grep >/dev/null 2>&1; then
    matches=$(grep -R -n 'reconciliation_required' docs/operations 2>/dev/null || true)
  else
    record SKIP C11 "对账恢复流程" "rg/grep 不可用"
    return
  fi

  if [[ -z "$matches" ]]; then
    record FAIL C11 "对账恢复流程" "docs/operations 中没有对账恢复 runbook（matches=0）"
    return
  fi

  line_count=$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d '[:space:]')
  file_count=$(printf '%s\n' "$matches" | sed '/^$/d' | cut -d: -f1 | sort -u | wc -l | tr -d '[:space:]')
  first_file=$(printf '%s\n' "$matches" | sed '/^$/d' | cut -d: -f1 | sort -u | head -n 1)
  record PASS C11 "对账恢复流程" "matches=$line_count files=$file_count first=$first_file"
}

MODE="read-only"
[[ "$ALLOW_WRITE" -eq 1 ]] && MODE="write-allowed"
printf 'release-preflight root=%s mode=%s\n' "$ROOT" "$MODE"

selected C0 && check_c0
selected C1 && check_c1
selected C2 && check_c2
selected C3 && check_c3
selected C4 && check_c4
selected C5 && check_c5
selected C6 && check_c6
selected C7 && check_c7
selected C8 && check_c8
selected C9 && check_c9
selected C10 && check_c10
selected C11 && check_c11
if selected C12; then
  record SKIP C12 "最终独立验收" "由独立验收人/agent 在最终候选上判定，脚本不代替"
fi

SELECTED_COUNT=0
for check_id in $ALL_IDS; do
  if selected "$check_id"; then
    SELECTED_COUNT=$((SELECTED_COUNT + 1))
  fi
done

printf 'SUMMARY selected=%d pass=%d fail=%d skip=%d\n' "$SELECTED_COUNT" "$PASS_COUNT" "$FAIL_COUNT" "$SKIP_COUNT"
if [[ -n "$FAILED_IDS" ]]; then
  printf 'FAILED%s\n' "$FAILED_IDS"
fi
if [[ -n "$SKIPPED_IDS" ]]; then
  printf 'SKIPPED%s\n' "$SKIPPED_IDS"
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  printf 'RESULT FAIL\n'
  exit 1
fi

printf 'RESULT PASS\n'
exit 0
