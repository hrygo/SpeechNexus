"""Meta-guard: no configuration key may be silently inert without a disposition.

Issue #660 upgraded the known sample -- a configured
``SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`` that started clean and still answered
``503 voice_draft_unavailable`` -- into a named startup refusal, then scanned the
remaining "configured A only works with C" keys. The scan itself is the fragile
part: the next silent key will be added by someone who never reads this card. This
guard keeps the scan from drifting and keeps every silent row disposed of.

It performs six checks, all text-only (system interpreter, no service import, no
container, no database):

1. **Inventory.** Every optional selector field declared by every settings class in
   the scanned modules (``SecretStr`` / ``Path`` / ``AnyHttpUrl`` / ``str``
   annotated ``| None``) must appear as a row key or in :data:`EXEMPT` with a
   reason. The guard discovers the classes itself (any ``SettingsConfigDict``
   model), so a second settings class such as ``WorkbenchSettings`` cannot hide
   its fields from the scan, and it pins each discovered class name and prefix
   against :data:`SETTINGS_CLASSES`. A newly added companion key fails until it is
   classified.
2. **Judgment existence.** Every ``点名拒绝`` / ``启动告警点名`` row cites
   ``probes`` -- symbols in the product source that the guard resolves with
   ``ast``. The union of their source ranges must contain every ``message_keys``
   entry. Deleting the judgment, emptying it, or replacing the message with a
   generic sentence turns the guard red *without running the cited test*; the
   cited test is evidence of today's behaviour, not the guard's proof.
3. **Evidence.** Every row cites ``<path>::<sentinel>``; the path must exist and a
   ``.py`` sentinel must be a test function in that file, a ``.md`` sentinel must
   appear verbatim. A renamed test or reworded paragraph turns the guard red.
4. **No silent rows.** A row classified ``静默无效`` must carry ``已开 issue #N``;
   claiming ``已修`` while the row is still silent is a contradiction and is
   refused. ``文档已声明`` rows must cite a document (not a test) and carry no
   product probe; ``点名拒绝`` rows with ``names_keys`` must name keys, rows
   without it must not claim key names.
5. **Default consistency.** The audio budget defaults live in
   ``settings.py``, ``asset_runtime.py``, ``deploy/compose.yaml`` and
   ``deploy/compose.env.example``. The guard binds them, so a changed default
   cannot silently turn a Compose-forwarded value into "operator drift".
6. **Document sync.** ``docs/operations/configuration-prerequisites.md`` carries
   the same table for operators; its keys, display text, message marker and
   disposition cells must match the module row for row, and the sections the
   document promises (conditional refusals, unknown keys, meta-guard limits) must
   exist.

What it cannot see: it cannot prove a cited test asserts the right thing (the
probe proves the judgment exists; the test is evidence, not proof), it cannot see
a pair that was never declared in a settings class at all (``extra="ignore"``
means a misspelled key is not a field anywhere), it cannot see numeric/concurrency
knobs (Compose forwards their defaults, so "configured" is not observable at
startup), and it cannot see platform-injected variables outside these modules.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.ci.configuration_prerequisites import (
    AUDIO_LIMIT_FIELDS,
    BACKEND,
    BEHAVIOURS,
    COMPOSE,
    COMPOSE_ENV,
    DOC,
    DOCUMENTED,
    EXEMPT,
    MIGRATED_LIMIT_FIELDS,
    NAMED,
    ROWS,
    SETTINGS_CLASSES,
    SILENT,
    WARNED,
    Row,
    message_marker,
)

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / DOC
HEADING = "## 扫描表"
NEXT_HEADING = "\n## "
TABLE_ROW = re.compile(
    r"(?m)^\|\s*(?P<keys>[^|]*?)\s*\|\s*(?P<pre>[^|]*?)\s*\|"
    r"\s*(?P<display>[^|]*?)\s*\|\s*(?P<marker>[^|]*?)\s*\|"
    r"\s*(?P<disposition>[^|]*?)\s*\|\s*$"
)
DISPOSED = re.compile(r"^(?:已修 #(?P<fixed>\d+)|已开 issue #(?P<tracked>\d+))")
SILENT_DISPOSED = re.compile(r"^已开 issue #\d+")
SELECTOR_TYPE = re.compile(r"\b(?:SecretStr|Path|AnyHttpUrl|str)\b")
COMPOSE_FORWARD = re.compile(
    r"(?P<variable>SPEECHNEXUS_[A-Z0-9_]+):\s*\$\{(?:SPEECHNEXUS_[A-Z0-9_]+):-(?P<default>[^}]*)\}"
)
ASSIGNED = re.compile(r"(?m)^(?P<variable>SPEECHNEXUS_[A-Z0-9_]+)=(?P<value>.*)$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _const(node: ast.expr) -> object:
    """Evaluate the integer/float constant expressions used in defaults."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_const(node.operand)  # type: ignore[operator]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div, ast.Add, ast.Pow)):
        left, right = _const(node.left), _const(node.right)
        if isinstance(node.op, ast.Mult):
            return left * right  # type: ignore[operator]
        if isinstance(node.op, ast.Div):
            return left / right  # type: ignore[operator]
        if isinstance(node.op, ast.Pow):
            return left**right  # type: ignore[operator]
        return left + right  # type: ignore[operator]
    raise AssertionError(f"unsupported default expression: {ast.unparse(node)}")


