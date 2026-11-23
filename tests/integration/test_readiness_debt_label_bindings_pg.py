"""The cancellation-debt labels are values: they travel as parameters, not as SQL text.

``_LEFT_CANCELLING_DEBT`` and ``_PERMANENT_DEBT`` used to interpolate their ``debt_class``
label into the statement text (``'{LEFT_CANCELLING_HOLDING_CAPACITY}' AS debt_class``).
A label is data, not SQL: how a dialect quotes a string is that dialect's rule, and a value
that contains the quote character ends the literal early -- so the row set (or the whole
statement) depends on the text of a label rather than on a parameter.

Both halves are executed against a real PostgreSQL, and both are needed:

* the shipped statements return the label as data -- a label containing a quote comes back
  verbatim, which can only happen because the text never contained it;
* the interpolated form they replaced does not survive that label: the same statement with
  ``O'Brien`` quoted into it is a syntax error.

The second half is the concrete harm behind the change; it is not asserted in prose. It is
also why the change is a binding and not a quoting routine: a routine that escapes the
label would still be one implementation per dialect, which is the thing being removed.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from tests.integration.test_cancel_failure_slot_pg import (
    LEASE,
    QUARANTINED_DOCUMENT_STATE,
    CancelSandbox,
    _database_now,
    _sandbox,
)

from speechnexus_backend.speech.infrastructure.readiness import (
    _PERMANENT_DEBT,
    ABANDONED_HOLDING_CAPACITY,
)

pytestmark = pytest.mark.integration

#: A label a quoting rule would have to escape and a binding does not. ``O'Brien`` is the
#: smallest example that ends a SQL string literal early.
_HOSTILE_LABEL = "O'Brien"


def _rows(box: CancelSandbox, statement: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    with box.admin.cursor() as cursor:
        cursor.execute(statement, parameters)
        return list(cursor.fetchall())


def _permanent_parameters(box: CancelSandbox, label: str) -> dict[str, Any]:
    return {"now_us": _database_now(box), "grace_us": 0, "limit": 96, "debt_class": label}


def test_the_abandoned_read_returns_its_label_as_data() -> None:
    """The label the row carries is the parameter's value, whatever it contains.

    The hostile read returns the *same* rows as the shipped label: if the label were part
    of the statement text, these two executions would be two different queries.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        shipped = _rows(
            box, _PERMANENT_DEBT, _permanent_parameters(box, ABANDONED_HOLDING_CAPACITY)
        )
        hostile = _rows(box, _PERMANENT_DEBT, _permanent_parameters(box, _HOSTILE_LABEL))

    assert [row["job_id"] for row in shipped] == [box.job_id]
    assert [row["debt_class"] for row in shipped] == [ABANDONED_HOLDING_CAPACITY]
    assert [row["job_id"] for row in hostile] == [row["job_id"] for row in shipped]
    assert [row["debt_class"] for row in hostile] == [_HOSTILE_LABEL]


def test_the_interpolated_form_the_binding_replaced_breaks_on_that_label() -> None:
    """Both directions of the injection, on the real database.

    ``legacy`` is the statement as it was written before this change -- the label quoted
    into the projection. It is reconstructed here on purpose: the point is that the old
    shape cannot carry this label, so a test that only exercises the new shape would not
    show what was removed.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        parameters = _permanent_parameters(box, ABANDONED_HOLDING_CAPACITY)
        parameters.pop("debt_class")
        legacy = _PERMANENT_DEBT.replace(
            "%(debt_class)s AS debt_class", "'" + _HOSTILE_LABEL + "' AS debt_class"
        )
        assert legacy != _PERMANENT_DEBT, "the label interpolation is no longer in the statement"

        with pytest.raises(psycopg.errors.SyntaxError) as broken:
            box.admin.execute(legacy, parameters)

        # The same rows are still readable through the shipped statement: the failure is
        # the label's, not the read's.
        shipped = _rows(
            box, _PERMANENT_DEBT, {"debt_class": ABANDONED_HOLDING_CAPACITY, **parameters}
        )

    assert "Brien" in str(broken.value)
    assert [row["job_id"] for row in shipped] == [box.job_id]
