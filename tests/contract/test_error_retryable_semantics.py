"""One stated meaning for `error.retryable`, and the two readings it does not give (#806).

`#803` made the export and assistant panels decide whether a settled refusal may be
replayed under the original Idempotency-Key from `ErrorBody.error.retryable`. That made a
consumer-facing decision rest on a field the shared schema declared as a bare `boolean`,
while the only statement of what it means lived inside the `generateRenderUnitV2`
operation description -- one paragraph, in one of the documents that publish the same
envelope (speech, realtime, admin and workbench all carry `ErrorBody`).

So the meaning is stated once, on the field, in `contracts/shared/errors.schema.json`, and
this module pins it there: the sentence cannot be deleted, and neither of the two
directions a reader gets wrong can be dropped from it. Both readings are live in the tree
today and both are already blocked in code -- the point of pinning them is that nothing
says *where* the block is.

1. **`503 outcome_unknown` is `retryable: false` and must still be replayed.** The
   workbench answers an unknown mutation outcome that way (`ERRORS["outcome_unknown"]`,
   whose message demands the original key), and the panels do replay it. What prevents the
   field from being read as "do not replay" is not the field but the *settled* guard:
   `recovery/command-journal.ts`'s `refusedCategory(settled, retryable)` returns `null`
   whenever `settled` is false, and the panels compute `settled` as 4xx excluding 408/429.
   A 5xx is never settled, so the category is `null` and `refusedReplayable(null)` keeps
   the key whatever `retryable` says; `recovery/command-journal.test.ts` pins the
   `(false, false)` and `(false, true)` rows. The field's own value stays `false` because
   "may this same dispatch be retried" is a different question from "may an unknown
   outcome be replayed".

2. **`execution_cleanup_pending` is `retryable: false` on the speech Job side while the
   correct action is to dispatch the same intent again.** The Job handler's allow-list
   (`speech/interfaces/http/job_errors.py`) names the three codes whose *unknown outcome*
   may be replayed, and the cleanup wait is not one of them. What prevents the render
   panel from inheriting that answer is the adapter: `_refusal()` keeps status, code,
   param and `Retry-After` only, `RenderRejection` has no `retryable` field, and the panel
   renders the workbench's own disposition table, in which the same wait is retryable
   (`render_submission_cleanup_pending`). Each value is correct *for its own question*, so
   copying either one into the other would be wrong in both directions.

Neither counterexample can be expressed as a schema constraint, so this module asserts the
facts that keep them true: the shared description states both, the 503 stays a 5xx, the
Job side publishes `false` for the cleanup wait, the workbench publishes a retryable
disposition for it, and the adapter cannot carry the upstream field.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, cast

import yaml

from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.workbench.application.render_units import RenderRejection
from speechnexus_backend.workbench.interfaces.http_render_units import (
    refusal_code,
    refusal_retryable,
)
from speechnexus_backend.workbench.interfaces.http_versions import ERRORS

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "contracts" / "shared" / "errors.schema.json"
WORKBENCH = ROOT / "contracts" / "workbench-openapi.yaml"
DISPATCH_PATH = "/v2/workbench/render-units/{unit_id}/generations"
# The phrase the field must state, and the two readings it must rule out where a consumer
# of the envelope reads it.
MEANING = "same dispatch may be retried after the refusal"
# What the shared definition must keep saying: the field is a refusal's own promise, the
# 503 is replayed although it is false, and the Job side computes the same field from a
# narrower question.
REQUIRED_PHRASES = (
    MEANING,
    "Idempotency-Key",
    "outcome_unknown",
    "execution_cleanup_pending",
    "workbench-openapi.yaml",
)


def _flat(text: str) -> str:
    """One line for a folded YAML scalar, so a phrase can be asserted across line breaks."""
    return re.sub(r"\s+", " ", text)


def _shared_schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SHARED.read_text(encoding="utf-8")))


def _retryable_field() -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        _shared_schema()["$defs"]["ErrorBody"]["properties"]["retryable"],
    )


def test_the_shared_field_carries_the_same_dispatch_rule_and_its_two_limits() -> None:
    """The meaning lives on the field, not inside one operation's prose.

    Deleting the description, or dropping either counterexample from it, makes this fail:
    the two panels read this field literally, and the schema is the only place the speech,
    realtime, admin and workbench documents agree on what it means.
    """
    field = _retryable_field()
    # The field stays a plain boolean: #806 adds a statement of meaning, not a new value.
    assert field["type"] == "boolean"
    assert set(field) == {"type", "description"}, field
    description = field.get("description")
    assert isinstance(description, str) and description, (
        "ErrorBody.error.retryable must state what it permits; a bare boolean is what let "
        "#803's panels mine its meaning out of one operation description"
    )
    flat = _flat(description)
    missing = [phrase for phrase in REQUIRED_PHRASES if phrase not in flat]
    assert missing == [], f"the retryable description no longer states {missing}"


def test_every_error_body_still_requires_the_field() -> None:
    """A consumer that reads it must never receive an envelope that omits it."""
    body = _shared_schema()["$defs"]["ErrorBody"]
    assert "retryable" in body["required"]
    assert body["additionalProperties"] is False


def test_the_workbench_dispatch_points_at_the_shared_statement() -> None:
    """The dispatch vocabulary's paragraph is a special case, not the definition."""
    document = yaml.safe_load(WORKBENCH.read_text(encoding="utf-8"))
    description = _flat(
        cast("dict[str, str]", document["paths"][DISPATCH_PATH]["post"])["description"]
    )
    assert MEANING in description
    # The operation must say where the field is defined, or the next reader will treat
    # this paragraph as the whole rule again -- which is the defect #806 records.
    assert "contracts/shared/errors.schema.json" in description
    # And it must state the direction that survives here: its own 503 keeps the key.
    assert "503 outcome_unknown" in description


