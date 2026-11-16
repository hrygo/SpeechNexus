"""One admission decision about where a published builtin qualification came from.

A qualification ledger row says *what* was published; the provenance row beside it
says *where that fact came from*. Reading only the former is what let an operator
identity with no controller observation and no accepted declaration produce a voice
that synchronous TTS and the public Job surface both treated as qualified while the
audit read surface called it `unverified`. Three consumers decide "is this voice
usable" -- the qualification lookup behind synchronous TTS admission, the public Job
builtin fallback, and the qualification admission transaction -- so the decision
lives here once instead of three hand-written predicates that drift apart (the
gate/observation path already paid for one such drift).

A row is accepted only with positive evidence: the probe authorization a controller
observation must name, or a live operator attestation. Rows written before the
provenance table existed have no provenance row at all and stay accepted
(grandfathered, and the read surface labels them `legacy_pre_0055`); that is not a
forgeable shape, because the declaration identity holds no `INSERT` on the
provenance table (see `operator_attestation_writer_grants`), so "published with no
provenance" can only be old history, never a new forgery. An `unverified` label --
"published, but no evidence backs the source" -- is therefore refused rather than
trusted, and an `operator_attested` label stops working when its attestation
window closes.
"""

# The internal admission refusal for "a qualification row exists but nothing
# evidences its source". It maps to the existing public `engine_resource_disabled`
# (see `speech/domain/admission.py`): the fix is a deployment-side re-declaration,
# not a caller correction or a retry.
SOURCE_UNVERIFIED = "qualification_source_unverified"

# The operator-facing sentence for that refusal. It names the item to fix instead of
# the class of failure, because the public answer for it is an existing code whose
# default text points at the wrong place: "engine_resource_disabled" sends an
# operator to look for a switch, while the actual missing thing is the qualification
# evidence chain (#877, and the same ruling as the Job-surface naming in #880).
SOURCE_UNVERIFIED_DETAIL = (
    "the builtin voice's qualification has no backing evidence: publish it through "
    "the controller probe path, or record an accepted operator attestation for it, "
    "then send a new request"
)


_NOW_US = "floor(extract(epoch FROM clock_timestamp())*1000000)::bigint"


def qualification_source_evidenced_sql(qualifications: str, *, now_us: str = _NOW_US) -> str:
    """SQL predicate over one qualification row reference: is its source evidenced?

    ``qualifications`` is the table name or alias the caller's query already uses
    (``q`` in the admission lookup, the bare table name in the Job fallback).
    ``now_us`` defaults to the database clock in microseconds (the attestation
    columns are bigint microseconds): an operator attestation is accepted only
    inside ``[observed_at_us, expires_at_us)``, so an expired declaration stops
    admitting work instead of staying usable forever.
    """
    provenance = "speech_builtin_voice_qualification_provenance"
    columns = f"p.voice_id={qualifications}.voice_id AND p.revision={qualifications}.revision"
    controller = "(p.source_kind='controller_observed' AND p.probe_authorization_id IS NOT NULL)"
    operator = (
        "(p.source_kind='operator_attested' AND t.attestation_id IS NOT NULL "
        f"AND t.observed_at_us <= {now_us} AND {now_us} < t.expires_at_us)"
    )
    return (
        f"NOT EXISTS (SELECT 1 FROM {provenance} p WHERE {columns}) OR EXISTS "
        f"(SELECT 1 FROM {provenance} p LEFT JOIN "
        "speech_builtin_voice_operator_attestations t ON t.attestation_id=p.attestation_id "
        f"WHERE {columns} AND ({controller} OR {operator}))"
    )


__all__ = ["SOURCE_UNVERIFIED", "qualification_source_evidenced_sql"]
