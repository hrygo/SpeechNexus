#!/usr/bin/env bash
# 军团状态面板：一条命令看清「谁在干、谁闲着、谁被卡住、哪些等我裁决」。
#
# 读-only。只查 Paperclip 本地库，不写任何表、不碰远端。
#
# 用法：
#   scripts/dev-env/fleet-status.sh                # 打一次
#   scripts/dev-env/fleet-status.sh --watch 60     # 每 60 秒重打
#   scripts/dev-env/fleet-status.sh --json         # 给脚本消费
#   scripts/dev-env/fleet-status.sh --db <dsn>     # 覆盖连接串
#
# 退出码：0 = 无可执行事项；1 = 有（空闲产能 / 唤醒被卡 / 超时未动 / 等我裁决）。
#
# 设计要点（为什么查这些）：
# - 「空闲 + 有 todo 卡」= 唤醒失败，不是没活干。这是我踩过两次的坑。
# - 「空闲 + 无卡」= 可派活，这是产能。
# - 「in_review 很久没动」= 等我裁决，这是瓶颈在我。
# - 「running 很久」= 可能卡死，需要打断重排。
set -uo pipefail

DB="${PAPERCLIP_DATABASE_URL:-postgresql://delegra_app@127.0.0.1:5432/delegra}"
MODE="text"
WATCH=""

# 阈值（分钟）
RUNNING_SLOW=25
REVIEW_WAIT=15
TODO_IDLE=8

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) MODE="json"; shift ;;
    --watch) WATCH="${2:?--watch needs seconds}"; shift 2 ;;
    --db) DB="${2:?--db needs a DSN}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! command -v psql >/dev/null 2>&1; then
  echo "fleet-status: psql not found" >&2
  exit 2
fi
if ! psql "$DB" -tAc 'SELECT 1' >/dev/null 2>&1; then
  echo "fleet-status: cannot reach $DB" >&2
  exit 2
fi

# 所有查询都用 CTE + 只读函数；不产生写入。
read_all() {
  psql "$DB" -tAF'|' -c "
WITH
run AS (
  SELECT DISTINCT ON (r.agent_id)
         r.agent_id,
         (SELECT i.identifier FROM issues i WHERE i.id = r.issue_id) AS ident,
         r.status,
         r.created_at,
         r.updated_at
  FROM heartbeat_runs r
  WHERE r.status IN ('running','queued')
  ORDER BY r.agent_id, r.created_at DESC
),
open_card AS (
  SELECT a.id AS agent_id,
         count(*) FILTER (WHERE i.status IN ('todo','in_progress','backlog')) AS open_cards,
         count(*) FILTER (WHERE i.status = 'in_review')                      AS review_cards,
         min(i.updated_at) FILTER (WHERE i.status IN ('todo','in_progress'))  AS oldest_open,
         string_agg(i.identifier || ':' || i.status, ',' ORDER BY i.identifier)
           FILTER (WHERE i.status NOT IN ('done','cancelled'))               AS cards
  FROM agents a
  LEFT JOIN issues i ON i.assignee_agent_id = a.id
  GROUP BY a.id
),
bad_wake AS (
  SELECT agent_id,
         string_agg(DISTINCT status, '+') AS status,
         string_agg(DISTINCT reason, '+') AS reason,
         sum(n)                           AS n
  FROM (
    SELECT w.agent_id, w.status, w.reason, count(*) AS n
    FROM agent_wakeup_requests w
    WHERE w.status IN ('deferred_issue_execution','failed')
      AND w.created_at > now() - interval '3 hours'
    GROUP BY w.agent_id, w.status, w.reason
  ) per_reason
  GROUP BY agent_id
),
stuck_recovery AS (
  SELECT ra.source_issue_id, count(*) AS n
  FROM issue_recovery_actions ra
  WHERE ra.status = 'active'
  GROUP BY ra.source_issue_id
)
SELECT
  a.name,
  a.status,
  coalesce(r.ident, '-')                                   AS running_card,
  coalesce(r.status, '-')                                  AS run_state,
  coalesce(round(extract(epoch FROM (now() - r.created_at)) / 60), -1)::int AS running_min,
  coalesce(c.open_cards, 0)                                AS open_cards,
  coalesce(c.review_cards, 0)                              AS review_cards,
  coalesce(c.cards, '-')                                   AS cards,
  coalesce(round(extract(epoch FROM (now() - c.oldest_open)) / 60), -1)::int AS open_oldest_min,
  coalesce(bw.status, '-')                                 AS bad_wake_state,
  coalesce(bw.reason, '-')                                 AS bad_wake_reason,
  coalesce(bw.n, 0)                                        AS bad_wake_n
FROM agents a
LEFT JOIN run r            ON r.agent_id = a.id
LEFT JOIN open_card c      ON c.agent_id = a.id
LEFT JOIN bad_wake bw      ON bw.agent_id = a.id
WHERE a.status NOT IN ('archived','terminated')
ORDER BY (r.status IS NOT NULL) DESC, a.name;
"
}

