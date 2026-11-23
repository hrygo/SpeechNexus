#!/usr/bin/env python3
"""canonical schema 的 bootstrap 与检查:按版本顺序应用 `migrations/000*.sql`.

为什么需要一个专门的入口,而不是让部署脚本循环 `psql -f`:

1. **每个版本只在一个事务里生效**。文件里的建表、索引、权限与 `schema_meta` 播种要么一起
   可见,要么一起不存在;中途失败留下半个 revision,后面的检查全部失效。
2. **revision 是自己写进 `schema_meta` 的**,应用之后必须回读确认 `schema_rev` 等于文件名里
   的版本。文件被改过、被截断、或者播种语句被漏掉,都只会在这一步暴露。
3. **不回退、不跳版**。目标库已经停在更后的版本时,脚本拒绝而不是「尽力而为」——降级需要
   单独的回退演练,不能藏在一个 bootstrap 脚本里。

用法:

    portable-schema.py --dsn <url> [--schema <name>] [--check] [--dry-run]

退出码:0 已到位(或 dry-run 打印了计划);1 需要人工处理(版本不符、未 bootstrap、SQL 失败)。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
META_ID = "portable"

#: `INSERT INTO schema_meta ... VALUES ('portable', '<rev>', '<fingerprint>', <epoch>)`
_SEED = re.compile(
    r"INSERT\s+INTO\s+schema_meta\s*\([^)]*\)\s*VALUES\s*\(\s*'"
    + META_ID
    + r"'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*(\d+)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


class BootstrapError(RuntimeError):
    """需要人工处理的 bootstrap 状况。"""


@dataclass(frozen=True, slots=True)
class Revision:
    """一个迁移文件:版本号(取自文件名)、路径、它自己声明的指纹。"""

    revision: str
    path: Path
    fingerprint: str

    @property
    def statements(self) -> list[str]:
        source = self.path.read_text(encoding="utf-8")
        body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("--"))
        return [part.strip() for part in body.split(";") if part.strip()]


def _revision_of(path: Path) -> str:
    match = re.match(r"^(\d{4})_", path.name)
    if match is None:
        raise BootstrapError(f"{path.name} does not start with a four-digit revision")
    return match.group(1)


def load_revisions(directory: Path = MIGRATIONS) -> list[Revision]:
    """按版本号排序读出全部迁移,并核对文件自己声明的指纹与文件名。"""

    revisions: list[Revision] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        revision = _revision_of(path)
        seeds = _SEED.findall(path.read_text(encoding="utf-8"))
        if not seeds:
            raise BootstrapError(f"{path.name} does not seed schema_meta for {META_ID!r}")
        if len(seeds) != 1:
            raise BootstrapError(f"{path.name} seeds schema_meta {len(seeds)} times")
        _seeded_rev, fingerprint, _epoch = seeds[0]
        # 文件名与播种值是否一致,由应用之后的回读判定(单一定义点)。这里只要求「有且只有
        # 一次播种」,好让回读有东西可比。
        revisions.append(Revision(revision=revision, path=path, fingerprint=fingerprint))
    if not revisions:
        raise BootstrapError(f"no revisions found in {directory}")
    return revisions


def current_revision(connection: psycopg.Connection[Any]) -> tuple[str, str] | None:
    """读当前 revision 与指纹;表不存在或没有行时返回 None(未 bootstrap)。"""

    try:
        cursor = connection.execute(
            "SELECT schema_rev, schema_fingerprint FROM schema_meta WHERE meta_id = %s", (META_ID,)
        )
        row = cursor.fetchone()
    except psycopg.errors.UndefinedTable:
        connection.rollback()
        return None
    return (str(row[0]), str(row[1])) if row is not None else None


def plan(revisions: Sequence[Revision], current: tuple[str, str] | None) -> list[Revision]:
    """从当前 revision 之后开始应用;当前版本不存在于文件集里就拒绝(不猜)。"""

    if current is None:
        return list(revisions)
    known = [item.revision for item in revisions]
    if current[0] not in known:
        raise BootstrapError(
            f"database is at revision {current[0]}, which this tree does not provide "
            f"({', '.join(known)}); a downgrade or a missing file needs a human decision"
        )
    return [item for item in revisions if item.revision > current[0]]


def apply_revision(connection: psycopg.Connection[Any], revision: Revision) -> None:
    """一个文件一个事务:语句全成、回读 revision 一致,才算这一版就位。"""

    with connection.transaction():
        for statement in revision.statements:
            connection.execute(statement)
        row = connection.execute(
            "SELECT schema_rev, schema_fingerprint FROM schema_meta WHERE meta_id = %s",
            (META_ID,),
        ).fetchone()
        if row is None:
            raise BootstrapError(f"{revision.path.name} did not seed schema_meta")
        # 单一定义点:文件名声明的版本与指纹,必须等于应用之后 `schema_meta` 里的值。
        if str(row[0]) != revision.revision or str(row[1]) != revision.fingerprint:
            raise BootstrapError(
                f"{revision.path.name} claims ({revision.revision}, {revision.fingerprint[:12]}…) "
                f"but schema_meta holds ({row[0]}, {str(row[1])[:12]}…)"
            )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="owner DSN; runtime roles never bootstrap")
    parser.add_argument("--schema", default=None, help="apply inside this schema (search_path)")
    parser.add_argument(
        "--migrations",
        default=str(MIGRATIONS),
        help="directory holding the numbered migrations (default: this tree's migrations/)",
    )
    parser.add_argument("--check", action="store_true", help="verify only, change nothing")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)

    try:
        revisions = load_revisions(Path(args.migrations))
    except BootstrapError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    with psycopg.connect(args.dsn, autocommit=False) as connection:
        if args.schema:
            connection.execute(f'SET search_path TO "{args.schema}", pg_catalog')
            connection.commit()
        current = current_revision(connection)
        try:
            todo = plan(revisions, current)
        except BootstrapError as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
        present = current[0] if current else "unbootstrapped"
        if args.check:
            if todo:
                print(
                    f"FAIL: at {present}, expected {revisions[-1].revision}; "
                    f"missing {', '.join(item.revision for item in todo)}",
                    file=sys.stderr,
                )
                return 1
            print(f"OK: at {present}, fingerprint {current[1] if current else '-'}")
            return 0
        for item in todo:
            if args.dry_run:
                print(f"PLAN: apply {item.path.name}")
                continue
            try:
                apply_revision(connection, item)
            except (BootstrapError, psycopg.Error) as error:
                print(f"FAIL: {item.path.name}: {error}", file=sys.stderr)
                return 1
            print(f"OK: applied {item.path.name}")
        if not todo:
            print(f"OK: already at {present}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
