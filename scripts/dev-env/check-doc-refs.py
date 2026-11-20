#!/usr/bin/env python3
"""校验文档里的 `文件:行号` 引用是否真的存在、是否越界.

为什么需要它:文档里写 `runtime_sql.py:545`,读者点不到、grep 不到;写
`config/settings.py:41` 而该文件根本不存在,比不写更坏——它让人以为有依据.
靠人逐条 `sed -n` 核,慢且必然出错(我们已经被猜出来的行号误导过一次).

判据有三种,都是机械的:
  MISSING      路径在仓库里不存在(通常是裸文件名,或缺了目录段)
  OUT_OF_RANGE 行号超过文件总行数
  AMBIGUOUS    只有裸文件名,且仓库里同名文件不止一个(无法确定指哪个)

这三种判据只回答「行号在不在」,不回答「那一行是不是被描述的那件事」.行号是序数:
目标文档在上方任意位置插一段,它下面所有行号整体后移,而引用字面仍然「存在且不越界」,
门禁照旧 PASS.#1038 的 6 处指针就是这样静默偏了 74-142 行.所以指向 Markdown 的引用
必须带内容断言,把「那一行是哪件事」写进引用本身:

    `deploy/README.md:1270` 后跟「该行须含 `check-public-job-execution-defaults.sql`」
    (括号用全角或半角皆可)

判据因此多出两种:
  DRIFT       断言片段不在目标行 ±ASSERTION_WINDOW 行内
  UNASSERTED  指向 Markdown 的终态引用没有带断言

窗口取 ±15:容忍同文件上方的小幅插入(一段话、几行表格),而 #1038 的 74-142 行整体
漂移必红.窗口取 0 会让「在文档顶部加一句话」把下方所有引用一起判红,重演 #859 那种
长期红、最后被无视的检查.片段由作者从目标行附近挑,必须是该行真实存在的内容;门禁只
校验存在性,不判断它是不是「该行最重要的那件事」.

断言只对终态文档判定;过程记录保持原样,连断言都不校验——改记录等于改写当时的事实.
代码目标(非 Markdown)的引用允许暂时不带断言,但报告用 `unasserted_code=` 计数,
让欠账可见而不是静默:这批引用同样会腐烂,只是逐条补断言需要人读目标行,分刀处理.

两类文档,两种处置(AGENTS.md「文档终态与事实化」):
  terminal 终态文档:docs/ 下除过程记录外的全部 Markdown.描述当前实现,引用
           必须仍然有效 → 全量门禁,新增一条就红.
  record   过程记录:docs/reviews/、docs/archive/、docs/validation/YYYY-MM-DD-*.md.
           按约定保留原时点事实:审查记录引用的是当时的代码行、`/private/tmp/...`
           临时工作区路径和从未提交的探针文件,这些失效本身就是证据,不能回头
           改.因此只计数、不门禁,并按基线键集合差报告增量.长期 FAIL 的检查
           等于没有检查:接入前 138 条问题全部出自过程记录、终态文档 0 条,真正
           的终态文档漂移会被这套噪音淹没.
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
  基线文件「接受」,那次改动在 PR diff 里可见,不是静默放行.`--write-baseline`
  也守这条线:它默认拒绝把基线里没有的终态问题写进 `terminal`,并逐条列出,必须
  再加 `--accept-new-terminal` 才写,且打印「已接受 N 条新终态问题」;重写已消失
  的旧键是收紧,不需要开关.否则「跑一次 --write-baseline」就能把一条没人看过的
  新问题永久写成「已接受」,门禁被一条命令静默打开.

过程记录在基线里存的是**问题键集合**(`<记录文件>::<引用>::<判定>`),不是绝对
计数,报告按「与基线键集合的差」给出 `RECORDS new=+N stale=+M`:
  new    这条问题键不在基线里 → 本片新增(或此前报告后新出现)的记录问题;
  stale  基线里有、现在没了 → 记录被删,或那条引用已经不再命中.
存计数的方案(到 #1063 为止)会随基线变旧而说谎:没人为「增量显示」去刷基线,
于是「实测 166 对基线 155」看起来像是本 PR 引入的 11 条.键集合没有这个问题——
新增记录只体现为 `new=+N`,不需要任何人记得刷新;差的是一份稳定的身份而不是一个
必然过期的数字.键含目标行号:同名目标换一行号是另一条问题,不能相互抵消.
两边都不参与判定,过程记录的增减不改变退出码.

`--fix` 把 DRIFT 的修复从「逐条改别的文件里的行号」压成一条命令:对每条 DRIFT,
若断言片段在目标文件里**唯一命中**,就把引用处的行号改写成命中行的行号;命中 0 处
或多处时不猜——该条不改、原样报 `UNFIXABLE` 并说明原因.它只改行号数字,不碰片段
文本,也不改目标文件.计划里的位移带符号(`30 -> 500 (+470)`),章节级搬移一眼可见.
`--dry-run` 只打印计划,不写文件.

写入后立刻重新扫描自检,判据不是再跑一遍 ±15 窗口——那等于用要修的窗口量误差,偏 1
行也会放过——而是逐条用**精确行号**核对写入行确有该片段,并要求引用总数不下降
(写入丢了引用时门禁计数会静默变少).残留 DRIFT、新出现 `UNASSERTED`、基线外终态
问题或过程记录布局问题都非零退出;`--dry-run` 不做写入,用首次扫描减去可修 DRIFT
外推同一判定,不把「门禁红但计划完整」显示成 PASS.

`--show-ok` 给 `--show` 补上 OK 侧:OK 引用也打印被引用行的原文,用来核对「这条
为什么算 OK」「片段是不是恰好命中了同一文件里另一处」.没有把全部结果塞进 `--json`:
JSON 是给机器消费的,把两千多条 OK 行加进去只会淹没真正的问题;人读的排障路径是
`--show`.

用法:
  scripts/dev-env/check-doc-refs.py                  # 报告,并按基线门禁终态文档
  scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json
  scripts/dev-env/check-doc-refs.py --write-baseline scripts/dev-env/doc-refs-baseline.json
                                             # 快照当前的问题键(过程记录刷新只影响
                                             # 增量显示);同一命令默认拒绝接受**新的**
                                             # 终态问题,需 --accept-new-terminal
  scripts/dev-env/check-doc-refs.py --path docs/operations --path deploy
  scripts/dev-env/check-doc-refs.py --show-records   # 逐条打印过程记录的问题
  scripts/dev-env/check-doc-refs.py --show           # 只打印问题项, 并附被引用行的原文
  scripts/dev-env/check-doc-refs.py --show --show-ok # OK 引用的被引用行也打印
  scripts/dev-env/check-doc-refs.py --fix --dry-run  # 只打印改写计划, 不写文件
  scripts/dev-env/check-doc-refs.py --fix            # 唯一命中时改写行号, 然后自检
  scripts/dev-env/check-doc-refs.py --json

默认扫描 `docs` 与 `deploy`:`deploy/README.md` 是终态文档,它自己的引用和指向它的引用
一样会腐烂.

退出码:0 没有新增终态问题;1 有新增终态问题或 `docs/reviews/` 下的文件不是审查
报告;2 环境不可用(不在 git 仓库、扫描路径不存在、基线不可读).`--fix` 沿用同一
判据:计划外仍有基线外终态问题、过程记录布局问题,或写入自检的精确行核对/引用总数
不变式失败时也是 1;dry-run 外推同一判定.
只读:只查 git 索引与工作区文件,除 `--write-baseline` 与 `--fix` 外不改任何东西.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple, TypedDict

# 文档里出现的 `路径:行号`.路径段允许 / . _ - 与字母数字.
REF = re.compile(
    r"(?<![\w/.-])([A-Za-z0-9_./-]+\.(?:py|sql|ts|tsx|js|mjs|sh|toml|yaml|yml|json|md)):(\d+)(?![\d])"
)

# 围栏代码块的起止行:最多 3 个前导空格,``` 或 ~~~,可带语言标记.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# 引用后面的内容断言:`path:line` 后跟「该行须含 `片段`」,全角或半角括号都可.
# 关掉的右引号可省:引用不一定包在代码段里.
ASSERTION = re.compile(r"^`?\s*[（(]\s*该行须含\s*`([^`\n]+)`\s*[）)]")
# 断言窗口:目标行上下各多少行内出现片段即算命中,理由见模块 docstring.
ASSERTION_WINDOW = 15

TERMINAL = "terminal"
RECORD = "record"

# 过程记录目录:这些目录下的 Markdown 保留原时点事实,不参与终态门禁.
RECORD_PREFIXES = ("docs/reviews/", "docs/archive/")
# dated 记录:`docs/validation/2026-11-14-job-workspace-default.md` 形态的验证记录.
# `docs/audits/` 同样按 dated 命名放时点审计:今天没有这样的文件,但把 dated 审查报告
# 直接丢进 `docs/audits/` 会绕开下面 `docs/reviews/` 的内容守卫,所以一并按记录处理.
DATED_RECORD = re.compile(r"^docs/(validation|audits)/\d{4}-\d{2}-\d{2}-.+\.md$")
# `docs/reviews/` 只放审查报告,否则过程记录的豁免就成了藏文档的地方.判据有两层,
# 都作用在文件自身:`<YYYY-MM-DD>-*.md` 的文件名,加上「一级标题写明是审查/复核/
# 核对,且开头 400 字符引用 issue/PR 编号」的内容特征.只查文件名挡不住「把终态
# 文档另存成合规文件名」——那种副本文件名合约定、引用却全部降级成记录.
REVIEW_RECORD_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-")
REVIEW_MARKER = re.compile(r"审查|复核|核对|review", re.IGNORECASE)
RECORD_REFERENCE = re.compile(r"#\d{2,}")
RECORD_HEAD_CHARS = 400

BASELINE_SCHEMA = 2
BASELINE_NOTE = (
    "check-doc-refs 棘轮基线:terminal 是已接受的终态文档引用问题(空列表 = 全量门禁),"
    "records 是已见过的过程记录问题键集合(<记录文件>::<引用>::<判定>),"
    "只用于按键集合差报告 new/stale 增量,不参与判定;存计数会随基线变旧而失真(#1063)."
    "由 scripts/dev-env/check-doc-refs.py --write-baseline 生成,刷新只改变增量显示."
)
# 过程记录问题键的分隔符:文档与引用里不会出现 `::`,而引用本身含 `:行号`,
# 所以键必须把「文档」「路径:行号」「判定」三段分开,不能只靠 `::` 切两段.
KEY_SEP = "::"
# 过程记录只产生这三种问题:断言判定(DRIFT/UNASSERTED)只对终态文档生效,所以
# 记录键里不会出现它们;出现即基线格式错误,按环境问题退出 2,不静默接受.
RECORD_VERDICTS = ("MISSING", "OUT_OF_RANGE", "AMBIGUOUS")


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
    assertion: str


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


def scan_document(doc: Path) -> tuple[list[tuple[int, str, str, int, str]], int]:
    """返回 (围栏外的引用, 围栏内被跳过的引用数).

    围栏代码块(顶栏与底栏之间的全部行)不参与检查:那里是引用的命令输出、
    探针摘录和示例片段,不是作者对当前文件的指认,详见模块 docstring.

    每条引用是 `(文档行号, 该行原文, 路径, 行号, 内容断言)`;断言为空表示作者
    没写,终态文档里指向 Markdown 的引用缺它就判 UNASSERTED.
    """
    refs: list[tuple[int, str, str, int, str]] = []
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
            assertion = ASSERTION.match(line[m.end() :])
            refs.append(
                (lineno, line, m.group(1), int(m.group(2)), assertion.group(1) if assertion else "")
            )
    return refs, fenced


def fragment_lines(target: str, fragment: str) -> list[int]:
    """目标文件里包含片段的行号(升序).

    命中 0 处表示片段全文没有;命中多处表示工具无法判断作者指的是哪一处——两种
    情况 `--fix` 都不猜.文件读不到时也返回空列表,调用方按 DRIFT 的原因报告.
    """
    try:
        lines = Path(target).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [number for number, text in enumerate(lines, 1) if fragment in text]


def assertion_miss(target: str, ref_line: int, assertion: str) -> str | None:
    """断言片段不在目标行 ±ASSERTION_WINDOW 内时返回原因,命中返回 None."""
    try:
        lines = Path(target).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None  # 读不到交给 classify 的 MISSING 分支,不在这里重复报
    low = max(1, ref_line - ASSERTION_WINDOW)
    high = min(len(lines), ref_line + ASSERTION_WINDOW)
    if any(assertion in lines[number - 1] for number in range(low, high + 1)):
        return None
    occurrences = fragment_lines(target, assertion)
    if not occurrences:
        return f"±{ASSERTION_WINDOW} 行内没有 `{assertion}`，全文也没有"
    _, at = min((abs(number - ref_line), number) for number in occurrences)
    return f"±{ASSERTION_WINDOW} 行内没有 `{assertion}`，最近在第 {at} 行（{at - ref_line:+d} 行）"


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


def record_key(result: RefResult) -> str:
    """过程记录问题的稳定键:记录文件 + 引用 + 判定.

    引用里带目标行号(`settings.py:41`),所以同名目标换一个行号是**另一条**问题,
    不会被「计数相同」抵消.记录文件自己的行号不进键:在记录顶部补一句话不该把
    已有问题算成新增.判定也进键:同一引用从 `MISSING` 变成 `AMBIGUOUS` 是问题
    换了性质,按消失+新增报告比静默归零诚实.
    """
    return KEY_SEP.join((result["doc"], result["ref"], result["verdict"]))


def record_key_parts(key: str) -> tuple[str, str, str] | None:
    """拆开一条过程记录问题键;不是本格式时返回 None(基线格式错误,不是检查结果)."""
    doc, first, rest = key.partition(KEY_SEP)
    ref, second, verdict = rest.partition(KEY_SEP)
    if not first or not second or not doc or ":" not in ref:
        return None
    if verdict not in RECORD_VERDICTS:
        return None
    return doc, ref, verdict


def record_delta(current: Counter[str], accepted: list[str]) -> tuple[Counter[str], Counter[str]]:
    """(本次新增的过程记录问题键, 基线里有但现在没了的问题键).

    按键集合的差,不按计数:新增一条记录只让 `new` 加一,不会变成「基线 127 /
    实测 128」两套数字;基线登记过的键现在没命中则进 `stale`,不会被静默吞掉.
    """
    baseline = Counter(accepted)
    return current - baseline, baseline - current


def record_delta_buckets(keys: Counter[str]) -> dict[str, Counter[str]]:
    """增量按记录目录与判定分桶,让 `RECORDS new=/stale=` 的明细能定位到目录."""
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    for key in keys.elements():
        parts = record_key_parts(key)
        if parts is None:
            buckets["(无法解析)"]["?"] += 1
            continue
        buckets[record_bucket(parts[0])][parts[2]] += 1
    return dict(buckets)


def load_baseline(path: Path) -> tuple[Counter[str], list[str]]:
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

    raw_records = raw.get("records", [])
    if not isinstance(raw_records, list):
        raise ValueError(
            f"{path}: records 必须是问题键字符串列表(schema {BASELINE_SCHEMA})；"
            "旧版计数格式请用 --write-baseline 重写"
        )
    records: list[str] = []
    for item in raw_records:
        if not isinstance(item, str) or record_key_parts(item) is None:
            raise ValueError(f"{path}: records 的元素必须是 `<文件>::<路径:行号>::<判定>`")
        records.append(item)
    return Counter(terminal), records


def baseline_payload(terminal: Counter[str], record_keys: list[str]) -> dict[str, object]:
    return {
        "schema": BASELINE_SCHEMA,
        "note": BASELINE_NOTE,
        "terminal": sorted(terminal.elements()),
        "records": sorted(record_keys),
    }


def print_ref(result: RefResult, *, show: bool, marker: str = "") -> None:
    if show:
        excerpt = referenced_line(result["target"], int(result["line"]))
        print(
            f"[{result['verdict']}] {result['doc']}:{result['doc_line']}"
            f"  ->  {result['ref']}{marker}\n      {excerpt}"
        )
    else:
        print(
            f"[{result['verdict']}] {result['doc']}:{result['doc_line']}  ->  "
            f"{result['ref']}  ({result['detail']}){marker}"
        )


def counts_line(counts: Counter[str]) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


class Scan(NamedTuple):
    """一次全量扫描的结果与计数.

    `--fix` 写入后拿同一套扫描再跑一次自检,所以扫描必须是一个可重复调用的函数,
    不能留在 `main` 的循环里——自检和首次扫描走同一条代码路径,两者不一致就没有意义.
    """

    results: list[RefResult]
    scanned: list[tuple[str, Path]]
    total: int
    fenced: int
    fenced_terminal: int
    doc_refs: int
    doc_refs_asserted: int
    unasserted_code: int


def scan_repository(root: Path, scan_roots: list[Path]) -> Scan:
    """扫描 `scan_roots` 下的全部 Markdown,返回逐条结果与计数."""
    tracked = tracked_files(root)
    tracked_set = set(tracked)
    by_basename: dict[str, list[str]] = defaultdict(list)
    for p in tracked:
        by_basename[Path(p).name].append(p)

    results: list[RefResult] = []
    scanned: list[tuple[str, Path]] = []
    total = 0
    fenced = 0
    fenced_terminal = 0
    doc_refs = 0
    doc_refs_asserted = 0
    unasserted_code = 0

    seen_docs: set[Path] = set()
    for scan_root in scan_roots:
        for doc in sorted(scan_root.rglob("*.md")):
            if not doc.is_file():
                continue
            resolved = doc.resolve()
            if resolved in seen_docs:
                continue
            seen_docs.add(resolved)
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
            for lineno, line, ref_path, ref_line, assertion in refs:
                total += 1
                if "外部依赖" in line:
                    verdict, detail, target = "EXTERNAL", "行内已标注为外部依赖", ""
                else:
                    verdict, detail, target = classify(
                        ref_path, ref_line, assertion, scope, tracked_set, by_basename
                    )
                    if scope == TERMINAL and ref_path.endswith(".md"):
                        doc_refs += 1
                        if assertion:
                            doc_refs_asserted += 1
                    elif scope == TERMINAL and not assertion:
                        unasserted_code += 1
                results.append(
                    {
                        "doc": repo_path,
                        "doc_line": lineno,
                        "ref": f"{ref_path}:{ref_line}",
                        "verdict": verdict,
                        "detail": detail,
                        "target": target,
                        "line": ref_line,
                        "scope": scope,
                        "assertion": assertion,
                    }
                )
    return Scan(
        results=results,
        scanned=scanned,
        total=total,
        fenced=fenced,
        fenced_terminal=fenced_terminal,
        doc_refs=doc_refs,
        doc_refs_asserted=doc_refs_asserted,
        unasserted_code=unasserted_code,
    )


class FixPlan(NamedTuple):
    """一条 DRIFT 的改写计划;`new_line` 为 None 表示不猜、原样保留."""

    result: RefResult
    spans: list[tuple[int, int]]
    new_line: int | None
    reason: str


def rewrite_spans(root: Path, result: RefResult) -> list[tuple[int, int]]:
    """引用处那一行里,这段引用的行号数字所占的区间(按出现顺序).

    扫描与改写是两次独立的读取,所以这里按「路径:行号」和断言同时匹配重新定位。
    同一行里同一个引用出现多次时全部改写——它们指向同一个目标,新行号也相同,
    同一行里路径:行号相同但断言不同的两条引用,靠断言区分.
    """
    try:
        lines = (root / result["doc"]).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    if not 1 <= result["doc_line"] <= len(lines):
        return []
    line = lines[result["doc_line"] - 1]
    spans: list[tuple[int, int]] = []
    for match in REF.finditer(line):
        if f"{match.group(1)}:{match.group(2)}" != result["ref"]:
            continue
        assertion = ASSERTION.match(line[match.end() :])
        fragment = assertion.group(1) if assertion is not None else ""
        if fragment != result["assertion"]:
            continue
        spans.append((match.start(2), match.end(2)))
    return spans


def plan_fixes(root: Path, results: list[RefResult]) -> list[FixPlan]:
    """把每条 DRIFT 变成一条改写计划;0 处或多处命中都不猜."""
    plans: list[FixPlan] = []
    for result in results:
        if result["verdict"] != "DRIFT":
            continue
        occurrences = fragment_lines(result["target"], result["assertion"])
        if len(occurrences) != 1:
            if not occurrences:
                reason = "片段在目标文件出现 0 次，不改行号"
            else:
                shown = "、".join(str(number) for number in occurrences[:5])
                reason = f"片段在目标文件出现 {len(occurrences)} 次（第 {shown} 行），不猜"
            plans.append(FixPlan(result, [], None, reason))
            continue
        spans = rewrite_spans(root, result)
        if not spans:
            plans.append(FixPlan(result, [], None, "引用处找不到行号数字，未改写"))
            continue
        plans.append(FixPlan(result, spans, occurrences[0], ""))
    return plans


def print_fix_plan(plan: FixPlan) -> None:
    result = plan.result
    if plan.new_line is None:
        print(
            f"[DRIFT] {result['doc']}:{result['doc_line']}  ->  {result['ref']}"
            f"  [UNFIXABLE：{plan.reason}]"
        )
        return
    shift = plan.new_line - result["line"]
    print(
        f"[FIX] {result['doc']}:{result['doc_line']}  ->  {result['ref']}"
        f"  {result['line']} -> {plan.new_line} ({shift:+d})"
    )
    print(f"      命中：{referenced_line(result['target'], plan.new_line)}")


def apply_fixes(root: Path, plans: list[FixPlan]) -> None:
    """只改行号数字,按区间从右往左替换,同一行多条引用互不影响."""
    edits_by_doc: dict[str, dict[int, list[tuple[int, int, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for plan in plans:
        if plan.new_line is None:
            continue
        result = plan.result
        for start, end in plan.spans:
            edits_by_doc[result["doc"]][result["doc_line"]].append((start, end, str(plan.new_line)))
    for repo_path, edits_by_line in edits_by_doc.items():
        path = root / repo_path
        try:
            original = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"cannot read {repo_path}: {exc}") from exc
        lines = original.splitlines(keepends=True)
        for doc_line, edits in edits_by_line.items():
            line = lines[doc_line - 1]
            for start, end, new in sorted(edits, reverse=True):
                if not line[start:end].isdigit():
                    raise SystemExit(f"{repo_path}:{doc_line} 的行号区间不是数字，未写入")
                line = line[:start] + new + line[end:]
            lines[doc_line - 1] = line
        try:
            path.write_bytes("".join(lines).encode("utf-8"))
        except OSError as exc:
            raise SystemExit(f"cannot write {repo_path}: {exc}") from exc


def self_check(root: Path, plans: list[FixPlan], first: Scan, after: Scan) -> list[str]:
    """写入后的不变式;返回违反项(空列表 = 通过).

    不能复用 ±ASSERTION_WINDOW:窗口容得下 15 行误差,而这里要回答的正是「刚写下去
    的这一个行号对不对」——偏 1 行也会被窗口放过,自检灵敏度就等于要修的窗口.
    所以逐条用精确行号核对目标行确有该片段。再要求引用总数不下降:写入把引用删掉
    时(实测 2175 -> 2146)逐条核对无从谈起,但门禁计数会静默变少,总量是最后一道闸.
    """
    failures: list[str] = []
    for plan in plans:
        if plan.new_line is None:
            continue
        result = plan.result
        try:
            lines = (
                (root / result["target"]).read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            failures.append(
                f"{result['doc']}:{result['doc_line']} 写入 {result['target']}:{plan.new_line}"
                " 后读不到目标文件"
            )
            continue
        if (
            not 1 <= plan.new_line <= len(lines)
            or result["assertion"] not in lines[plan.new_line - 1]
        ):
            failures.append(
                f"{result['doc']}:{result['doc_line']} 写入 {result['target']}:{plan.new_line}，"
                f"该行没有 `{result['assertion']}`"
            )
    if after.total < first.total:
        failures.append(f"引用总数 {first.total} -> {after.total}，写入后丢了引用")
    return failures


def run_fix(
    root: Path,
    scan_roots: list[Path],
    accepted: Counter[str],
    first: Scan,
    *,
    dry_run: bool,
) -> int:
    """执行 `--fix`:计划、可选写入、写入后自检.

    退出码与门禁对齐:残留 DRIFT、新出现 UNASSERTED、基线外终态问题、过程记录布局
    问题,或写入自检的精确行核对/引用总数不变式失败时为 1;dry-run 不做写入,用首次
    扫描减去可修 DRIFT 外推同样的判定,防止「门禁红但计划完整」显示成 PASS.
    """
    plans = plan_fixes(root, first.results)
    for plan in plans:
        print_fix_plan(plan)
    fixable = sum(1 for plan in plans if plan.new_line is not None)
    unfixable = len(plans) - fixable
    drift_before = len(plans)
    unasserted_before = sum(1 for r in first.results if r["verdict"] == "UNASSERTED")
    first_problems = [r for r in first.results if r["verdict"] not in ("OK", "EXTERNAL")]
    first_terminal = [r for r in first_problems if r["scope"] == TERMINAL]
    first_keys: Counter[str] = Counter(problem_key(r) for r in first_terminal)
    new_keys = first_keys - accepted
    layouts = misplaced_records(first.scanned)
    fixed_keys: Counter[str] = Counter(
        problem_key(plan.result) for plan in plans if plan.new_line is not None
    )
    projected_new = new_keys - fixed_keys
    print(
        f"\nFIX drift={drift_before} fixable={fixable} unfixable={unfixable} "
        f"projected_new={sum(projected_new.values())}"
    )
    for repo_path, reason in layouts:
        print(f"LAYOUT {repo_path}  {reason}")

    if dry_run:
        reasons: list[str] = []
        if unfixable:
            reasons.append(f"计划未覆盖全部 DRIFT（unfixable={unfixable}）")
        if projected_new:
            reasons.append(f"计划外仍有 {sum(projected_new.values())} 条基线外终态问题")
        if layouts:
            reasons.append(f"{len(layouts)} 个过程记录布局问题")
        if reasons:
            print("FIX RESULT FAIL  " + "；".join(reasons) + "；--dry-run 未写文件")
            return 1
        print("FIX RESULT PASS  --dry-run 未写文件；去掉 --dry-run 才会写入")
        return 0

    apply_fixes(root, plans)
    after = scan_repository(root, scan_roots)
    drift_after = sum(1 for r in after.results if r["verdict"] == "DRIFT")
    unasserted_after = sum(1 for r in after.results if r["verdict"] == "UNASSERTED")
    failures = self_check(root, plans, first, after)
    print(
        f"SELFCHECK written={fixable} failures={len(failures)} "
        f"drift_before={drift_before} drift_after={drift_after} "
        f"unasserted_before={unasserted_before} unasserted_after={unasserted_after}"
    )
    for failure in failures:
        print(f"FIX SELFCHECK FAIL {failure}")

    after_problems = [r for r in after.results if r["verdict"] not in ("OK", "EXTERNAL")]
    after_terminal = [r for r in after_problems if r["scope"] == TERMINAL]
    terminal_keys: Counter[str] = Counter(problem_key(r) for r in after_terminal)
    after_new = terminal_keys - accepted
    print(
        f"SCOPE {TERMINAL} problems={sum(terminal_keys.values())} "
        f"new={sum(after_new.values())} accepted={sum(accepted.values())}"
    )
    for result in after_terminal:
        if result["verdict"] == "DRIFT":
            print_ref(result, show=False, marker="  [未修复]")
    if drift_after:
        print(f"FIX UNRESOLVED drift={drift_after}  片段不唯一或全文没有，行号未改写")
    if unasserted_after > unasserted_before:
        print(
            f"FIX UNRESOLVED unasserted={unasserted_after - unasserted_before}"
            "  改写后新出现 UNASSERTED"
        )
    if failures:
        print(f"FIX UNRESOLVED selfcheck={len(failures)}  写入的行号或引用总数未通过精确核对")
    if drift_after or unasserted_after > unasserted_before or after_new or layouts or failures:
        print("RESULT FAIL")
        return 1
    print("RESULT PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--path",
        action="append",
        default=None,
        metavar="DIR",
        help="要扫描的目录，可重复（默认 docs 与 deploy）",
    )
    ap.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="棘轮基线 JSON；省略时不接受任何终态文档问题（等于全量门禁）",
    )
    ap.add_argument(
        "--accept-new-terminal",
        action="store_true",
        help=(
            "配合 --write-baseline：显式接受基线里没有的终态问题；缺省拒绝写入，"
            "避免一条命令把没看过的新问题写成「已接受」"
        ),
    )
    ap.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "按当前结果重写基线后退出：terminal 是已接受的问题，records 是过程记录"
            "问题键的快照（只影响增量显示）；过程记录目录布局不合约定时不写"
        ),
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
        help="只打印问题项并附被引用行的原文；用于判断内容是否与文档描述相符",
    )
    ap.add_argument(
        "--show-ok",
        action="store_true",
        help="配合 --show：OK 引用也打印被引用行原文（可单独使用）",
    )
    ap.add_argument(
        "--fix",
        action="store_true",
        help="对每条 DRIFT：片段在目标文件唯一命中时改写行号，0/多处命中不改并报 UNFIXABLE",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="配合 --fix：只打印改写计划，不写文件",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.dry_run and not args.fix:
        print("--dry-run 需要与 --fix 一起使用", file=sys.stderr)
        return 2
    if args.fix and args.write_baseline is not None:
        print("--fix 不能与 --write-baseline 同时使用", file=sys.stderr)
        return 2
    if args.accept_new_terminal and args.write_baseline is None:
        print("--accept-new-terminal 需要与 --write-baseline 一起使用", file=sys.stderr)
        return 2
    if args.fix and args.json:
        print("--fix 不能与 --json 同时使用", file=sys.stderr)
        return 2
    if args.fix and (args.show or args.show_ok):
        print("--fix 不接受 --show：它自己会打印每条改写与命中的那一行", file=sys.stderr)
        return 2

    root = repository_root()
    if root is None:
        print("not inside a git repository", file=sys.stderr)
        return 2

    explicit_paths = args.path is not None
    scan_roots: list[Path] = []
    for name in args.path or ["docs", "deploy"]:
        candidate_root = Path(name)
        if not candidate_root.exists():
            if explicit_paths:
                print(f"path not found: {candidate_root}", file=sys.stderr)
                return 2
            # 默认根之一不存在(例如测试用的临时仓库没有 deploy/)时跳过,不是环境错误.
            continue
        resolved = candidate_root.resolve()
        if any(resolved == other.resolve() for other in scan_roots):
            continue
        scan_roots.append(candidate_root)
    if not scan_roots:
        print("path not found: docs, deploy", file=sys.stderr)
        return 2

    accepted: Counter[str] = Counter()
    baseline_records: list[str] = []
    if args.baseline is not None:
        try:
            accepted, baseline_records = load_baseline(args.baseline)
        except ValueError as exc:
            print(f"baseline invalid: {exc}", file=sys.stderr)
            return 2

    scan = scan_repository(root, scan_roots)
    if args.fix:
        return run_fix(root, scan_roots, accepted, scan, dry_run=args.dry_run)

    results = scan.results
    scanned = scan.scanned
    total = scan.total
    fenced = scan.fenced
    fenced_terminal = scan.fenced_terminal
    doc_refs = scan.doc_refs
    doc_refs_asserted = scan.doc_refs_asserted
    unasserted_code = scan.unasserted_code

    problems = [r for r in results if r["verdict"] not in ("OK", "EXTERNAL")]
    terminal_problems = [r for r in problems if r["scope"] == TERMINAL]
    record_problems = [r for r in problems if r["scope"] == RECORD]

    terminal_keys: Counter[str] = Counter(problem_key(r) for r in terminal_problems)
    new_keys = terminal_keys - accepted
    stale_keys = accepted - terminal_keys

    # 逐条展示与 --write-baseline 的守卫共用同一份切分:新的终态问题在前,
    # 基线已接受的在后面单独标注.放在写基线之前,写基线时才能逐条列出它要
    # 接受什么,而不是只印一个计数.
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

    record_counts: dict[str, Counter[str]] = {}
    for r in record_problems:
        record_counts.setdefault(record_bucket(r["doc"]), Counter())[r["verdict"]] += 1
    record_keys: Counter[str] = Counter(record_key(r) for r in record_problems)
    # 只有给了基线才有「差」可言;不给时既不报 new 也不报 stale,不把全部记录
    # 问题冒充成本次新增.
    records_new: Counter[str] = Counter()
    records_stale: Counter[str] = Counter()
    if args.baseline is not None:
        records_new, records_stale = record_delta(record_keys, baseline_records)

    layout_offenders = misplaced_records(scanned)

    if args.write_baseline is not None:
        if layout_offenders:
            for repo_path, reason in layout_offenders:
                print(f"LAYOUT {repo_path}  {reason}", file=sys.stderr)
            print("过程记录目录不合审查报告约定，未写基线", file=sys.stderr)
            return 1
        # `terminal` 是「已接受」白名单.把一条没人看过的新问题写成「已接受」,
        # 等于用一条命令静默打开门禁,所以新增终态键必须显式接受;把已消失的
        # 旧键重写掉是收紧,不需要开关.
        if new_keys:
            for r in new_terminal:
                print_ref(r, show=False, marker="  [新增,基线外]")
            if not args.accept_new_terminal:
                print(
                    f"REFUSED 基线里没有的终态问题 {sum(new_keys.values())} 条，未写基线："
                    "`--write-baseline` 不会静默把它们写成「已接受」；确认要接受后加 "
                    "`--accept-new-terminal`（并用 `--baseline` 指向前一份基线，"
                    "否则其余已接受项也会被当成新增）",
                    file=sys.stderr,
                )
                return 1
            print(
                f"ACCEPTED new-terminal={sum(new_keys.values())}  "
                f"已接受 {sum(new_keys.values())} 条新终态问题（--accept-new-terminal），"
                "它们从此不再门禁"
            )
        payload = baseline_payload(terminal_keys, list(record_keys.elements()))
        try:
            args.write_baseline.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"cannot write {args.write_baseline}: {exc.strerror}", file=sys.stderr)
            return 2
        print(
            f"WROTE {args.write_baseline} terminal={sum(terminal_keys.values())} "
            f"records={sum(record_keys.values())}"
        )
        return 0

    if args.json:
        print(
            json.dumps(
                {
                    "total": total,
                    "fenced": fenced,
                    "fenced_terminal": fenced_terminal,
                    "assertions": {
                        "doc_refs": doc_refs,
                        "asserted": doc_refs_asserted,
                        "unasserted_code": unasserted_code,
                        "window": ASSERTION_WINDOW,
                    },
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
                    "records_baseline": {
                        "new": sum(records_new.values()),
                        "stale": sum(records_stale.values()),
                    },
                    "layout": layout_offenders,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if new_keys or layout_offenders else 0

    if args.show or args.show_ok:
        # 排障模式:默认只打印问题项。全量打印会把 DRIFT 埋在两千多行 OK 里,
        # 而 `--show-ok` 是显式要求才放出的 OK 侧,并附被引用行原文——核对「这条
        # 为什么算 OK」「片段是不是恰好命中了同一文件里另一处」只能靠它.
        # 档位标记在排障视图里同样要带:先分清这条是新问题还是老问题,再看内容.
        tier = {id(r): "  [新增,基线外]" for r in new_terminal}
        tier.update({id(r): "  [基线已接受]" for r in accepted_terminal})
        for r in results:
            if r["verdict"] == "EXTERNAL":
                continue
            if r["verdict"] == "OK" and not args.show_ok:
                continue
            marker = tier.get(id(r), "  [过程记录]" if r["scope"] == RECORD else "")
            print_ref(r, show=True, marker=marker)
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
        f"ASSERT doc_refs={doc_refs} asserted={doc_refs_asserted} "
        f"window=±{ASSERTION_WINDOW} unasserted_code={unasserted_code}"
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
        # 增量只报差,不报「基线计数 -> 实测计数」:后者把两个必然不同步的数字
        # 摆在一起,读起来像是本片引入了那一段差值(#1063).
        print(
            f"RECORDS new={sum(records_new.values()):+d} "
            f"stale={sum(records_stale.values()):+d}"
            "  过程记录增量(与基线键集合的差,不参与判定)"
        )
        for label, keys in (("new", records_new), ("stale", records_stale)):
            for bucket, counts in sorted(record_delta_buckets(keys).items()):
                rendered = " ".join(
                    f"{verdict}={value:+d}" for verdict, value in sorted(counts.items())
                )
                print(f"RECORDS {label} {bucket} {rendered}")
        if records_stale:
            print(
                "RECORDS stale 基线里登记的过程记录问题现在没命中(记录被删或引用"
                "已不再命中),所以计为消失;不参与判定"
            )
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
    ref_path: str,
    ref_line: int,
    assertion: str,
    scope: str,
    tracked: set[str],
    by_basename: dict[str, list[str]],
) -> tuple[str, str, str]:
    """返回 (判定, 说明, 解析到的目标路径)。判定只可能是 OK / MISSING /
    OUT_OF_RANGE / AMBIGUOUS / DRIFT / UNASSERTED。"""
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
            return "AMBIGUOUS", f"后缀匹配到 {len(matches)} 个：{joined}", ""

    if candidate is None:
        same_name = by_basename.get(Path(ref_path).name, [])
        if len(same_name) == 1:
            # 裸文件名唯一:路径可推断,但文档里仍应写全
            return "MISSING", f"裸文件名，唯一匹配 {same_name[0]}", ""
        if len(same_name) > 1:
            return "AMBIGUOUS", f"{len(same_name)} 个同名文件", ""
        return "MISSING", "仓库内不存在该文件", ""

    total = line_count(candidate)
    if total is None:
        return "MISSING", f"无法读取 {candidate}", ""
    if ref_line < 1 or ref_line > total:
        return "OUT_OF_RANGE", f"{candidate} 共 {total} 行", candidate

    # 内容断言只对终态文档判定:记录里的引用保留原时点事实,连断言都不校验.
    if scope == TERMINAL:
        if assertion:
            miss = assertion_miss(candidate, ref_line, assertion)
            if miss is not None:
                return "DRIFT", f"{candidate}:{ref_line} {miss}", candidate
        elif ref_path.endswith(".md"):
            return (
                "UNASSERTED",
                "指向 Markdown 的终态引用须带「该行须含 `片段`」,否则行号漂移无法被发现",
                candidate,
            )
    return "OK", candidate, candidate


if __name__ == "__main__":
    sys.exit(main())
