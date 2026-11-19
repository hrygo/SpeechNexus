"""The ledger's attempt bound is one object, not three copies of one number.

``speech_job_cancellation_reconciliations.attempts`` is bounded in three places that
must agree: the table's ``CHECK``, the claim's clamp
``LEAST(attempts + 1, _MAX_ATTEMPTS)``, and the readiness read that names a saturated
row (``ATTEMPT_CEILING``, added by #1038). The claim side imports the schema constant,
so equality is a consequence of identity. That makes a value comparison the wrong
guard: it goes green on any pair of equal literals, which is exactly the re-forking
this file exists to catch.

These assertions therefore check the *source binding* and the *SQL text*:

* the claim module's ``_MAX_ATTEMPTS`` must have exactly one module-level binding,
  and that binding must be the imported ``ATTEMPT_CEILING`` (bare name, its import
  alias, or ``<module alias>.ATTEMPT_CEILING``) -- never an expression that merely
  evaluates to the same number;
* the import must come from ``job_cancellation_fence_schema``, and the claim module
  must not rebind ``ATTEMPT_CEILING``: a later ``ATTEMPT_CEILING = 64`` wins at import
  time and would silently undo the alias;
* the claim must pass that binding as its ``%(max_attempts)s`` parameter, and the SQL
  must clamp through the placeholder with no numeric literal in its place;
* the DDL must interpolate the same object.

Two traps this file guards against, both of which pass a value comparison:

* ``is`` cannot tell a literal from the constant: ``64`` is a CPython cached small int,
  so ``_MAX_ATTEMPTS = 64`` is still ``is``-identical to ``ATTEMPT_CEILING``;
* Python keeps the *last* module-level binding, so a second assignment (or a later
  ``ATTEMPT_CEILING = 64``) overrides the imported one while a helper that reads the
  first binding stays green.

Drift is a behaviour bug, not a cosmetic one:

* a claim bound **below** the CHECK bound saturates ``attempts`` early, so the read
  face never says ``retrying at attempt ceiling`` for a row that is already spent --
  the silent state #1038 removed;
* a claim bound **above** it makes ``LEAST`` emit 65+, which the ``CHECK`` rejects: the
  claim ``UPDATE`` fails and the row stops being retried at all.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap

from speechnexus_backend.speech.infrastructure import (
    job_cancellation_fence_schema as fence_schema,
)
from speechnexus_backend.speech.infrastructure import (
    job_cancellation_reconciliation_postgres as ledger_store,
)

_SCHEMA_MODULE_SUFFIX = "job_cancellation_fence_schema"
_CEILING = "ATTEMPT_CEILING"
_CLAIM_ALIAS = "_MAX_ATTEMPTS"


def _claim_module_tree() -> ast.Module:
    """The claim module's own tree, so bindings can be read without executing them."""
    return ast.parse(textwrap.dedent(inspect.getsource(ledger_store)))


def _call_arguments(sql: str, function: str) -> list[list[str]]:
    """Top-level arguments of every ``function(...)`` call, paren nesting respected."""
    calls: list[list[str]] = []
    for match in re.finditer(rf"{function}\(", sql):
        depth, index = 1, match.end()
        while index < len(sql) and depth:
            if sql[index] == "(":
                depth += 1
            elif sql[index] == ")":
                depth -= 1
            index += 1
        assert depth == 0, f"unbalanced {function}( in the claim SQL"
        calls.append(_split_arguments(sql[match.end() : index - 1]))
    return calls