render() {
  local rows="$1" now
  now=$(date '+%H:%M:%S')

  local busy=0 idle_with_card=0 idle_free=0 stuck=0 waiting_me=0 badwake=0

  echo "══ 军团状态 ${now} ══"
  printf '%-14s %-9s %-10s %-9s %-8s %s\n' "AGENT" "状态" "当前卡" "已跑" "开口" "卡详情"
  printf '%-14s %-9s %-10s %-9s %-8s %s\n' "---------------" "--------" "---------" "--------" "-------" "--------"

  while IFS='|' read -r name status rcard rstate rmin open_cards review_cards cards oldest badstate badreason badn; do
    [[ -n "$name" ]] || continue
    local flags=""
    local shown_run="-"
    if [[ "$rstate" == "running" || "$rstate" == "queued" ]]; then
      busy=$((busy + 1))
      shown_run="${rmin}m"
      if [[ "$rmin" -ge $RUNNING_SLOW ]]; then
        flags="${flags}超时·"
        stuck=$((stuck + 1))
      fi
    else
      shown_run="—"
      if [[ "$open_cards" -gt 0 ]]; then
        idle_with_card=$((idle_with_card + 1))
        flags="${flags}有活没动·"
        if [[ "$oldest" -ge $TODO_IDLE ]]; then flags="${flags}积压${oldest}m·"; fi
      elif [[ "$status" == "paused" ]]; then
        flags="${flags}已暂停(需人工恢复)·"
      else
        idle_free=$((idle_free + 1))
        flags="${flags}可派活·"
      fi
    fi
    if [[ "$badn" != "0" ]]; then
      badwake=$((badwake + 1))
      flags="${flags}唤醒异常(${badreason})×${badn}·"
    fi
    if [[ "$review_cards" -gt 0 ]]; then waiting_me=$((waiting_me + review_cards)); fi
    flags="${flags%·}"
    printf '%-14s %-9s %-10s %-9s %-8s %s\n' \
      "$name" "$status" "${rcard}" "${shown_run}" "${open_cards}开/${review_cards}审" "${flags:-—}"
  done <<< "$rows"

  echo
  echo "── 汇总 ──"
  echo "  在跑 ${busy} · 空闲且有卡未动 ${idle_with_card} · 空闲可派活 ${idle_free}"
  echo "  超时未收工 ${stuck}（阈值 ${RUNNING_SLOW}m） · 等我裁决的 in_review ${waiting_me} · 唤醒异常 ${badwake}"

  local action=$((idle_with_card + stuck + badwake))
  echo
  if [[ "$action" -gt 0 || "$waiting_me" -gt 0 ]]; then
    echo "── 该我动手 ──"
    [[ "$idle_with_card" -gt 0 ]] && echo "  · 有 agent 空闲但卡没动：唤醒失败，换新卡或清 blocked_transition_at"
    [[ "$badwake" -gt 0 ]]        && echo "  · 唤醒被 deferred/failed：先看 reason，多为 decision_pending 或会话冲突"
    [[ "$stuck" -gt 0 ]]          && echo "  · 有 agent 跑太久：interrupt 后重排，别让它空占槽位"
    [[ "$waiting_me" -gt 0 ]]     && echo "  · in_review：这是瓶颈在我，先裁决再派新活"
    return 1
  fi
  echo "  无可执行事项。"
  return 0
}

json_out() {
  local rows="$1"
  python3 - "$rows" <<'PY'
import json, sys
rows = [r.split('|') for r in sys.argv[1].strip().splitlines() if r.strip()]
out = []
for r in rows:
    if len(r) < 12:
        continue
    out.append({
        "agent": r[0], "status": r[1], "running_card": r[2], "run_state": r[3],
        "running_min": int(r[4]), "open_cards": int(r[5]), "review_cards": int(r[6]),
        "cards": r[7], "open_oldest_min": int(r[8]),
        "bad_wake": {"state": r[9], "reason": r[10], "count": int(r[11])},
    })
print(json.dumps({"agents": out}, ensure_ascii=False, indent=2))
PY
}

loop() {
  local rows rc
  rows=$(read_all)
  if [[ "$MODE" == "json" ]]; then json_out "$rows"; return 0; fi
  render "$rows"; rc=$?
  return $rc
}

if [[ -n "$WATCH" ]]; then
  while true; do
    clear
    loop || true
    sleep "$WATCH"
  done
else
  loop
fi
