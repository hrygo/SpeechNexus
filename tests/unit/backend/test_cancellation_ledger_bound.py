"""The ledger's attempt bound is one object, not three copies of one number.

``speech_job_cancellation_reconciliations.attempts`` is bounded in three places that
must agree: the table's ``CHECK``, the claim's clamp
``LEAST(attempts + 1, _MAX_ATTEMPTS)``, and the readiness read that names a saturated
row (``ATTEMPT_CEILING``, added by #1038). The claim side now imports the schema
constant, so equality is a consequence of identity. That makes a value comparison the
wrong guard: it goes green on any pair of equal literals, which is exactly the
re-forking this file exists to catch.

These assertions therefore check the *source binding* and the *SQL text*:

* the claim module's ``_MAX_ATTEMPTS`` must be bound to the imported
  ``ATTEMPT_CEILING`` name, not to a literal, and it must be imported from the schema
  module rather than defined next door;
* the claim must pass that binding as its ``%(max_attempts)s`` parameter, and the SQL
  must clamp through the placeholder with no numeric literal in its place;
* the DDL must interpolate the same object.

``is`` alone cannot carry the Python side: ``64`` is a CPython cached small int, so a
reintroduced ``_MAX_ATTEMPTS = 64`` is still ``is``-identical to the constant. The AST
assertion is what makes the counterexample fail.

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


def _module_level_binding(name: str) -> ast.expr | None:
    for node in _claim_module_tree().body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node.value
    return None


def _parameters_dict_value(key: str) -> ast.expr | None:
    for node in ast.walk(_claim_module_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for dict_key, dict_value in zip(node.keys, node.values, strict=True):
            if isinstance(dict_key, ast.Constant) and dict_key.value == key:
                return dict_value
    return None


def _imports_ceiling_from_schema() -> bool:
    for node in ast.walk(_claim_module_tree()):
        if not isinstance(node, ast.ImportFrom) or not (node.module or "").endswith(
            _SCHEMA_MODULE_SUFFIX
        ):
            continue
        if any(alias.name == "ATTEMPT_CEILING" for alias in node.names):
            return True
    return False


def test_the_claim_binds_the_read_face_constant_instead_of_a_literal() -> None:
    """One bound, two readers: the claim alias must be the imported object itself."""
    binding = _module_level_binding("_MAX_ATTEMPTS")
    assert isinstance(binding, ast.Name) and binding.id == "ATTEMPT_CEILING", (
        "the claim's _MAX_ATTEMPTS must be assigned the imported ATTEMPT_CEILING name, "
        f"not its own value; found {ast.dump(binding) if binding is not None else 'no binding'}"
    )
    assert _imports_ceiling_from_schema(), (
        "ATTEMPT_CEILING must be imported from job_cancellation_fence_schema: a "
        "look-alike constant defined beside the claim would satisfy the name check "
        "while re-forking the number"
    )
    # Weak on its own (small ints are cached, so a literal is also ``is``-identical);
    # it is kept to state the runtime result the AST assertion above guarantees.
    assert ledger_store._MAX_ATTEMPTS is fence_schema.ATTEMPT_CEILING


def test_the_claim_parameter_carries_that_binding() -> None:
    """The SQL parameter must be the imported alias, not a second number."""
    value = _parameters_dict_value("max_attempts")
    assert isinstance(value, ast.Name) and value.id == "_MAX_ATTEMPTS", (
        "the claim must pass the imported ceiling as max_attempts; a literal here "
        "would bypass the binding above; found "
        f"{ast.dump(value) if value is not None else 'nothing'}"
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
