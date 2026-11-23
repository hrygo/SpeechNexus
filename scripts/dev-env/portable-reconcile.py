#!/usr/bin/env python3
"""切换前的对账:草案表与 canonical 表在**同一批 job_id** 上必须逐字段一致.

为什么需要它:搬迁「跑完了」和搬迁「对了」是两件事。换算函数与搬迁脚本都可能对,但只处理了
一部分行(分页截断、并发新增、失败重试)。这类缺口在切换后表现为「某些任务永远不被认领」或
「到期时间差 8 小时」,而不是任何异常。

对账口径(每条都与 canonical 的定义点一致,不另立规则):

| 字段 | 期望 |
| --- | --- |
| `bucket_no` | `bucket_for(job_id)` |
| `claim_expires_at` | 草案 `0` → `NULL`;否则 epoch 微秒 → UTC(naive) |
| `due_at` | epoch 微秒 → UTC(naive) |
| `last_event_seq` | canonical `MAX(job_events.event_seq)`,无事件时 0 |
| `request_digest` / `job_state` / `claim_owner_id` | 直传 |

退出码:0 完全一致;1 有差异或查询失败。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg

from speechnexus_portable_sql.claims import bucket_for

META_JOB_COLUMNS = (
    "job_id",
    "operation_id",
    "attempt_id",
    "request_digest",
    "job_state",
    "claim_owner_id",
    "claim_expires_at_us",
    "claim_version",
    "due_at_us",
    "last_event_seq",
)


def _expected_time(value: object) -> datetime | None:
    microseconds = int(Decimal(str(value)))
    if microseconds <= 0:
        return None
    return datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC).replace(tzinfo=None)


def mismatches(connection: psycopg.Connection[Any], *, draft_table: str, limit: int) -> list[str]:
    """逐字段比较;返回人能直接照着修的描述,而不是计数。"""

    cursor = connection.execute(
        "SELECT job_id, operation_id, attempt_id, request_digest, job_state, claim_owner_id, "
        "claim_expires_at_us, claim_version, due_at_us, last_event_seq FROM "
        + draft_table
        + " ORDER BY job_id"
    )
    found: list[str] = []
    for row in cursor.fetchall():
        job_id = str(row[0])
        canonical = connection.execute(
            "SELECT operation_id, attempt_id, request_digest, bucket_no, due_at, job_state, "
            "claim_owner_id, claim_expires_at, claim_version, last_event_seq "
            "FROM coordination_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        if canonical is None:
            found.append(f"{job_id}: missing from coordination_jobs")
            if len(found) >= limit:
                return found
            continue
        head = connection.execute(
            "SELECT COALESCE(MAX(event_seq), 0) FROM job_events WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert head is not None
        checks = [
            ("operation_id", row[1], canonical[0]),
            ("attempt_id", row[2], canonical[1]),
            ("request_digest", row[3], canonical[2]),
            ("bucket_no", bucket_for(job_id), Decimal(str(canonical[3]))),
            ("due_at", _expected_time(row[8]), canonical[4]),
            ("job_state", row[4], canonical[5]),
            ("claim_owner_id", row[5], canonical[6]),
            ("claim_expires_at", _expected_time(row[6]), canonical[7]),
            ("last_event_seq", Decimal(str(head[0])), Decimal(str(canonical[9]))),
        ]
        for name, expected, actual in checks:
            if expected != actual:
                found.append(f"{job_id}: {name} expected {expected!r}, found {actual!r}")
                if len(found) >= limit:
                    return found
        # `claim_version` 是 fencing 计数,只增不减:搬迁把 head 推进时会让它 +1,这恰恰是
        # 作废旧系统在飞认领的手段。所以这里比的是「不小于草案」,而不是相等。
        draft_version = Decimal(str(row[7]))
        canonical_version = Decimal(str(canonical[8]))
        if canonical_version < draft_version:
            found.append(
                f"{job_id}: claim_version went backwards "
                f"(draft {draft_version}, canonical {canonical_version})"
            )
            if len(found) >= limit:
                return found
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="owner DSN; the check is read-only")
    parser.add_argument("--schema", default=None, help="schema holding both tables")
    parser.add_argument("--draft-table", default="draft_jobs", help="draft-shaped job table")
    parser.add_argument("--limit", type=int, default=20, help="stop after this many mismatches")
    args = parser.parse_args(argv)

    with psycopg.connect(args.dsn, autocommit=True) as connection:
        if args.schema:
            connection.execute(f'SET search_path TO "{args.schema}", pg_catalog')
        try:
            found = mismatches(connection, draft_table=args.draft_table, limit=max(1, args.limit))
        except psycopg.Error as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
    if found:
        for line in found:
            print(f"MISMATCH: {line}", file=sys.stderr)
        print(f"FAIL: {len(found)} mismatch(es); switchover must not proceed", file=sys.stderr)
        return 1
    print("OK: draft and canonical agree on every field")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
