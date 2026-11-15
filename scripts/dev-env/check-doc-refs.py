#!/usr/bin/env python3
"""校验文档里的 `文件:行号` 引用是否真的存在、是否越界.

为什么需要它:文档里写 `runtime_sql.py:545`,读者点不到、grep 不到;写
`config/settings.py:41` 而该文件根本不存在,比不写更坏——它让人以为有依据.
靠人逐条 `sed -n` 核,慢且必然出错(我们已经被猜出来的行号误导过一次).

判据只有三种,都是机械的:
  MISSING      路径在仓库里不存在(通常是裸文件名,或缺了目录段)
  OUT_OF_RANGE 行号超过文件总行数
  AMBIGUOUS    只有裸文件名,且仓库里同名文件不止一个(无法确定指哪个)

两类文档,两种处置(AGENTS.md「文档终态与事实化」):
  terminal 终态文档:docs/ 下除过程记录外的全部 Markdown.描述当前实现,引用
           必须仍然有效 → 全量门禁,新增一条就红.
  record   过程记录:docs/reviews/、docs/archive/、docs/validation/YYYY-MM-DD-*.md.
           按约定保留原时点事实:审查记录引用的是当时的代码行、`/private/tmp/...`
           临时工作区路径和从未提交的探针文件,这些失效本身就是证据,不能回头
           改.因此只计数、不门禁,并按基线报告增量.长期 FAIL 的检查等于没有
           检查:接入前 138 条问题全部出自过程记录、终态文档 0 条,真正的终态
           文档漂移会被这套噪音淹没.
  判不准的路径一律按 terminal 处理:宁可让它变红,也不悄悄放行.

围栏代码块内不算引用.围栏里贴的是命令输出、探针摘录或示例,是文档引用别人
的产物,不是作者对当前文件的指认.实测 1427 条 `路径:行号` 文本有 119 条落在
围栏内,若按引用计入会多出 27 条问题(21 条 MISSING,其中 20 条来自审查记录里
的探针输出与 `/private/tmp` 绝对路径),它们不是本文档自身的引用.
这条规则对终态文档同样是「看不见」:终态文档围栏内的文本只计入
`fenced_terminal=`,不判定.理想形态是「围栏排除只对过程记录生效」,但本门禁的
证据块本身就要在围栏里贴失败的 `RESULT FAIL` 输出(见
`docs/validation/ci-validation.md`),按范围收窄会把证据块判成坏引用.因此保留
统一排除,并用 `fenced_terminal=` 把终态文档的这部分规模摆在报告里,不让它彻底
静默.

`docs/reviews/` 的豁免有内容守卫:目录下的 Markdown 必须形如 `<YYYY-MM-DD>-*.md`,
一级标题写明审查/复核/核对,且开头 400 字符引用 issue/PR 编号——只查文件名挡不住
「把终态文档另存成合规文件名」.当前 68 份审查报告全部通过,`docs/` 下 123 个终态
文档的原文搬到合规文件名下全部被拒.布局不合约定即失败,不能用基线接受.

棘轮:`--baseline` 只对基线之外(新增)的终态问题变红,基线为空时等价全量门禁.
  当前基线是 `scripts/dev-env/doc-refs-baseline.json`.新增终态问题只能通过改
  基线文件「接受」,那次改动在 PR diff 里可见,不是静默放行.

用法:
  scripts/dev-env/check-doc-refs.py                  # 报告,并按基线门禁终态文档
  scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json
  scripts/dev-env/check-doc-refs.py --write-baseline scripts/dev-env/doc-refs-baseline.json
  scripts/dev-env/check-doc-refs.py --path docs/operations
  scripts/dev-env/check-doc-refs.py --show-records   # 逐条打印过程记录的问题
  scripts/dev-env/check-doc-refs.py --show           # 连被引用行的原文一起打印
  scripts/dev-env/check-doc-refs.py --json

退出码:0 没有新增终态问题;1 有新增终态问题或 `docs/reviews/` 下的文件不是审查
报告;2 环境不可用(不在 git 仓库、扫描路径不存在、基线不可读).
只读:只查 git 索引与工作区文件,除 `--write-baseline` 外不改任何东西.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TypedDict

# 文档里出现的 `路径:行号`.路径段允许 / . _ - 与字母数字.
REF = re.compile(
    r"(?<![\w/.-])([A-Za-z0-9_./-]+\.(?:py|sql|ts|tsx|js|mjs|sh|toml|yaml|yml|json|md)):(\d+)(?![\d])"
)

# 围栏代码块的起止行:最多 3 个前导空格,``` 或 ~~~,可带语言标记.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

TERMINAL = "terminal"
RECORD = "record"

# 过程记录目录:这些目录下的 Markdown 保留原时点事实,不参与终态门禁.
RECORD_PREFIXES = ("docs/reviews/", "docs/archive/")
# dated 记录:`docs/validation/2026-11-14-job-workspace-default.md` 形态的验证记录.
DATED_RECORD = re.compile(r"^docs/validation/\d{4}-\d{2}-\d{2}-.+\.md$")
# `docs/reviews/` 只放审查报告,否则过程记录的豁免就成了藏文档的地方.判据有两层,
# 都作用在文件自身:`<YYYY-MM-DD>-*.md` 的文件名,加上「一级标题写明是审查/复核/
# 核对,且开头 400 字符引用 issue/PR 编号」的内容特征.只查文件名挡不住「把终态
# 文档另存成合规文件名」——那种副本文件名合约定、引用却全部降级成记录.
REVIEW_RECORD_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-")
REVIEW_MARKER = re.compile(r"审查|复核|核对|review", re.IGNORECASE)
RECORD_REFERENCE = re.compile(r"#\d{2,}")
RECORD_HEAD_CHARS = 400

BASELINE_SCHEMA = 1
BASELINE_NOTE = (
    "check-doc-refs 棘轮基线:terminal 是已接受的终态文档引用问题(空列表 = 全量门禁),"
    "records 是过程记录的问题计数(只用于报告增量,不门禁)."
    "由 scripts/dev-env/check-doc-refs.py --write-baseline 生成."
)


class RefResult(TypedDict):
    """一条引用检查结果."""

    doc: str
    doc_line: int
    ref: str
    verdict: str
    detail: str
    target: str
    line: int
    scope: str


def git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def repository_root() -> Path | None:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def tracked_files(root: Path) -> list[str]:
    return [p for p in git(root, "ls-files").splitlines() if p]


def relative_to_repo(doc: Path, root: Path) -> str:
    """仓库相对 POSIX 路径;仓库外的路径按原样返回(随后按终态文档处理)."""
    try:
        return doc.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return doc.as_posix()


def line_count(path: str) -> int | None:
    try:
        return len(Path(path).read_bytes().splitlines())
    except OSError:
        return None


def scan_document(doc: Path) -> tuple[list[tuple[int, str, str, int]], int]:
    """返回 (围栏外的引用, 围栏内被跳过的引用数).

    围栏代码块(顶栏与底栏之间的全部行)不参与检查:那里是引用的命令输出、
    探针摘录和示例片段,不是作者对当前文件的指认,详见模块 docstring.
    """
    refs: list[tuple[int, str, str, int]] = []
    fenced = 0
    fence: str | None = None
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
        marker = FENCE.match(line)
        if marker is not None:
            token = marker.group(1)
            if fence is None:
                fence = token
                continue
            if token[0] == fence[0] and len(token) >= len(fence):
                fence = None
                continue
        if fence is not None:
            fenced += len(REF.findall(line))
            continue
        for m in REF.finditer(line):
            refs.append((lineno, line, m.group(1), int(m.group(2))))
    return refs, fenced


def scope_of(repo_path: str) -> str:
    """terminal / record:过程记录目录之外一律按终态文档处理(判不准就门禁)."""
    if repo_path.startswith(RECORD_PREFIXES):
        return RECORD
    if DATED_RECORD.match(repo_path):
        return RECORD
    return TERMINAL


def record_bucket(repo_path: str) -> str:
    """过程记录按目录分桶计数,否则下次没人知道 docs/reviews/ 涨到多少."""
    return "/".join(repo_path.split("/")[:2])


def first_heading(text: str) -> str:
    """第一个一级标题(没有就返回空串)."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line
    return ""