def test_the_503_that_keeps_its_key_is_not_a_settled_refusal() -> None:
    """The replay of an unknown outcome is outside what the field decides.

    The panels settle only 4xx (minus 408/429), so this status can never reach the field.
    Moving it to a 4xx -- the one edit that would make the field decide the replay -- fails
    here instead of silently withdrawing the replay affordance.
    """
    status, _, message = ERRORS["outcome_unknown"]
    assert status >= 500, (
        "outcome_unknown must stay a 5xx: consumers settle only a 4xx refusal, and the "
        "original key is replayed because the outcome is unknown, not because "
        "error.retryable permits it"
    )
    assert "Preserve the original idempotency key" in message


def test_the_job_side_publishes_false_for_the_cleanup_wait() -> None:
    """The Job field answers "may an unknown outcome be replayed", so this is `false`."""
    response = job_error("execution_cleanup_pending", "rid-806-probe")
    assert response.status_code == 409
    error = json.loads(bytes(response.body))["error"]
    assert error["code"] == "execution_cleanup_pending"
    assert error["retryable"] is False, (
        "the Job handler's allow-list answers a different question; if the cleanup wait "
        "became retryable there, the shared description's counterexample would be wrong"
    )


def test_the_workbench_publishes_retryable_for_the_same_wait() -> None:
    """The same refusal keeps the opposite value where the operator step is stated."""
    rejection = RenderRejection.upstream(
        status_code=409, code="execution_cleanup_pending", param=None
    )
    disposition = refusal_code(rejection)
    assert disposition == "render_submission_cleanup_pending"
    assert refusal_retryable(disposition) is True
    status, _, _ = ERRORS[disposition]
    assert status == 409


def test_the_workbench_adapter_cannot_carry_the_upstream_field() -> None:
    """The consumer table is per-code because the adapter never forwards the field.

    If `RenderRejection` ever gains `retryable`, the two authorities could disagree and
    the cleanup wait would inherit the Job side's `false`.
    """
    names = {field.name for field in dataclasses.fields(RenderRejection)}
    assert "retryable" not in names, names
