#!/usr/bin/env bash
# 军团状态面板：谁在跑、谁闲着、谁被卡住、哪些等我裁决。
#
# 读-only。只查 Paperclip 本地库，不写任何表、不碰远端。
#
# 用法：
#   scripts/dev-env/fleet-status.sh                # 打一次
#   scripts/dev-env/fleet-status.sh --watch 60     # 每 N 秒刷新（N>=30）
#   scripts/dev-env/fleet-status.sh --json         # 给脚本消费
#   scripts/dev-env/fleet-status.sh --company <id> # 多公司时必填
#
# 退出码：
#   0 = 无可执行事项
#   1 = 有事要动（有活没动 / 超时 / 状态可疑 / 唤醒异常 / 待我裁决）
#   2 = 面板不可用（缺 psql、连不上库、查询报错、多公司未指定）
#
# 设计原则（每条都来自一次真实误判）：
# - 查不到 ≠ 一切正常。SQL 报错必须 exit 2，绝不渲染成"无可执行事项"。
# - 「空闲」不等于「可派活」：paused / 状态陈旧 / 最近 run 失败 都要单独标，
#   否则停摆的 agent 会被当产能派活（"卡住被当空闲"）。
# - backlog 与 blocked 是停放，不算"该动没动"。
# - 唤醒异常只在「最近一次成功之后仍未恢复」才算；正常节流不算。
# - 只读：psql -X + default_transaction_read_only，避免 ~/.psqlrc 副作用。
set -uo pipefail

DB="${PAPERCLIP_DATABASE_URL:-postgresql://delegra_app@127.0.0.1:5432/delegra}"
MODE="text"
WATCH=""
COMPANY=""

RUNNING_SLOW=25   # 在跑超过该分钟数 → 超时
TODO_IDLE=8       # 在办卡超过该分钟数 → 积压
WAKE_WINDOW=3     # 只看近 N 小时的唤醒异常

fail() { echo "fleet-status: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) MODE="json"; shift ;;
    --watch) WATCH="${2:?--watch needs seconds}"; shift 2 ;;
    --db) DB="${2:?--db needs a DSN}"; shift 2 ;;
    --company) COMPANY="${2:?--company needs an id}"; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$WATCH" ]] && { ! [[ "$WATCH" =~ ^[0-9]+$ ]] || [[ "$WATCH" -lt 30 ]]; }; then
  fail "--watch needs an integer >= 30 (got '$WATCH')"
fi

command -v psql >/dev/null 2>&1 || fail "psql not found"

# -X 忽略 ~/.psqlrc(防副作用)；ON_ERROR_STOP 让 SQL 错立刻失败而不是静默空结果。
PSQL=(psql "$DB" -X -v ON_ERROR_STOP=1 -tAF'|')

if ! PGOPTIONS='-c default_transaction_read_only=on' "${PSQL[@]}" -c 'SELECT 1' >/dev/null 2>&1; then
  fail "cannot query database"
fi

if [[ -z "$COMPANY" ]]; then
  n=$("${PSQL[@]}" -c "SELECT count(DISTINCT company_id) FROM agents WHERE status NOT IN ('archived','terminated')" 2>/dev/null) || fail "cannot read agents"
  [[ "$n" -gt 1 ]] && fail "multiple companies present ($n); pass --company <id>"
  COMPANY=$("${PSQL[@]}" -c "SELECT company_id FROM agents WHERE status NOT IN ('archived','terminated') LIMIT 1" 2>/dev/null)
  if [[ -z "$COMPANY" ]]; then
    echo "══ 军团状态 $(date '+%H:%M:%S') ══"
    echo "  （该公司暂无 agent）"
    exit 0
  fi
fi

