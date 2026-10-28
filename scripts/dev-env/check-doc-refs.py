#!/usr/bin/env python3
"""校验文档里的 `文件:行号` 引用是否真的存在、是否越界.

为什么需要它:文档里写 `runtime_sql.py:545`,读者点不到、grep 不到;写
`config/settings.py:41` 而该文件根本不存在,比不写更坏——它让人以为有依据.
靠人逐条 `sed -n` 核,慢且必然出错(我们已经被猜出来的行号误导过一次).

判据只有三种,都是机械的:
  MISSING      路径在仓库里不存在(通常是裸文件名,或缺了目录段)
  OUT_OF_RANGE 行号超过文件总行数
  AMBIGUOUS    只有裸文件名,且仓库里同名文件不止一个(无法确定指哪个)

用法:
  scripts/dev-env/check-doc-refs.py                 # 检查 docs/ 全量
  scripts/dev-env/check-doc-refs.py --path docs/operations
  scripts/dev-env/check-doc-refs.py --problems-only
  scripts/dev-env/check-doc-refs.py --json

退出码:0 全部有效;1 有问题;2 环境不可用(不在 git 仓库内).
只读:只查 git 索引与工作区文件,不改任何东西.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

# 文档里出现的 `路径:行号`.路径段允许 / . _ - 与字母数字.
REF = re.compile(
    r"(?<![\w/.-])([A-Za-z0-9_./-]+\.(?:py|sql|ts|tsx|js|mjs|sh|toml|yaml|yml|json|md)):(\d+)(?![\d])"
)

# 不当作代码引用的后缀(:123 可能是时间、编号等)
SKIP_SUFFIX = (".md",)


class RefResult(TypedDict):
    """一条引用检查结果。"""

    doc: str
    doc_line: int
    ref: str
    verdict: str
    detail: str


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def tracked_files() -> list[str]:
    return [p for p in git("ls-files").splitlines() if p]


def line_count(path: str) -> int | None:
    try:
        return len(Path(path).read_bytes().splitlines())
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="docs", help="要扫描的目录（默认 docs）")
    ap.add_argument("--problems-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
        print("not inside a git repository", file=sys.stderr)
        return 2

    tracked = set(tracked_files())
    by_basename: dict[str, list[str]] = defaultdict(list)
    for p in tracked:
        by_basename[Path(p).name].append(p)

    scan_root = Path(args.path)
    if not scan_root.exists():
        print(f"path not found: {scan_root}", file=sys.stderr)
        return 2

    results: list[RefResult] = []
    total = 0

    for doc in sorted(scan_root.rglob("*.md")):
        if not doc.is_file():
            continue
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in REF.finditer(line):
                ref_path, ref_line = m.group(1), int(m.group(2))
                total += 1
                if "外部依赖" in line:
                    verdict, detail = "EXTERNAL", "行内已标注为外部依赖"
                else:
                    verdict, detail = classify(ref_path, ref_line, tracked, by_basename)
                results.append(
                    {
                        "doc": str(doc),
                        "doc_line": lineno,
                        "ref": f"{ref_path}:{ref_line}",
                        "verdict": verdict,
                        "detail": detail,
                    }
                )

    problems = [r for r in results if r["verdict"] not in ("OK", "EXTERNAL")]

    if args.json:
        print(json.dumps({"total": total, "problems": problems}, ensure_ascii=False, indent=2))
    else:
        shown = (
            problems
            if args.problems_only
            else [r for r in results if r["verdict"] not in ("OK", "EXTERNAL")]
        )
        for r in shown:
            print(f"[{r['verdict']}] {r['doc']}:{r['doc_line']}  ->  {r['ref']}  ({r['detail']})")
        counts: dict[str, int] = defaultdict(int)
        for r in results:
            counts[r["verdict"]] += 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"\nSUMMARY refs={total} {summary}")
        if not problems:
            print("RESULT PASS")
        else:
            print("RESULT FAIL")

    return 0 if not problems else 1


def classify(
    ref_path: str, ref_line: int, tracked: set[str], by_basename: dict[str, list[str]]
) -> tuple[str, str]:
    """返回 (判定, 说明)。判定只可能是 OK / MISSING / OUT_OF_RANGE / AMBIGUOUS。"""
    candidate: str | None = None

    if ref_path in tracked:
        candidate = ref_path
    else:
        # 允许写成仓库内任意相对路径(含 ../ 形态之外的前缀)
        matches = [p for p in tracked if p.endswith("/" + ref_path)]
        if len(matches) == 1:
            candidate = matches[0]
        elif len(matches) > 1:
            joined = ", ".join(sorted(matches)[:3])
            return "AMBIGUOUS", f"后缀匹配到 {len(matches)} 个：{joined}"

    if candidate is None:
        same_name = by_basename.get(Path(ref_path).name, [])
        if len(same_name) == 1:
            # 裸文件名唯一:路径可推断,但文档里仍应写全
            return "MISSING", f"裸文件名，唯一匹配 {same_name[0]}"
        if len(same_name) > 1:
            return "AMBIGUOUS", f"{len(same_name)} 个同名文件"
        return "MISSING", "仓库内不存在该文件"

    total = line_count(candidate)
    if total is None:
        return "MISSING", f"无法读取 {candidate}"
    if ref_line < 1 or ref_line > total:
        return "OUT_OF_RANGE", f"{candidate} 共 {total} 行"
    return "OK", candidate


if __name__ == "__main__":
    sys.exit(main())
