"""Resolve usable builtin voices from publication and independent qualification."""

from __future__ import annotations

from typing import Any

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    binding_disable_admission_grants,
    tts_voice_admission_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    qualification_reader_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_builtin_voice,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publication_schema import model_catalog_reader_grants
from speechnexus_backend.speech.infrastructure.speech_facts_postgres import (
    PostgresSpeechFacts,
    _publication,
)
from speechnexus_backend.speech.infrastructure.tts_admission import TtsRecipe
from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
    tts_settlement_writer_grants,
)


def speech_synthesis_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Additional SELECT-only grants on top of coordination runtime permissions."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        *model_catalog_reader_grants(schema, role),
        *qualification_reader_grants(schema, role),
        *binding_disable_admission_grants(schema, role),
        *tts_voice_admission_grants(schema, role),
        *tts_settlement_writer_grants(schema, role),
        f'GRANT SELECT ON "{schema}".speech_workspaces TO "{role}"',
    )


class QualifiedPostgresSpeechFacts(PostgresSpeechFacts):
    """Expose only a published, live-qualified voice for this pinned deployment."""

    def __init__(self, pool: SpeechPool, recipe: TtsRecipe, *, timeout_seconds: float = 3) -> None:
        super().__init__(pool, timeout_seconds=timeout_seconds)
        self._qualified_pool, self._recipe, self._qualified_timeout = pool, recipe, timeout_seconds

    def has_qualified_voice(self) -> bool:
        """A bounded startup gate; each later request is independently rechecked."""
        rows = self._read(
            "SELECT voice_id,revision FROM speech_builtin_voice_qualifications "
            "WHERE binding_revision_id=%s ORDER BY voice_id,revision LIMIT 64",
            (self._recipe.approved.binding_revision_id,),
        )
        return any(self._voice(row["voice_id"], int(row["revision"])) is not None for row in rows)

    def voice_execution_authority(self, voice: Publication) -> tuple[str, str]:
        """Read pinned authority; admission rechecks the same facts under its voice lock."""
        recipe = self._recipe
        binding = recipe.approved.stages[0].runtime_plan.binding
        try:
            with self._qualified_pool.connection(timeout=self._qualified_timeout) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._qualified_timeout * 1000))),),
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT current_generation FROM runtime_instance_heads "
                        "WHERE instance_id=%s",
                        (recipe.instance_id,),
                    )
                    rows = cursor.fetchall()
                    if len(rows) != 1:
                        raise ValueError("missing instance head")
                    facts = require_builtin_voice(
                        cursor,
                        voice_id=voice.object_id,
                        revision=voice.revision,
                        revision_id=voice.revision_id,
                        binding_revision_id=recipe.approved.binding_revision_id,
                        instance_id=recipe.instance_id,
                        instance_generation=int(rows[0]["current_generation"]),
                        host_node_id=binding.target_node_id,
                        gateway_id=binding.gateway_id,
                        policy_revision_id=recipe.approved.policy_revision_id,
                    )
                    return facts["gateway_policy_digest"], facts["native_speaker_id"]
        except Exception:
            raise PublicationUnavailableError("Voice authority is unavailable") from None

    def latest_publication(self, kind: PublicationKind, object_id: str) -> Publication | None:
        if kind == "voice":
            return self._voice(object_id, None)
        return super().latest_publication(kind, object_id)

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None:
        if kind == "voice":
            return self._voice(object_id, revision)
        return super().publication(kind, object_id, revision)

    def _voice(self, object_id: str, revision: int | None) -> Publication | None:
        recipe = self._recipe
        binding = recipe.approved.stages[0].runtime_plan.binding
        try:
            with self._qualified_pool.connection(timeout=self._qualified_timeout) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(self._qualified_timeout * 1000))),),
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT p.kind,p.object_id,p.revision,p.revision_id,p.published_at_us "
                        "FROM speech_publications p WHERE p.kind='voice' AND p.object_id=%s "
                        "AND (%s::text IS NULL OR p.revision=%s) "
                        "AND (%s::text IS NOT NULL OR EXISTS "
                        "(SELECT 1 FROM speech_publication_heads h "
                        "WHERE h.kind=p.kind AND h.object_id=p.object_id AND h.revision=p.revision "
                        "AND h.revision_id=p.revision_id)) "
                        "AND NOT EXISTS (SELECT 1 FROM speech_publication_retirements r "
                        "WHERE r.kind=p.kind AND r.object_id=p.object_id AND r.revision=p.revision "
                        "AND r.revision_id=p.revision_id)",
                        (
                            object_id,
                            None if revision is None else str(revision),
                            None if revision is None else str(revision),
                            None if revision is None else str(revision),
                        ),
                    )
                    rows: list[dict[str, Any]] = cursor.fetchall()
                    if not rows:
                        return None
                    if len(rows) != 1:
                        raise ValueError
                    voice = _publication(rows[0])
                    cursor.execute(
                        "SELECT h.current_generation,i.host_node_id,i.gateway_id,"
                        "i.binding_revision_id,i.manifest_id,i.pool_id "
                        "FROM runtime_instance_heads h JOIN engine_instances i "
                        "ON i.instance_id=h.instance_id "
                        "AND i.instance_generation=h.current_generation "
                        "WHERE h.instance_id=%s",
                        (recipe.instance_id,),
                    )
                    current = cursor.fetchall()
                    if len(current) != 1:
                        return None
                    row = current[0]
                    if (
                        row["host_node_id"],
                        row["gateway_id"],
                        row["pool_id"],
                        row["binding_revision_id"],
                        row["manifest_id"],
                    ) != (
                        binding.target_node_id,
                        binding.gateway_id,
                        binding.pool_id,
                        recipe.approved.binding_revision_id,
                        recipe.approved.manifest_id,
                    ):
                        return None
                    try:
                        require_builtin_voice(
                            cursor,
                            voice_id=voice.object_id,
                            revision=voice.revision,
                            revision_id=voice.revision_id,
                            binding_revision_id=recipe.approved.binding_revision_id,
                            instance_id=recipe.instance_id,
                            instance_generation=int(row["current_generation"]),
                            host_node_id=binding.target_node_id,
                            gateway_id=binding.gateway_id,
                            policy_revision_id=recipe.approved.policy_revision_id,
                        )
                    except AdmissionRejectedError:
                        return None
                    return voice
        except PublicationUnavailableError:
            raise
        except Exception:
            raise PublicationUnavailableError from None
