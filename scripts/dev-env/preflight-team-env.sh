#!/usr/bin/env bash

# Team execution preflight: proves the Agent fleet can actually start work.
#
# Run this before declaring a blocker "external" and before dispatching a batch.
# It checks the four things that silently stopped the team in October 2026:
#   1. a stranded deferred wake holding an agent's wake slot;
#   2. an agent left paused and reported as blocked;
#   3. a remote execution channel that no longer routes;
#   4. test fixtures (PostgreSQL / MinIO) that look configured but cannot run.
#
# Exit codes: 0 all checks passed, 1 a check failed, 2 configuration missing.
#
# Every check prints PASS/FAIL plus the evidence line that produced it, so a
# failure here is directly actionable and never requires re-reading logs.

set -uo pipefail

PAPERCLIP_DSN="${PAPERCLIP_DSN:-postgresql://delegra_app@127.0.0.1:5432/delegra}"
COMPANY_ID="${PAPERCLIP_COMPANY_ID:-}"
FAILURES=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
info() { printf '      %s\n' "$1"; }
skip() { printf 'SKIP  %s\n' "$1"; }

psql_ro() { psql "$PAPERCLIP_DSN" -tAc "$1" 2>/dev/null; }

if ! command -v psql >/dev/null 2>&1; then
  echo "configuration: psql not found; install PostgreSQL client tools" >&2
  exit 2
fi

if ! psql_ro 'select 1' >/dev/null; then
  echo "configuration: cannot reach Paperclip database at $PAPERCLIP_DSN" >&2
  exit 2
fi

echo "== Paperclip wake queue =="
# A deferred wake whose issue is gone can never be delivered. It keeps a
# 30-second self-heal loop running and occupies the agent's wake slot, which
# looks exactly like "the platform will not schedule this agent".
stranded="$(psql_ro "select coalesce(string_agg(id::text, ' '), '')
  from agent_wakeup_requests w
  where w.status = 'deferred_issue_execution'
    and not exists (
      select 1 from issues i
      where i.id::text = w.payload->>'issueId'
        and i.status <> 'cancelled'
    )")"
if [[ -z "$stranded" ]]; then
  pass "no stranded deferred wake"
else
  fail "stranded deferred wake holding a wake slot"
  for wake in $stranded; do
    info "$wake → resolve with: update agent_wakeup_requests set status='skipped', reason='issue_cancelled_superseded', updated_at=now() where id='$wake'"
  done
  info "see docs/operations/paperclip.md; deleting child rows does not help"
fi

echo
echo "== Agent admission state =="
# A deliberately disabled agent is fine. A paused agent holding actionable
# work is not: callers report it as "blocked" and nothing will ever move it.
blocked_work="$(psql_ro "select coalesce(string_agg(a.name || ' -> ' || i.identifier, ', '), '')
  from agents a
  join issues i on i.assignee_agent_id = a.id
  where a.pause_reason is not null
    and i.status in ('todo', 'in_progress', 'in_review')")"
if [[ -z "$blocked_work" ]]; then
  pass "no paused agent holds actionable work"
else
  fail "paused agent holds actionable work: $blocked_work"
  info "a paused agent is reported as 'blocked' by callers; clear pause_reason first"
fi

disabled="$(psql_ro "select coalesce(string_agg(name || ' (' || coalesce(pause_reason, 'paused') || ')', ', '), '')
  from agents where pause_reason is not null")"
if [[ -n "$disabled" ]]; then
  info "disabled agents: $disabled"
fi

# Accounting that just started is normal. Only a recovery still open minutes
# after the run finished means new work is genuinely gated.
recovering="$(psql_ro "select coalesce(string_agg(left(r.id::text, 8) || ' (' || f.phase || ', finished ' || to_char(r.finished_at, 'HH24:MI') || ')', ', '), '')
  from heartbeat_runs r
  join native_run_finalizations f on f.run_id = r.id
  where r.cost_accounting_pending and r.runtime_mode = 'native'
    and r.status in ('succeeded','failed','timed_out','cancelled','interrupted')
    and f.result_id is null and f.phase <> 'terminal_failure'
    and coalesce(r.finished_at, r.created_at) < now() - interval '5 minutes'")"
if [[ -z "$recovering" ]]; then
  pass "no run stuck awaiting accounting"
else
  fail "runs stuck awaiting accounting: $recovering"
  info "new work stays blocked while a native recovery is open past 5 minutes"
fi

echo
echo "== Remote execution channel =="
if [[ -n "${TEAM_REMOTE_HOST:-}" ]]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$TEAM_REMOTE_HOST" true 2>/dev/null; then
    pass "ssh to $TEAM_REMOTE_HOST"
  else
    fail "ssh to $TEAM_REMOTE_HOST"
  fi
  # A remote home directory on a local filesystem is the signature of a run
  # that was routed locally while configured for the remote host.
  if [[ -n "${TEAM_REMOTE_HOME:-}" && -e "$TEAM_REMOTE_HOME" && ! -d "$TEAM_REMOTE_HOME" ]]; then
    fail "remote home $TEAM_REMOTE_HOME resolves on this host; runs will route locally"
  fi
else
  skip "set TEAM_REMOTE_HOST to check the remote execution channel"
fi

echo
echo "== Test fixtures =="
if [[ -n "${SPEECHNEXUS_TEST_DATABASE_URL:-}" ]]; then
  if psql "$SPEECHNEXUS_TEST_DATABASE_URL" -tAc 'select 1' >/dev/null 2>&1; then
    pass "isolated PostgreSQL reachable"
  else
    fail "SPEECHNEXUS_TEST_DATABASE_URL is set but not reachable"
  fi
else
  skip "SPEECHNEXUS_TEST_DATABASE_URL not set; real-database tests will skip"
fi

if [[ -n "${SPEECHNEXUS_TEST_MINIO_ENDPOINT:-}" ]]; then
  case "$SPEECHNEXUS_TEST_MINIO_ENDPOINT" in
    http://*|https://*) pass "MinIO endpoint is a URL" ;;
    *) fail "SPEECHNEXUS_TEST_MINIO_ENDPOINT must include http:// (pydantic rejects a bare host:port)" ;;
  esac
else
  skip "SPEECHNEXUS_TEST_MINIO_ENDPOINT not set; storage tests will skip"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "preflight: all checks passed"
  exit 0
fi
echo "preflight: $FAILURES check(s) failed"
exit 1