read_all() {
  "${PSQL[@]}" -c "
WITH
last_run AS (
  SELECT DISTINCT ON (r.agent_id) r.agent_id, r.status, r.created_at,
         (SELECT i.identifier FROM issues i WHERE i.id = r.issue_id) AS ident
  FROM heartbeat_runs r
  WHERE r.status IN ('running','queued')
  ORDER BY r.agent_id, r.created_at DESC
),
last_ok AS (
  SELECT agent_id, max(created_at) AS at
  FROM heartbeat_runs WHERE status = 'succeeded' GROUP BY agent_id
),
last_fail AS (
  SELECT agent_id, max(created_at) AS at
  FROM heartbeat_runs WHERE status IN ('failed','cancelled') GROUP BY agent_id
),
cards AS (
  SELECT a.id AS agent_id,
         count(*) FILTER (WHERE i.status IN ('todo','in_progress')) AS active_cards,
         count(*) FILTER (WHERE i.status = 'in_review')             AS review_cards,
         count(*) FILTER (WHERE i.status IN ('backlog','blocked'))   AS parked_cards,
         min(i.updated_at) FILTER (WHERE i.status IN ('todo','in_progress')) AS oldest_active,
         string_agg(i.identifier || ':' || i.status, ',' ORDER BY i.identifier)
           FILTER (WHERE i.status NOT IN ('done','cancelled'))      AS detail
  FROM agents a
  LEFT JOIN issues i ON i.assignee_agent_id = a.id
  GROUP BY a.id
),
wake AS (
  SELECT w.agent_id, string_agg(DISTINCT w.reason, '+') AS reason, count(*) AS n
  FROM agent_wakeup_requests w
  LEFT JOIN last_ok lo ON lo.agent_id = w.agent_id
  WHERE w.status IN ('deferred_issue_execution','failed')
    AND w.created_at > now() - make_interval(hours => $WAKE_WINDOW)
    AND w.created_at > coalesce(lo.at, to_timestamp(0))
  GROUP BY w.agent_id
)
SELECT a.name, a.status,
       coalesce(r.status, '-'),
       coalesce(r.ident, '-'),
       coalesce(round(extract(epoch FROM (now() - r.created_at)) / 60), -1)::int,
       coalesce(c.active_cards, 0),
       coalesce(c.review_cards, 0),
       coalesce(c.parked_cards, 0),
       coalesce(round(extract(epoch FROM (now() - c.oldest_active)) / 60), -1)::int,
       coalesce(c.detail, '-'),
       coalesce(w.reason, '-'),
       coalesce(w.n, 0),
       coalesce(round(extract(epoch FROM (now() - lf.at)) / 60), -1)::int
FROM agents a
LEFT JOIN last_run r   ON r.agent_id = a.id
LEFT JOIN last_fail lf ON lf.agent_id = a.id
LEFT JOIN cards c      ON c.agent_id = a.id
LEFT JOIN wake w       ON w.agent_id = a.id
WHERE a.company_id = '$COMPANY'::uuid
  AND a.status NOT IN ('archived','terminated')
ORDER BY (r.status IS NOT NULL) DESC, a.name;
" 2>&1
}

orphan_review() {
  "${PSQL[@]}" -c "SELECT count(*) FROM issues WHERE status='in_review' AND assignee_agent_id IS NULL" 2>/dev/null || echo 0
}

json_out() {
  local rows="$1"
  python3 - "$rows" "$COMPANY" "$MODE" <<'PY'
import json, sys
raw, company = sys.argv[1], sys.argv[2]
agents = []
for line in raw.strip().splitlines():
    p = line.split('|')
    if len(p) < 13:
        continue
    agents.append({
        "agent": p[0], "status": p[1], "run_state": p[2], "running_card": p[3],
        "running_min": int(p[4]), "active_cards": int(p[5]), "review_cards": int(p[6]),
        "parked_cards": int(p[7]), "active_oldest_min": int(p[8]), "cards": p[9],
        "bad_wake": {"reason": p[10], "count": int(p[11])},
        "last_fail_min": int(p[12]),
    })
print(json.dumps({"company": company, "agents": agents}, ensure_ascii=False, indent=2))
PY
}

