"""Observed public Job progress stored in the authoritative event table.

A progress row is not a lifecycle transition and never updates speech_jobs: it
records one observed fact (the durable generated-audio watermark reported by the
execution gateway) for a Job that is still running. The existing
(job_id, event_seq) identity and SSE numbering are reused, so a restart replays
the same ordered facts instead of recomputing progress from current
configuration. A terminal Job cannot receive one, and a repeated or decreasing
value is rejected by the guard rather than stored as a second fact.
"""

EVENTS = "speech_job_events"
JOBS = "speech_jobs"
PROGRESS_INDEX = "speech_job_events_progress_latest"
PROGRESS_GUARD = "speech_job_events_progress_guard"

PUBLIC_JOB_PROGRESS_DDL = (
    f"""ALTER TABLE {EVENTS}
        ADD COLUMN kind VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'lifecycle'""",
    f'ALTER TABLE {EVENTS} ADD COLUMN progress_unit VARCHAR(16) COLLATE "C"',
    f"ALTER TABLE {EVENTS} ADD COLUMN progress_completed BIGINT",
    f"ALTER TABLE {EVENTS} ADD COLUMN progress_total BIGINT",
    f"""ALTER TABLE {EVENTS} ADD CONSTRAINT speech_job_events_kind_check
        CHECK (kind IN ('lifecycle', 'progress'))""",
    f"""ALTER TABLE {EVENTS} ADD CONSTRAINT speech_job_events_progress_shape_check
        CHECK (
            (kind = 'lifecycle'
             AND progress_unit IS NULL
             AND progress_completed IS NULL
             AND progress_total IS NULL)
            OR (kind = 'progress'
                AND state = 'running'
                AND progress_unit IN ('audio_ms', 'text_codepoints', 'items')
                AND progress_completed IS NOT NULL
                AND progress_completed >= 0
                AND (progress_total IS NULL OR progress_total >= progress_completed))
        )""",
    f"""CREATE INDEX {PROGRESS_INDEX} ON {EVENTS} (job_id, event_seq DESC)
        WHERE kind = 'progress'""",
    f"""CREATE FUNCTION speech_job_event_progress_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          job_state VARCHAR(16);
          latest_seq BIGINT;
          latest_completed BIGINT;
        BEGIN
          IF NEW.kind <> 'progress' THEN
            RETURN NEW;
          END IF;
          SELECT state INTO job_state FROM {JOBS} WHERE job_id = NEW.job_id;
          IF job_state IS DISTINCT FROM 'running' THEN
            RAISE EXCEPTION 'progress requires a running public Job'
              USING ERRCODE = '23514';
          END IF;
          SELECT MAX(event_seq) INTO latest_seq FROM {EVENTS}
            WHERE job_id = NEW.job_id;
          SELECT MAX(progress_completed) INTO latest_completed FROM {EVENTS}
            WHERE job_id = NEW.job_id AND kind = 'progress';
          IF NEW.event_seq <> COALESCE(latest_seq, -1) + 1 THEN
            RAISE EXCEPTION 'progress event sequence must follow the stored events'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.progress_completed <= COALESCE(latest_completed, -1) THEN
            RAISE EXCEPTION 'progress must strictly advance the stored fact'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END
        $$""",
    f"""CREATE TRIGGER {PROGRESS_GUARD} BEFORE INSERT ON {EVENTS}
        FOR EACH ROW EXECUTE FUNCTION speech_job_event_progress_guard()""",
)

PUBLIC_JOB_PROGRESS_DROP = (
    f"DROP TRIGGER IF EXISTS {PROGRESS_GUARD} ON {EVENTS}",
    "DROP FUNCTION IF EXISTS speech_job_event_progress_guard()",
    f"DROP INDEX IF EXISTS {PROGRESS_INDEX}",
    f"ALTER TABLE {EVENTS} DROP CONSTRAINT IF EXISTS speech_job_events_progress_shape_check",
    f"ALTER TABLE {EVENTS} DROP CONSTRAINT IF EXISTS speech_job_events_kind_check",
    f"ALTER TABLE {EVENTS} DROP COLUMN IF EXISTS progress_total",
    f"ALTER TABLE {EVENTS} DROP COLUMN IF EXISTS progress_completed",
    f"ALTER TABLE {EVENTS} DROP COLUMN IF EXISTS progress_unit",
    f"ALTER TABLE {EVENTS} DROP COLUMN IF EXISTS kind",
)

