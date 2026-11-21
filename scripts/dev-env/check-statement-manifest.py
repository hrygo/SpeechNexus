#!/usr/bin/env python3
"""statement manifest guard:契约形状、profile 登记与摘要一致性.

判据(#1094 验收标准):

  shape       statement 的 `?` 个数必须等于声明参数个数,SQL 必须落在共同子集内
  authority   `authority.probe` 的执行 SQL 必须等于 `speechnexus_portable_sql.schema.AUTHORITY_SQL`
  profile     variant 的 profile 必须已登记,statement 必须存在,参数个数不得改变
  digest      canonical statement 摘要必须等于提交的 `manifest.digest`

摘要只覆盖 canonical statement(不含 variant):profile 增删不改变摘要,但任何 statement 的
参数、结果、NULL 形状、事务、锁序、幂等或错误语义变化都会改变它,所以「摘要没变」是有意义的断言。

只读:除 `--write-digest` 外不改任何文件.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SOURCE = REPO_ROOT / "packages/portable-sql/src"
STATEMENTS = REPO_ROOT / "packages/portable-sql/statements"

if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))

from speechnexus_portable_sql.budget import STATEMENTS as BUDGET_STATEMENTS  # noqa: E402
from speechnexus_portable_sql.claims import STATEMENTS as CLAIM_STATEMENTS  # noqa: E402
from speechnexus_portable_sql.manifest import (  # noqa: E402 - 需先补 sys.path
    Manifest,
    ManifestError,
    ProfileVariant,
    digest,
    load_manifest,
    load_profile_registry,
)
from speechnexus_portable_sql.schema import AUTHORITY_SQL  # noqa: E402
from speechnexus_portable_sql.subset import (  # noqa: E402
    SubsetViolationError,
    placeholder_count,
    validate,
)
from speechnexus_portable_sql.usage import STATEMENTS as USAGE_STATEMENTS  # noqa: E402

MANIFEST = STATEMENTS / "manifest.json"
PROFILES = STATEMENTS / "profiles.json"
DIGEST = STATEMENTS / "manifest.digest"


def check_manifest(manifest: Manifest, profiles: dict[str, object]) -> list[str]:
    """返回问题描述;空列表表示通过。"""

    problems: list[str] = []
    seen_variants: set[tuple[str, str]] = set()

    for statement in manifest.statements:
        try:
            validate(statement.sql)
        except SubsetViolationError as error:
            problems.append(f"{statement.statement_id}: outside the subset ({error})")
        expected = placeholder_count(statement.sql)
        if expected != len(statement.params):
            problems.append(
                f"{statement.statement_id}: {expected} placeholders but {len(statement.params)} "
                "declared parameters"
            )
        if not statement.transaction:
            problems.append(f"{statement.statement_id}: missing transaction boundary")

    # 运行时可执行 SQL 是唯一来源:manifest 与代码必须互为子集,任何一侧漂移都红。
    executable = {
        "authority.probe": AUTHORITY_SQL,
        **CLAIM_STATEMENTS,
        **BUDGET_STATEMENTS,
        **USAGE_STATEMENTS,
    }
    registered = {statement.statement_id: statement.sql for statement in manifest.statements}
    for statement_id, sql in sorted(executable.items()):
        if statement_id not in registered:
            problems.append(f"{statement_id}: executable statement is not registered")
        elif registered[statement_id] != sql:
            problems.append(f"{statement_id}: SQL drifted from the executable statement")
    for statement_id in sorted(set(registered) - set(executable)):
        problems.append(f"{statement_id}: registered but no executable statement exists")

    for variant in manifest.profiles:
        key = (variant.profile_id, variant.statement_id)
        if key in seen_variants:
            problems.append(f"duplicate variant {key}")
            continue
        seen_variants.add(key)
        if variant.profile_id not in profiles:
            problems.append(f"{variant.statement_id}: unregistered profile {variant.profile_id!r}")
        declared = manifest.statement(variant.statement_id)
        if declared is None:
            problems.append(f"variant references unknown statement {variant.statement_id!r}")
            continue
        try:
            validate(variant.sql)
        except SubsetViolationError as error:
            problems.append(f"variant {key}: outside the subset ({error})")
        expected = placeholder_count(variant.sql)
        if expected != len(declared.params):
            problems.append(
                f"variant {key}: {expected} placeholders changes the parameter shape "
                f"({len(declared.params)} declared)"
            )

    return problems


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def check_digest(manifest: Manifest, expected_path: Path) -> tuple[str, str | None]:
    """返回 (当前摘要, 问题或 None)。"""

    current = digest(manifest)
    if not expected_path.is_file():
        return current, f"missing {_display(expected_path)}"
    expected = expected_path.read_text(encoding="utf-8").strip()
    if expected != current:
        return current, (
            f"statement manifest digest changed ({expected[:12]} -> {current[:12]}); "
            "review the contract change and rerun with --write-digest"
        )
    return current, None


def claims_sql(statement_id: str) -> str:
    return CLAIM_STATEMENTS[statement_id]


def selftest() -> list[str]:
    """注入式自检:每一类漂移都必须能红。"""

    problems: list[str] = []
    base = load_manifest(MANIFEST)
    profiles = load_profile_registry(PROFILES)
    statement = base.statements[0]

    if check_manifest(base, profiles):
        problems.append("the committed manifest does not pass its own checks")

    def mutated(**changes: object) -> Manifest:
        return Manifest(base.revision, (replace(statement, **changes),), base.profiles)  # type: ignore[arg-type]

    if not check_manifest(
        mutated(sql="SELECT meta_id FROM schema_meta WHERE meta_id = %s"), profiles
    ):
        problems.append("non-qmark parameter style was accepted")
    if not check_manifest(
        mutated(sql="SELECT meta_id FROM schema_meta WHERE meta_id = ? AND x = ?"), profiles
    ):
        problems.append("a changed parameter count was accepted")
    if not check_manifest(
        mutated(sql="SELECT CURRENT_TIMESTAMP FROM schema_meta FOR UPDATE SKIP LOCKED"), profiles
    ):
        problems.append("a forbidden construct was accepted")

    unregistered = ProfileVariant(
        profile_id="oracle-19",
        statement_id=statement.statement_id,
        sql=statement.sql,
        rationale="test",
        evidence="test",
    )
    if not check_manifest(Manifest(base.revision, base.statements, (unregistered,)), profiles):
        problems.append("an unregistered profile variant was accepted")

    if not check_manifest(mutated(sql=claims_sql("job.claim") + " /* drift */"), profiles):
        problems.append("a drifted statement SQL was accepted")

    if digest(mutated(result=statement.result[:1])) == digest(base):
        problems.append("a changed result shape did not change the manifest digest")
    if digest(mutated(lock_order=("schema_meta", "coordination_jobs"))) == digest(base):
        problems.append("a changed lock order did not change the manifest digest")

    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--profiles", type=Path, default=PROFILES)
    parser.add_argument("--digest-file", type=Path, default=DIGEST)
    parser.add_argument("--write-digest", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        problems = selftest()
        for problem in problems:
            print(f"SELFTEST FAIL: {problem}", file=sys.stderr)
        print("selftest OK: every manifest drift is rejected" if not problems else "SELFTEST FAIL")
        return 1 if problems else 0

    try:
        manifest = load_manifest(args.manifest)
        profiles = load_profile_registry(args.profiles)
    except ManifestError as error:
        print(f"RESULT FAIL: {error}", file=sys.stderr)
        return 1

    problems = check_manifest(manifest, profiles)
    current, digest_problem = check_digest(manifest, args.digest_file)
    if digest_problem is not None:
        problems.append(digest_problem)

    if args.write_digest:
        args.digest_file.write_text(current + "\n", encoding="utf-8")
        print(f"wrote {_display(args.digest_file)} = {current}")
        return 0

    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    if problems:
        print("RESULT FAIL: statement manifest is not consistent")
        return 1
    print(
        f"statements={len(manifest.statements)} profiles={len(manifest.profiles)} "
        f"digest={current[:12]}"
    )
    print("RESULT OK: statement manifest, profiles and digest agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