def _split_arguments(arguments: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for index, character in enumerate(arguments):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(arguments[start:index])
            start = index + 1
    parts.append(arguments[start:])
    return parts


def _module_level_bindings(name: str) -> list[tuple[int, ast.expr]]:
    """Every module-level assignment to ``name``, in source order."""
    bindings: list[tuple[int, ast.expr]] = []
    for node in _claim_module_tree().body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                bindings.append((node.lineno, node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            bindings.append((node.lineno, node.value))
    return bindings


def _one_binding(name: str) -> ast.expr:
    """The binding runtime keeps; a second one silently overrides the first."""
    bindings = _module_level_bindings(name)
    found = "; ".join(f"line {line}: {ast.dump(value)}" for line, value in bindings)
    assert len(bindings) == 1, (
        f"the claim module must bind {name} exactly once: Python keeps the last "
        "module-level binding, so a second one overrides what the import aliased; "
        f"found {found or 'no binding'}"
    )
    return bindings[0][1]


def _ceiling_import_bindings() -> tuple[set[str], set[str], set[str]]:
    """Names that denote the schema's ATTEMPT_CEILING without restating its value."""
    from_names: set[str] = set()
    module_aliases: set[str] = set()
    module_paths: set[str] = set()
    for node in ast.walk(_claim_module_tree()):
        if isinstance(node, ast.ImportFrom):
            from_schema = (node.module or "").endswith(_SCHEMA_MODULE_SUFFIX)
            for alias in node.names:
                if alias.name == _CEILING and from_schema:
                    from_names.add(alias.asname or alias.name)
                elif alias.name == _SCHEMA_MODULE_SUFFIX:
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.endswith(_SCHEMA_MODULE_SUFFIX):
                    continue
                if alias.asname:
                    module_aliases.add(alias.asname)
                else:
                    module_paths.add(alias.name)
    return from_names, module_aliases, module_paths


def _dotted_path(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_the_one_ceiling(node: ast.expr) -> bool:
    """True when the expression *is* the schema constant, not a copy of its value."""
    from_names, module_aliases, module_paths = _ceiling_import_bindings()
    if isinstance(node, ast.Name):
        return node.id in from_names
    if isinstance(node, ast.Attribute):
        path = _dotted_path(node)
        if path is None:
            return False
        candidates = {f"{alias}.{_CEILING}" for alias in module_aliases}
        candidates |= {f"{module}.{_CEILING}" for module in module_paths}
        return path in candidates
    return False


def _parameters_dict_values(key: str) -> list[tuple[int, ast.expr]]:
    values: list[tuple[int, ast.expr]] = []
    for node in ast.walk(_claim_module_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for dict_key, dict_value in zip(node.keys, node.values, strict=True):
            if isinstance(dict_key, ast.Constant) and dict_key.value == key:
                values.append((dict_value.lineno, dict_value))
    return values


def test_the_claim_binds_the_read_face_constant_instead_of_a_literal() -> None:
    """One bound, two readers: the claim alias must be the imported object itself."""
    imported = _ceiling_import_bindings()
    assert any(imported), (
        f"{_CEILING} must come from {_SCHEMA_MODULE_SUFFIX} (name, alias, or module "
        "alias): a look-alike constant defined beside the claim would satisfy the "
        "shape check while re-forking the number"
    )
    binding = _one_binding(_CLAIM_ALIAS)
    assert _is_the_one_ceiling(binding), (
        f"the claim's {_CLAIM_ALIAS} must be the schema's {_CEILING}, not a copy of "
        f"its value; accepted shapes: {_CEILING}, its import alias from "
        f"{_SCHEMA_MODULE_SUFFIX}, or <module alias>.{_CEILING}; found "
        f"{ast.dump(binding)} at line {binding.lineno}. Same-value expressions "
        "(64, int('64'), ATTEMPT_CEILING * 1) are rejected: they re-fork the value "
        "even when they happen to equal it today"
    )
    # Weak on its own (small ints are cached, so a literal is also ``is``-identical);
    # it is kept to state the runtime result the AST assertion above guarantees.
    assert ledger_store._MAX_ATTEMPTS is fence_schema.ATTEMPT_CEILING


def test_the_claim_does_not_rebind_the_imported_constant() -> None:
    """A later ``ATTEMPT_CEILING = 64`` wins at import time and undoes the alias."""
    rebindings = _module_level_bindings(_CEILING)
    found = "; ".join(f"line {line}: {ast.dump(value)}" for line, value in rebindings)
    assert rebindings == [], (
        f"the claim module must not reassign {_CEILING}: the import is its only "
        "binding, and a later module-level assignment overrides it before any claim "
        f"runs; found {found or 'nothing'}"
    )
    rebound_globals = [
        (node.lineno, name)
        for node in ast.walk(_claim_module_tree())
        if isinstance(node, ast.Global)
        for name in node.names
        if name in {_CEILING, _CLAIM_ALIAS}
    ]
    assert rebound_globals == [], (
        "the claim module must not rebind the ceiling or its alias through "
        f"``global``; found {rebound_globals}"
    )


def test_the_claim_parameter_carries_that_binding() -> None:
    """The SQL parameter must be the imported ceiling, not a second number."""
    values = _parameters_dict_values("max_attempts")
    found = "; ".join(f"line {line}: {ast.dump(value)}" for line, value in values)
    assert len(values) == 1, (
        "the claim must set max_attempts exactly once: a second dict entry would "
        f"silently override the first; found {found or 'nothing'}"
    )
    value = values[0][1]
    same_source = isinstance(value, ast.Name) and value.id == _CLAIM_ALIAS
    same_source = same_source or _is_the_one_ceiling(value)
    assert same_source, (
        "the claim must pass the imported ceiling as max_attempts, through the alias "
        f"or directly; a value here bypasses the binding above; found {ast.dump(value)}"
    )


def test_the_claim_sql_clamps_through_the_constant() -> None:
    """A number written inside the claim SQL is the copy no import can protect."""
    sql = ledger_store._CLAIM
    assert "%(max_attempts)s" in sql, "the claim must take the ceiling as a bound parameter"
    clamps = _call_arguments(sql, "LEAST")
    assert len(clamps) == 1, "the claim clamps attempts exactly once"
    increment, ceiling = (argument.strip() for argument in clamps[0])
    assert increment.endswith("+ 1"), (
        f"the clamp's first argument must increment the counter; found {increment!r}"
    )
    assert ceiling == "%(max_attempts)s", (
        "LEAST's second argument must be the placeholder, not a numeric literal: a "
        "number here is the copy that drifts while every Python-level assertion stays "
        f"green; found {ceiling!r}"
    )
    assert sql.count("%(max_attempts)s") == 1, (
        "one placeholder, bound by the parameters dict the previous test pins"
    )


def test_the_table_check_interpolates_that_ceiling() -> None:
    """The DDL must not carry a second copy of the number."""
    ddl = " ".join(fence_schema.JOB_CANCELLATION_FENCE_DDL)
    assert f"CHECK (attempts BETWEEN 0 AND {fence_schema.ATTEMPT_CEILING})" in ddl
    if fence_schema.ATTEMPT_CEILING != 64:  # the historical literal must not linger
        assert "CHECK (attempts BETWEEN 0 AND 64)" not in ddl


def test_the_debt_reader_is_granted_the_counter_it_compares() -> None:
    """The scope can only be computed if the reader may select the column."""
    grants = " ".join(
        fence_schema.cancellation_debt_reader_grants("speech", "cancellation_debt_reader")
    )
    assert "settled_at_us, last_outcome, next_check_at_us, attempts" in grants, (
        "the read face compares attempts against the ceiling, so the reader needs it"
    )
