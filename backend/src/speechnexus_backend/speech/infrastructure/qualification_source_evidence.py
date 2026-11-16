"""One admission decision about where a published builtin qualification came from.

A qualification ledger row says *what* was published; the provenance row beside it
says *where that fact came from*. Reading only the former is what let an operator
identity with no controller observation and no accepted declaration produce a voice
that synchronous TTS and the public Job surface both treated as qualified while the
audit read surface called it `unverified`. Three decision points read the same
predicate here instead of three hand-written copies that drift apart (the
gate/observation path already paid for one such drift).

Admission requires positive evidence, with no exception: the probe authorization a
controller observation must name, or an operator attestation inside its declared
window. A qualification row with no provenance row is refused as well. That case
looks like history, but the provenance row is a child table with no database-level
"the qualification must have one" constraint, and writing a qualification row is a
required permission of the publishing identity -- so an unprovenanced row is exactly
what a hand-published forgery looks like, and "no provenance row" only means "not
written by the publish command". Allowing it would leave the one hole an attacker
can reach with the minimum privilege set. A real pre-0055 row is restored by
publishing the voice again through either source, which is the same recovery an
operator would use for any other evidence gap, and the audit read surface still
labels those rows `legacy_pre_0055`.
"""

# The internal admission refusal for "a qualification row exists but nothing
# evidences its source". It maps to the existing public `engine_resource_disabled`
# (see `speech/domain/admission.py`): the fix is a deployment-side re-declaration,
# not a caller correction or a retry.
SOURCE_UNVERIFIED = "qualification_source_unverified"

# The states the predicate distinguishes. Only ``EVIDENCED`` is accepted; the rest
# exist to name the concrete repair, because "no backing evidence" alone does not
# tell an operator whose declaration elapsed from whose row was never evidence-backed.
STATE_EVIDENCED = "evidenced"
STATE_ABSENT = "absent"
STATE_UNVERIFIED = "unverified"
STATE_CONTROLLER_WITHOUT_PROBE = "controller_without_probe"
STATE_OPERATOR_WITHOUT_ATTESTATION = "operator_without_attestation"
STATE_OPERATOR_ELAPSED = "operator_elapsed"

_NOW_US = "floor(extract(epoch FROM clock_timestamp())*1000000)::bigint"


def qualification_source_state_sql(qualifications: str, *, now_us: str = _NOW_US) -> str:
    """SQL expression over one qualification row reference: which state is it in?

    ``qualifications`` is the table name or alias the caller's query already uses
    (``q`` in the admission lookup, the bare table name in the Job fallback).
    ``now_us`` defaults to the database clock in microseconds (the attestation
    columns are bigint microseconds), so an elapsed declaration stops admitting work
    instead of staying usable forever.
    """
    provenance = "speech_builtin_voice_qualification_provenance"
    columns = f"p.voice_id={qualifications}.voice_id AND p.revision={qualifications}.revision"
    return (
        "CASE WHEN NOT EXISTS (SELECT 1 FROM "
        f"{provenance} p WHERE {columns}) THEN '{STATE_ABSENT}'"
        " WHEN EXISTS (SELECT 1 FROM "
        f"{provenance} p WHERE {columns} AND p.source_kind='controller_observed' "
        "AND p.probe_authorization_id IS NOT NULL) THEN '" + STATE_EVIDENCED + "'"
        " WHEN EXISTS (SELECT 1 FROM "
        f"{provenance} p JOIN speech_builtin_voice_operator_attestations t "
        "ON t.attestation_id=p.attestation_id "
        f"WHERE {columns} AND p.source_kind='operator_attested' "
        f"AND t.observed_at_us <= {now_us} AND {now_us} < t.expires_at_us) "
        f"THEN '{STATE_EVIDENCED}'"
        " WHEN EXISTS (SELECT 1 FROM "
        f"{provenance} p WHERE {columns} AND p.source_kind='controller_observed') "
        f"THEN '{STATE_CONTROLLER_WITHOUT_PROBE}'"
        " WHEN EXISTS (SELECT 1 FROM "
        f"{provenance} p JOIN speech_builtin_voice_operator_attestations t "
        "ON t.attestation_id=p.attestation_id "
        f"WHERE {columns} AND p.source_kind='operator_attested' AND {now_us} >= t.expires_at_us) "
        f"THEN '{STATE_OPERATOR_ELAPSED}'"
        " WHEN EXISTS (SELECT 1 FROM "
        f"{provenance} p WHERE {columns} AND p.source_kind='operator_attested') "
        f"THEN '{STATE_OPERATOR_WITHOUT_ATTESTATION}'"
        f" ELSE '{STATE_UNVERIFIED}' END"
    )


def qualification_source_evidenced_sql(qualifications: str, *, now_us: str = _NOW_US) -> str:
    """SQL predicate over one qualification row reference: is its source evidenced?"""
    return (
        f"({qualification_source_state_sql(qualifications, now_us=now_us)} = '{STATE_EVIDENCED}')"
    )


# The operator-facing sentence per state. Each names the item to fix instead of the
# class of failure, because every public answer for it is an existing code whose
# default text points elsewhere: "engine_resource_disabled" sends an operator to
# look for a switch, and "voice_not_published" says the voice is not published when
# it plainly is (#877, and the same ruling as the Job-surface naming in #880).
_NEW_REVISION = (
    "publish a new voice revision and record its source there (controller probe path or "
    "an accepted operator attestation), then send a new request"
)
_DETAILS: dict[str, str] = {
    STATE_ABSENT: (
        "the published builtin voice has no qualification source recorded: " + _NEW_REVISION
    ),
    STATE_UNVERIFIED: (
        "the published builtin voice's qualification has no backing evidence: " + _NEW_REVISION
    ),
    STATE_CONTROLLER_WITHOUT_PROBE: (
        "the published builtin voice claims a controller observation without the probe "
        "authorization it must name: " + _NEW_REVISION
    ),
    STATE_OPERATOR_WITHOUT_ATTESTATION: (
        "the published builtin voice claims an operator declaration that is not recorded: "
        + _NEW_REVISION
    ),
    STATE_OPERATOR_ELAPSED: (
        "the operator attestation behind the published builtin voice declared an elapsed "
        "window; a window cannot be extended on a published qualification: " + _NEW_REVISION
    ),
}

SOURCE_UNVERIFIED_DETAIL = _DETAILS[STATE_UNVERIFIED]


def source_refusal_detail(state: str) -> str:
    """The sentence that names the concrete repair for one refused source state."""
    return _DETAILS.get(state, _DETAILS[STATE_UNVERIFIED])


__all__ = [
    "SOURCE_UNVERIFIED",
    "SOURCE_UNVERIFIED_DETAIL",
    "STATE_ABSENT",
    "STATE_CONTROLLER_WITHOUT_PROBE",
    "STATE_EVIDENCED",
    "STATE_OPERATOR_ELAPSED",
    "STATE_OPERATOR_WITHOUT_ATTESTATION",
    "STATE_UNVERIFIED",
    "qualification_source_evidenced_sql",
    "qualification_source_state_sql",
    "source_refusal_detail",
]
