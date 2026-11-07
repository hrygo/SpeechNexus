-- Read-only preflight for the #537 public Job upgrade.
-- Lists published ASR/TTS bindings whose frozen definition lacks execution_defaults
-- for a task it claims to support. Run before deploying an A2-capable API:
--   psql "$SPEECHNEXUS_DATABASE_URL" -f scripts/dev-env/check-public-job-execution-defaults.sql
WITH published AS (
    SELECT DISTINCT
        p.object_id AS binding_id,
        p.revision::text AS revision,
        p.revision_id,
        r.definition_json::jsonb AS definition
    FROM speech_publications AS p
    JOIN speech_binding_revisions AS r
      ON r.binding_id = p.object_id
     AND r.revision = p.revision
     AND r.revision_id = p.revision_id
    WHERE p.kind = 'binding'
), missing AS (
    SELECT
        binding_id,
        revision,
        revision_id,
        ARRAY_REMOVE(ARRAY[
            CASE WHEN definition->'tasks' ? 'tts'
                  AND NOT (definition->'execution_defaults' ? 'tts')
                 THEN 'tts' END,
            CASE WHEN definition->'tasks' ? 'asr'
                  AND NOT (definition->'execution_defaults' ? 'asr')
                 THEN 'asr' END
        ], NULL) AS missing_fields
    FROM published
    WHERE definition ? 'tasks'
)
SELECT binding_id, revision, revision_id, missing_fields
FROM missing
WHERE cardinality(missing_fields) > 0
ORDER BY binding_id, revision;