def review_layout_problem(repo_path: str, text: str) -> str | None:
    """`docs/reviews/` 下的 Markdown 是不是一份审查报告;不是则返回原因.

    过程记录不参与终态门禁,所以这个目录是唯一能把引用从门禁里降级的位置.
    判据只认文件自身,不靠路径可见性:
      - 文件名 `<YYYY-MM-DD>-*.md`;
      - 一级标题写明审查/复核/核对(该目录里 68/68 份报告的写法),
        且开头 400 字符引用 issue/PR 编号(`#<数字>`,报告要写它对应哪张卡).
    实测效果:当前 68 份审查报告全部通过,`docs/` 下 123 个终态文档
    (`docs/architecture/`、`components/`、`operations/`、`plans/`、`prd.md` 等)
    的原文搬到合规文件名下全部被拒——`docs/architecture/speech-api.md` 就在其中.
    这条守卫防的是误放;刻意连内容一起伪装的绕过仍需同时改两处、diff 可见.
    不合约定即失败,并且不能用基线接受.
    """
    if not REVIEW_RECORD_NAME.match(Path(repo_path).name):
        return "文件名不是 <YYYY-MM-DD>-*.md"
    if not REVIEW_MARKER.search(first_heading(text)) or not RECORD_REFERENCE.search(
        text[:RECORD_HEAD_CHARS]
    ):
        return "内容不是审查报告（一级标题须含审查/复核/核对，开头 400 字符须含 #<issue 号>）"
    return None