def settings_models(module: str) -> dict[str, str]:
    """Discover ``{class name: env prefix}`` for every SettingsConfigDict model."""
    tree = ast.parse(read(ROOT / module))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if "model_config" not in targets:
                continue
            call = statement.value
            if not isinstance(call, ast.Call) or ast.unparse(call.func) != "SettingsConfigDict":
                continue
            prefix = ""
            for keyword in call.keywords:
                if keyword.arg == "env_prefix":
                    prefix = str(_const(keyword.value))
            found[node.name] = prefix
    return found


def selector_variables(module: str, class_name: str, prefix: str) -> dict[str, str]:
    """Return ``{env name: field name}`` for every optional selector in the class."""
    tree = ast.parse(read(ROOT / module))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            annotation = ast.unparse(statement.annotation)
            if "None" not in annotation or SELECTOR_TYPE.search(annotation) is None:
                continue
            field = statement.target.id
            found[prefix + field.upper()] = field
    return found


def _symbol_node(tree: ast.Module, dotted: str) -> ast.AST:
    scope: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in dotted.split("."):
        node = None
        for statement in scope:
            if (
                isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and statement.name == part
            ):
                node = statement
                break
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == part for target in statement.targets
            ):
                node = statement
                break
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == part
            ):
                node = statement
                break
        if node is None:
            raise AssertionError(f"symbol {dotted!r} not found")
        scope = node.body if isinstance(node, ast.ClassDef) else []
    assert node is not None
    return node


def probe_source(path: str, spec: str) -> str:
    """Resolve ``path::symbol[|symbol]`` (or ``path::text:fragment``) to source text."""
    text = read(ROOT / path)
    if spec.startswith("text:"):
        fragment = spec.removeprefix("text:")
        if fragment not in text:
            raise AssertionError(f"{path} does not contain {fragment!r}")
        return fragment
    tree = ast.parse(text)
    chunks: list[str] = []
    for symbol in spec.split("|"):
        node = _symbol_node(tree, symbol)
        segment = ast.get_source_segment(text, node)
        if not segment:
            raise AssertionError(f"{path}::{symbol} has no source")
        chunks.append(segment)
    return "\n".join(chunks)


def documented_rows() -> dict[str, tuple[str, str, str]]:
    """Parse the operator table between ``## 扫描表`` and the next level-two heading."""
    text = read(DOC_PATH)
    start = text.find(HEADING)
    if start < 0:
        raise AssertionError(f"{DOC} has no {HEADING!r} section")
    end = text.find(NEXT_HEADING, start + len(HEADING))
    section = text[start : len(text) if end < 0 else end]
    rows: dict[str, tuple[str, str, str]] = {}
    for match in TABLE_ROW.finditer(section):
        keys = match.group("keys").strip()
        if not keys or set(keys) <= {"-", " "}:
            continue
        if "键" in keys:
            continue
        rows[keys] = (
            match.group("display").strip(),
            match.group("marker").strip(),
            match.group("disposition").strip(),
        )
    return rows


def keys_cell(row: Row) -> str:
    if not row.keys:
        return "（数值与并发旋钮组）"
    return "、".join(f"`{key}`" for key in row.keys)


def display_cell(row: Row) -> str:
    return row.display or row.behaviour


