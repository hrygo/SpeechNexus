"""Least-privilege DCL for the synchronous admission barrier.

这些是部署期的授权语句:`GRANT … ON "schema".table TO "role"` 里的 schema 与 role 是标识符,
不能绑定成参数,所以它们在物理 profile 的 ``deploy`` 类别里登记,而不属于运行期 DML。
与 ``qualification_admission_grants`` / ``builtin_voice_grants`` 同一约定:授权生产者单独成文件,
运行时模块不携带 DCL。
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    coordination_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def binding_disable_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Least-privilege provisioning for the synchronous admission barrier."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT SELECT (binding_id, revision_id) ON "{schema}".speech_binding_revisions '
        f'TO "{role}"',
        f"GRANT SELECT (kind, object_id, revision_id, mode) "
        f'ON "{schema}".speech_publication_retirements TO "{role}"',
    )


def tts_voice_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Read only the publication identities needed under the voice lock."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f"GRANT SELECT (kind, object_id, revision, revision_id) "
        f'ON "{schema}".speech_publications TO "{role}"',
        f"GRANT SELECT (kind, object_id, revision, revision_id) "
        f'ON "{schema}".speech_publication_retirements TO "{role}"',
        # New TTS admission keeps the voice identity's row lock until commit, so this
        # identity needs the keyed lock surface: without it the first admission of a
        # voice is a 42501, not a slower admission.
        *coordination_keyed_lock_grants(schema, role),
    )