def misplaced_records(scanned: list[tuple[str, Path]]) -> list[tuple[str, str]]:
    """`docs/reviews/` 下不合审查报告约定的文件与原因."""
    offenders: list[tuple[str, str]] = []
    for repo_path, doc in scanned:
        if not repo_path.startswith("docs/reviews/"):
            continue
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            offenders.append((repo_path, "无法读取，不能确认是审查报告"))
            continue
        problem = review_layout_problem(repo_path, text)
        if problem is not None:
            offenders.append((repo_path, problem))
    return offenders


def problem_key(result: RefResult) -> str:
    """基线里的稳定键:文档 + 引用目标.

    不含文档行号:文档在顶部插一行不该把已有问题算成新增;同一文档内的重复引用
    靠 Counter 计数区分.
    """
    return f"{result['doc']}::{result['ref']}"


def load_baseline(path: Path) -> tuple[Counter[str], dict[str, Counter[str]]]:
    """读棘轮基线;格式不对是环境问题(退出码 2),不是检查结果."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取 {path}: {exc.strerror}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} 不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 顶层必须是对象")
    if raw.get("schema") != BASELINE_SCHEMA:
        raise ValueError(f"{path}: schema 必须是 {BASELINE_SCHEMA}")

    raw_terminal = raw.get("terminal", [])
    if not isinstance(raw_terminal, list):
        raise ValueError(f"{path}: terminal 必须是字符串列表")
    terminal: list[str] = []
    for item in raw_terminal:
        if not isinstance(item, str):
            raise ValueError(f"{path}: terminal 必须是字符串列表")
        terminal.append(item)

    raw_records = raw.get("records", {})
    if not isinstance(raw_records, dict):
        raise ValueError(f"{path}: records 必须是对象")
    records: dict[str, Counter[str]] = {}
    for bucket, counts in raw_records.items():
        if not isinstance(bucket, str):
            raise ValueError(f"{path}: records 的键必须是目录名")
        if not isinstance(counts, dict):
            raise ValueError(f"{path}: records[{bucket}] 必须是对象")
        bucket_counter: Counter[str] = Counter()
        for verdict, value in counts.items():
            if not isinstance(verdict, str) or not isinstance(value, int):
                raise ValueError(f"{path}: records[{bucket}] 必须是「判定: 计数」")
            if isinstance(value, bool):
                raise ValueError(f"{path}: records[{bucket}] 的计数必须是整数")
            bucket_counter[verdict] = value
        records[bucket] = bucket_counter
    return Counter(terminal), records


def baseline_payload(terminal: Counter[str], records: dict[str, Counter[str]]) -> dict[str, object]:
    return {
        "schema": BASELINE_SCHEMA,
        "note": BASELINE_NOTE,
        "terminal": sorted(terminal.elements()),
        "records": {
            bucket: dict(sorted(counts.items())) for bucket, counts in sorted(records.items())
        },
    }


def print_ref(result: RefResult, *, show: bool, marker: str = "") -> None:
    if show:
        excerpt = referenced_line(result["target"], int(result["line"]))
        print(
            f"[{result['verdict']}] {result['doc']}:{result['doc_line']}"
            f"  ->  {result['ref']}\n      {excerpt}"
        )
    else:
        print(
            f"[{result['verdict']}] {result['doc']}:{result['doc_line']}  ->  "
            f"{result['ref']}  ({result['detail']}){marker}"
        )


def counts_line(counts: Counter[str]) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="docs", help="要扫描的目录（默认 docs）")
    ap.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="棘轮基线 JSON；省略时不接受任何终态文档问题（等于全量门禁）",
    )
    ap.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        metavar="PATH",
        help="按当前结果重写基线后退出；过程记录目录布局不合约定时不写",
    )
    ap.add_argument(
        "--problems-only",
        "--show-records",
        dest="show_records",
        action="store_true",
        help="逐条打印过程记录的问题（默认只打印计数，避免逐条噪音淹没终态问题）",
    )
    ap.add_argument(
        "--show",
        action="store_true",
        help="在每条引用后打印被引用行的原文；用于判断内容是否与文档描述相符",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = repository_root()
    if root is None:
        print("not inside a git repository", file=sys.stderr)
        return 2

    tracked = tracked_files(root)
    tracked_set = set(tracked)
    by_basename: dict[str, list[str]] = defaultdict(list)
    for p in tracked:
        by_basename[Path(p).name].append(p)

    scan_root = Path(args.path)
    if not scan_root.exists():
        print(f"path not found: {scan_root}", file=sys.stderr)
        return 2

    accepted: Counter[str] = Counter()
    baseline_records: dict[str, Counter[str]] = {}
    if args.baseline is not None:
        try:
            accepted, baseline_records = load_baseline(args.baseline)
        except ValueError as exc:
            print(f"baseline invalid: {exc}", file=sys.stderr)
            return 2

    results: list[RefResult] = []
    scanned: list[tuple[str, Path]] = []
    total = 0
    fenced = 0
    fenced_terminal = 0

    for doc in sorted(scan_root.rglob("*.md")):
        if not doc.is_file():
            continue
        repo_path = relative_to_repo(doc, root)
        scanned.append((repo_path, doc))
        try:
            refs, skipped = scan_document(doc)
        except (OSError, UnicodeDecodeError):
            continue
        fenced += skipped
        scope = scope_of(repo_path)
        if scope == TERMINAL:
            fenced_terminal += skipped
        for lineno, line, ref_path, ref_line in refs:
            total += 1
            if "外部依赖" in line:
                verdict, detail = "EXTERNAL", "行内已标注为外部依赖"
            else:
                verdict, detail = classify(ref_path, ref_line, tracked_set, by_basename)
            results.append(
                {
                    "doc": repo_path,
                    "doc_line": lineno,
                    "ref": f"{ref_path}:{ref_line}",
                    "verdict": verdict,
                    "detail": detail,
                    "target": detail if verdict in ("OK", "EXTERNAL") else "",
                    "line": ref_line,
                    "scope": scope,
                }
            )

    problems = [r for r in results if r["verdict"] not in ("OK", "EXTERNAL")]
    terminal_problems = [r for r in problems if r["scope"] == TERMINAL]
    record_problems = [r for r in problems if r["scope"] == RECORD]

    terminal_keys: Counter[str] = Counter(problem_key(r) for r in terminal_problems)
    new_keys = terminal_keys - accepted
    stale_keys = accepted - terminal_keys

    record_counts: dict[str, Counter[str]] = {}
    for r in record_problems:
        record_counts.setdefault(record_bucket(r["doc"]), Counter())[r["verdict"]] += 1

    layout_offenders = misplaced_records(scanned)

    if args.write_baseline is not None:
        if layout_offenders:
            for repo_path, reason in layout_offenders:
                print(f"LAYOUT {repo_path}  {reason}", file=sys.stderr)
            print("过程记录目录不合审查报告约定，未写基线", file=sys.stderr)
            return 1
        payload = baseline_payload(terminal_keys, record_counts)
        try:
            args.write_baseline.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"cannot write {args.write_baseline}: {exc.strerror}", file=sys.stderr)
            return 2
        print(
            f"WROTE {args.write_baseline} terminal={sum(terminal_keys.values())} "
            f"records={sum(sum(c.values()) for c in record_counts.values())}"
        )
        return 0

    # 逐条展示:新的终态问题(红灯原因)在前,基线已接受的在后面单独标注.
    remaining = Counter(new_keys)
    new_terminal: list[RefResult] = []
    accepted_terminal: list[RefResult] = []
    for r in terminal_problems:
        key = problem_key(r)
        if remaining[key] > 0:
            remaining[key] -= 1
            new_terminal.append(r)
        else:
            accepted_terminal.append(r)

    if args.json:
        print(
            json.dumps(
                {
                    "total": total,
                    "fenced": fenced,
                    "fenced_terminal": fenced_terminal,
                    "problems": problems,
                    "terminal": {
                        "problems": sum(terminal_keys.values()),
                        "new": sum(new_keys.values()),
                        "accepted": sum(accepted.values()),
                    },
                    "records": {
                        bucket: dict(sorted(counts.items()))
                        for bucket, counts in sorted(record_counts.items())
                    },
                    "layout": layout_offenders,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if new_keys or layout_offenders else 0

    if args.show:
        for r in results:
            if r["verdict"] != "EXTERNAL":
                print_ref(r, show=True)
    else:
        for r in new_terminal:
            print_ref(r, show=False, marker="  [新增,基线外]")
        for r in accepted_terminal:
            print_ref(r, show=False, marker="  [基线已接受]")
        if args.show_records:
            for r in record_problems:
                print_ref(r, show=False)

    for repo_path, reason in layout_offenders:
        print(
            f"LAYOUT {repo_path}  {reason}；过程记录不参与终态门禁，"
            "所以 docs/reviews/ 只放审查报告，终态文档请移到 docs/ 的对应目录"
        )

    counts: Counter[str] = Counter(r["verdict"] for r in results)
    print(
        f"\nSUMMARY refs={total} fenced={fenced} fenced_terminal={fenced_terminal} "
        f"{counts_line(counts)}"
    )
    print(
        f"SCOPE {TERMINAL} problems={sum(terminal_keys.values())} "
        f"new={sum(new_keys.values())} accepted={sum(accepted.values())}"
    )
    if record_counts:
        buckets = "; ".join(
            f"{bucket} {counts_line(counts)}" for bucket, counts in sorted(record_counts.items())
        )
        print(f"SCOPE {RECORD} problems={len(record_problems)} {buckets}")
    else:
        print(f"SCOPE {RECORD} problems=0")
    if args.baseline is not None:
        for bucket in sorted(set(baseline_records) | set(record_counts)):
            before = baseline_records.get(bucket, Counter())
            now = record_counts.get(bucket, Counter())
            deltas = [
                f"{verdict} {before.get(verdict, 0)}->{now.get(verdict, 0)} "
                f"({now.get(verdict, 0) - before.get(verdict, 0):+d})"
                for verdict in sorted(set(before) | set(now))
                if before.get(verdict, 0) != now.get(verdict, 0)
            ]
            if deltas:
                print(f"RECORDS {bucket} " + " ".join(deltas))
    if stale_keys:
        print(
            f"BASELINE stale={sum(stale_keys.values())}  基线登记的终态问题已消失，"
            "可用 --write-baseline 缩短基线"
        )
    if not new_keys and not layout_offenders:
        print("RESULT PASS")
        return 0
    print("RESULT FAIL")
    return 1


def referenced_line(path: str, lineno: int) -> str:
    """Return the referenced line verbatim, never a silent empty string."""
    if not path or lineno < 1:
        return "(无可展示内容)"
    try:
        lines = Path(path).read_bytes().splitlines()
    except OSError as exc:
        return f"(读取失败: {exc.strerror})"
    if lineno > len(lines):
        return f"(越界: 该文件仅 {len(lines)} 行)"
    return lines[lineno - 1].decode("utf-8", errors="replace").strip()[:200] or "(空行)"


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