render() {
  local rows="$1" orphan="$2" now
  now=$(date '+%H:%M:%S')
  local busy=0 queued=0 idle_free=0 idle_with_card=0 paused=0 suspicious=0 stuck=0 badwake=0 review=0
  local out="" line

  out+="══ 军团状态 ${now} ══"$'\n'
  printf -v line '%-14s %-9s %-10s %-8s %-13s %s' "AGENT" "状态" "当前卡" "已跑" "办/审/停" "判定"
  out+="$line"$'\n'

  while IFS='|' read -r name status rstate rcard rmin active review_c parked oldest detail wreason wn fail_min; do
    [[ -n "$name" ]] || continue
    local cls="" shown_run="—"
    if [[ "$status" == "paused" ]]; then
      cls="已暂停(需人工恢复)"; paused=$((paused + 1))
    elif [[ "$rstate" == "running" ]]; then
      shown_run="${rmin}m"; busy=$((busy + 1)); cls="在跑"
      if [[ "$rmin" -ge $RUNNING_SLOW ]]; then cls="在跑·超时"; stuck=$((stuck + 1)); fi
    elif [[ "$rstate" == "queued" ]]; then
      shown_run="排队"; queued=$((queued + 1)); cls="排队中"
    elif [[ "$status" != "idle" ]]; then
      cls="状态异常(${status})"; suspicious=$((suspicious + 1))
    elif [[ "$fail_min" -ge 0 && "$fail_min" -lt 60 && "$active" -eq 0 ]]; then
      cls="上次失败·可疑"; suspicious=$((suspicious + 1))
    elif [[ "$active" -gt 0 ]]; then
      cls="有活没动·积压${oldest}m"; idle_with_card=$((idle_with_card + 1))
    else
      cls="可派活"; idle_free=$((idle_free + 1))
    fi
    if [[ "$wn" != "0" ]]; then
      cls="${cls} | 唤醒异常(${wreason})×${wn}"; badwake=$((badwake + 1))
    fi
    review=$((review + review_c))
    printf -v line '%-14s %-9s %-10s %-8s %-13s %s' \
      "$name" "$status" "$rcard" "$shown_run" "${active}办/${review_c}审/${parked}停" "$cls"
    out+="$line"$'\n'
  done <<< "$rows"

  out+=$'\n'"── 汇总 ──"$'\n'
  out+="  在跑 ${busy} · 排队 ${queued} · 可派活 ${idle_free} · 有卡未动 ${idle_with_card}"$'\n'
  out+="  已暂停 ${paused} · 状态可疑 ${suspicious} · 超时 ${stuck}（阈值 ${RUNNING_SLOW}m）"$'\n'
  out+="  等裁决 in_review ${review}（另有 ${orphan} 张未指派） · 唤醒异常 ${badwake}"$'\n'
  out+=$'\n'

  local action=$((idle_with_card + stuck + suspicious + badwake))
  if [[ "$action" -gt 0 || "$review" -gt 0 || "$orphan" -gt 0 ]]; then
    out+="── 该我动手 ──"$'\n'
    [[ "$idle_with_card" -gt 0 ]] && out+="  · 有卡未动：多为唤醒失败（残留状态 / decision_pending / 会话冲突）→ 重排或换新卡"$'\n'
    [[ "$suspicious" -gt 0 ]]     && out+="  · 状态可疑：最近失败或状态与运行记录不一致 → 先看工作区有无产物"$'\n'
    [[ "$badwake" -gt 0 ]]        && out+="  · 唤醒异常：看 reason；恢复后仍出现才是真异常"$'\n'
    [[ "$stuck" -gt 0 ]]          && out+="  · 超时：先看工作区有无新改动，零改动才 interrupt"$'\n'
    [[ "$review" -gt 0 || "$orphan" -gt 0 ]] && out+="  · in_review：瓶颈在我，先裁决（未指派的尤其）"$'\n'
    printf '%s' "$out"
    return 1
  fi
  out+="  无可执行事项。"$'\n'
  printf '%s' "$out"
  return 0
}

run_once() {
  local rows rc orphan
  rows=$(read_all)
  if grep -qE '(^|: )(ERROR|FATAL):' <<<"$rows"; then
    echo "fleet-status: query error:" >&2
    grep -E '(^|: )(ERROR|FATAL):' <<<"$rows" >&2
    exit 2
  fi
  if [[ "$MODE" == "json" ]]; then
    orphan=$(orphan_review)
    json_out "$rows" "$orphan" >/dev/null 2>&1 || true
    json_out "$rows"
    # JSON 模式下也要有非零退出码，否则消费者永远看到"正常"
    local jaction
    jaction=$(python3 - "$rows" "$orphan" <<'PY'
import sys
rows = [l.split('|') for l in sys.argv[1].strip().splitlines() if l.strip()]
orphan = int(sys.argv[2] or 0)
bad = 0
for p in rows:
    if len(p) < 13:
        continue
    status, rstate, active, wn, fail_min = p[1], p[2], int(p[5]), int(p[11]), int(p[12])
    if status == 'paused' or status != 'idle' and rstate == '-':
        bad += 1
    elif rstate == 'running' and int(p[4]) >= 25:
        bad += 1
    elif rstate == '-' and active > 0:
        bad += 1
    elif rstate == '-' and 0 <= fail_min < 60 and active == 0:
        bad += 1
    if wn:
        bad += 1
print(bad + orphan + sum(int(p[6]) for p in rows if len(p) >= 13))
PY
)
    if [[ "${jaction:-0}" -gt 0 ]]; then return 1; fi
    return 0
  fi
  orphan=$(orphan_review)
  render "$rows" "$orphan"
  rc=$?
  return $rc
}

if [[ -n "$WATCH" ]]; then
  while true; do
    clear
    run_once || true
    sleep "$WATCH"
  done
else
  run_once
fi
