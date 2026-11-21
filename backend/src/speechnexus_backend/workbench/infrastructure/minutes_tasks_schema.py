"""Owner-only task fragment, after workbench versions/deletion/sealing fragments."""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def minutes_tasks_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_minutes_runtime_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.minutes_intents, {s}.minutes_tasks, {s}.minutes_attempts,
        {s}.minutes_request_keys, {s}.minutes_retry_keys TO {r};
    GRANT UPDATE (status, attempt, attempt_token, version, updated_at, result_text, error_code)
        ON {s}.minutes_tasks TO {r};
    GRANT UPDATE (outcome, finished_at, input_tokens, output_tokens,
        completion_digest, completion_confirmed) ON {s}.minutes_attempts TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


def grant_minutes_cleanup_sql(schema: str, role: str) -> sql.Composed:
    # The source FK cascade invokes the guard as cleanup role. It may inspect
    # request completion facts but cannot forge them or delete a task directly.
    return sql.SQL("GRANT SELECT ON {s}.minutes_attempts TO {r}").format(
        s=schema_identifier(schema), r=sql.Identifier(role)
    )


_DDL = """
CREATE TABLE {s}.minutes_intents (
    task_id text PRIMARY KEY CHECK (char_length(task_id) BETWEEN 1 AND 128),
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    transcript_id text NOT NULL,
    source_revision numeric(19,0) NOT NULL CHECK (source_revision > 0),
    source_revision_id text NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    profile_sha256 text NOT NULL CHECK (profile_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    binding_id text NOT NULL CHECK (binding_id ~ '^sha256:[0-9a-f]{{64}}$'),
    model text NOT NULL CHECK (char_length(model) BETWEEN 1 AND 128),
    template_revision numeric(19,0) NOT NULL CHECK (template_revision > 0),
    max_input_characters integer NOT NULL CHECK (max_input_characters BETWEEN 1 AND 1000000),
    max_output_characters integer NOT NULL CHECK (max_output_characters BETWEEN 1 AND 1000000),
    max_output_tokens integer NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 32768),
    created_at timestamptz NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$')
);
CREATE TABLE {s}.minutes_tasks (
    id text PRIMARY KEY REFERENCES {s}.minutes_intents(task_id),
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    project_id text NOT NULL REFERENCES {s}.projects(id),
    transcript_id text NOT NULL,
    source_revision numeric(19,0) NOT NULL,
    source_revision_id text NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    profile_sha256 text NOT NULL CHECK (profile_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    binding_id text NOT NULL CHECK (binding_id ~ '^sha256:[0-9a-f]{{64}}$'),
    profile jsonb NOT NULL CHECK (jsonb_typeof(profile) = 'object'
        AND octet_length(profile::text) <= 128000),
    status text NOT NULL CHECK (status IN
        ('queued','running','succeeded','failed','cancelled','expired')),
    attempt integer NOT NULL CHECK (attempt >= 0),
    attempt_token text,
    version numeric(19,0) NOT NULL CHECK (version BETWEEN 1 AND 9999999999999999999),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    result_text text CHECK (char_length(result_text) BETWEEN 1 AND 1000000),
    error_code text CHECK (error_code IN ('unavailable','invalid_response','interrupted')),
    FOREIGN KEY (transcript_id, source_revision)
        REFERENCES {s}.transcript_revisions(transcript_id, revision) ON DELETE CASCADE,
    CHECK ((status = 'succeeded') = (result_text IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK (status <> 'running' OR (attempt > 0 AND attempt_token IS NOT NULL))
);
CREATE INDEX minutes_source ON {s}.minutes_tasks(transcript_id, source_revision);
CREATE INDEX minutes_project ON {s}.minutes_tasks(project_id);
CREATE INDEX minutes_queue ON {s}.minutes_tasks(workspace_id, status, created_at, id);

CREATE TABLE {s}.minutes_attempts (
    task_id text NOT NULL REFERENCES {s}.minutes_intents(task_id),
    number integer NOT NULL CHECK (number > 0),
    token text NOT NULL UNIQUE,
    started_at timestamptz NOT NULL,
    finished_at timestamptz CHECK (finished_at >= started_at),
    outcome text NOT NULL CHECK (outcome IN ('running','succeeded','failed','interrupted')),
    input_tokens bigint CHECK (input_tokens BETWEEN 0 AND 1000000000),
    output_tokens bigint CHECK (output_tokens BETWEEN 0 AND 1000000000),
    completion_digest text CHECK (completion_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    completion_confirmed boolean NOT NULL DEFAULT false,
    PRIMARY KEY (task_id, number),
    CHECK ((input_tokens IS NULL) = (output_tokens IS NULL)),
    CHECK ((outcome = 'running') = (finished_at IS NULL)),
    CHECK ((outcome = 'running') = (completion_digest IS NULL)),
    CHECK (NOT completion_confirmed OR outcome = 'succeeded')
);
CREATE TABLE {s}.minutes_request_keys (
    workspace_id text NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    task_id text NOT NULL REFERENCES {s}.minutes_intents(task_id),
    PRIMARY KEY (workspace_id, key_digest)
);
CREATE TABLE {s}.minutes_retry_keys (
    task_id text NOT NULL REFERENCES {s}.minutes_intents(task_id),
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    expected_attempt integer NOT NULL CHECK (expected_attempt >= 0),
    PRIMARY KEY (task_id, key_digest)
);

CREATE FUNCTION {s}.guard_minutes_task_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM {s}.minutes_attempts a
        WHERE a.task_id = OLD.id AND NOT a.completion_confirmed) THEN
        RAISE EXCEPTION 'LLM request completion unconfirmed'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$guard$;
CREATE TRIGGER minutes_task_delete_guard BEFORE DELETE ON {s}.minutes_tasks
    FOR EACH ROW EXECUTE FUNCTION {s}.guard_minutes_task_delete();
"""