class ConfigurationPrerequisitesTest(unittest.TestCase):
    def test_every_settings_class_is_discovered_with_its_prefix(self) -> None:
        discovered: dict[tuple[str, str], str] = {}
        for module, _, _ in SETTINGS_CLASSES:
            for name, prefix in settings_models(module).items():
                discovered[(module, name)] = prefix
        expected = {(module, name): prefix for module, name, prefix in SETTINGS_CLASSES}
        self.assertEqual(
            sorted(expected),
            sorted(discovered),
            "a settings class was added, renamed or lost its SettingsConfigDict; pin it",
        )
        for key, prefix in expected.items():
            self.assertEqual(prefix, discovered[key], f"{key[0]}:{key[1]} prefix changed")

    def test_every_optional_selector_is_classified(self) -> None:
        covered = {key for row in ROWS for key in row.keys}
        for module, class_name, prefix in SETTINGS_CLASSES:
            variables = selector_variables(module, class_name, prefix)
            self.assertTrue(variables, f"{module}:{class_name} yielded no optional selectors")
            for variable, field in variables.items():
                if (module, field) in EXEMPT:
                    self.assertTrue(
                        EXEMPT[(module, field)].strip(), f"{module}:{field} exemption has no reason"
                    )
                    continue
                self.assertIn(
                    variable,
                    covered,
                    f"{module}:{class_name}.{field} is an unclassified optional selector; add a "
                    f"row to tests/ci/configuration_prerequisites.py or an EXEMPT reason",
                )

    def test_exemptions_name_real_unclassified_selectors(self) -> None:
        selectors: dict[tuple[str, str], str] = {}
        for module, class_name, prefix in SETTINGS_CLASSES:
            for variable, field in selector_variables(module, class_name, prefix).items():
                selectors[(module, field)] = variable
        covered = {key for row in ROWS for key in row.keys}
        for (module, field), reason in EXEMPT.items():
            self.assertIn(
                (module, field),
                selectors,
                f"{module}:{field} is no longer an optional selector; drop the exemption",
            )
            self.assertNotIn(
                selectors[(module, field)],
                covered,
                f"{module}:{field} has a row and must not also be exempt",
            )
            self.assertTrue(reason.strip(), f"{module}:{field} exemption has no reason")

    def test_every_row_cites_existing_evidence(self) -> None:
        for row in ROWS:
            path, _, sentinel = row.evidence.partition("::")
            target = ROOT / path
            self.assertTrue(
                target.is_file(),
                f"{keys_cell(row)}: missing evidence path {path}",
            )
            text = read(target)
            if path.endswith(".py"):
                tree = ast.parse(text)
                names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                self.assertIn(sentinel, names, f"{path} has no test function {sentinel}")
            else:
                self.assertIn(sentinel, text, f"{path} no longer contains {sentinel!r}")

    def test_every_judgment_row_probes_a_live_judgment(self) -> None:
        """Deleting or neutralizing the judgment turns this red without any test."""
        for row in ROWS:
            if row.behaviour not in (NAMED, WARNED):
                continue
            self.assertNotEqual(
                (), row.probes, f"{keys_cell(row)}: a refusal row must probe its judgment"
            )
        for row in ROWS:
            chunks: list[str] = []
            for spec in row.probes:
                path, _, symbol = spec.partition("::")
                chunks.append(probe_source(path, symbol))
            sources = "\n".join(chunks)
            for variable in row.message_keys:
                self.assertIn(
                    variable,
                    sources,
                    f"{keys_cell(row)}: message key {variable} is absent from the judgment "
                    f"source; the check was deleted, emptied or generalized",
                )
            if row.names_keys:
                self.assertNotEqual(
                    (),
                    row.message_keys,
                    f"{keys_cell(row)}: names_keys claims key names but message_keys is empty",
                )
            else:
                self.assertEqual(
                    (),
                    row.message_keys,
                    f"{keys_cell(row)}: names_keys=False must not claim key names",
                )

    def test_documented_rows_cite_documents_and_carry_no_product_probe(self) -> None:
        for row in ROWS:
            if row.behaviour != DOCUMENTED:
                continue
            self.assertEqual((), row.probes, f"{keys_cell(row)}: documented rows have no judgment")
            path, _, sentinel = row.evidence.partition("::")
            self.assertTrue(path.endswith(".md"), f"{keys_cell(row)}: cite a document")
            self.assertIn(sentinel, read(ROOT / path), f"{path} lost {sentinel!r}")

    def test_a_silent_row_must_be_open_and_never_claim_a_fix(self) -> None:
        for row in ROWS:
            self.assertIn(row.behaviour, BEHAVIOURS, f"unknown behaviour {row.behaviour}")
            if row.behaviour != SILENT:
                continue
            self.assertIsNotNone(
                SILENT_DISPOSED.match(row.disposition),
                f"{keys_cell(row)} is silent and must record an open issue; a silent row cannot "
                f"claim a fix ({row.disposition})",
            )

    def test_the_operator_table_matches_the_module(self) -> None:
        documented = documented_rows()
        expected = {
            keys_cell(row): (display_cell(row), message_marker(row), row.disposition)
            for row in ROWS
        }
        self.assertEqual(
            sorted(expected),
            sorted(documented),
            f"{DOC} and tests/ci/configuration_prerequisites.py disagree about the keys",
        )
        for cell, cells in expected.items():
            self.assertEqual(
                cells,
                documented[cell],
                f"{DOC} row {cell} disagrees with the module",
            )

    def test_the_document_states_its_own_limits(self) -> None:
        text = read(DOC_PATH)
        for marker in (
            "消息点名",
            "已知非拒绝形态",
            "拼写与未知键",
            "只证明判定存在",
            'extra="ignore"',
        ):
            self.assertIn(marker, text, f"{DOC} lost the {marker!r} statement")
        start = text.find("## 元守护")
        self.assertGreater(start, 0, f"{DOC} has no meta-guard section")
        section = text[start : text.find(NEXT_HEADING, start)]
        self.assertIn("不证明行为", section, "the meta-guard section must not claim behaviour")

    def test_audio_budget_defaults_agree_everywhere(self) -> None:
        settings_text = read(ROOT / BACKEND)
        tree = ast.parse(settings_text)
        declared: dict[str, object] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name in AUDIO_LIMIT_FIELDS or name in MIGRATED_LIMIT_FIELDS:
                    call = node.value
                    self.assertIsInstance(call, ast.Call, f"{name} must use Field(...)")
                    for keyword in call.keywords:  # type: ignore[union-attr]
                        if keyword.arg == "default":
                            declared[name] = _const(keyword.value)
        runtime_tree = ast.parse(read(ROOT / "backend/src/speechnexus_backend/asset_runtime.py"))
        runtime_defaults: dict[str, object] = {}
        for node in ast.walk(runtime_tree):
            name = ""
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    name, value = target.id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            if name not in ("_AUDIO_ONLY_LIMITS", "_MIGRATED_LIMITS") or value is None:
                continue
            assert isinstance(value, ast.Tuple), f"{name} must be a tuple literal"
            for element in value.elts:
                assert isinstance(element, ast.Tuple)
                field = str(_const(element.elts[0]))
                runtime_defaults[field] = _const(element.elts[2])
        for field in (*AUDIO_LIMIT_FIELDS, *MIGRATED_LIMIT_FIELDS):
            self.assertIn(field, declared, f"{BACKEND} declares no default for {field}")
            self.assertIn(field, runtime_defaults, f"asset_runtime.py lost the drift tuple {field}")
            self.assertEqual(
                declared[field],
                runtime_defaults[field],
                f"{field}: settings.py and asset_runtime.py disagree about the default",
            )
        forwarded = dict(COMPOSE_FORWARD.findall(read(ROOT / COMPOSE)))
        example = dict(ASSIGNED.findall(read(ROOT / COMPOSE_ENV)))
        for field in (*AUDIO_LIMIT_FIELDS, *MIGRATED_LIMIT_FIELDS):
            variable = "SPEECHNEXUS_" + field.upper()
            if variable in forwarded:
                self.assertEqual(
                    float(str(declared[field])),
                    float(forwarded[variable]),
                    f"{COMPOSE} forwards a {variable} default that contradicts settings.py",
                )
            if variable in example:
                self.assertEqual(
                    float(str(declared[field])),
                    float(example[variable]),
                    f"{COMPOSE_ENV} sets {variable} to a value that contradicts settings.py",
                )

    def test_the_known_sample_is_a_named_refusal(self) -> None:
        """Regression anchor: the sample from #660 must not drift back to silent."""
        sample = [
            row
            for row in ROWS
            if row.keys and row.keys[0] == "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
        ]
        self.assertTrue(sample)
        self.assertTrue(all(row.behaviour == NAMED for row in sample))
        self.assertTrue(all(row.names_keys for row in sample))


if __name__ == "__main__":
    unittest.main()
