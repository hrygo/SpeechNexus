"""One transactional catalogue version and its visibility tombstones.

The catalogue revision is not derived from a mutable row count.  A single state
row carries the last version, and every insert that can change the public
catalogue advances it in the same transaction.  Retirements are separate
tombstones so publication rows stay immutable and an exact revision remains
readable after it leaves the default catalogue.
"""

STATE_TABLE = "speech_catalog_state"
RETIREMENT_TABLE = "speech_publication_retirements"
PUBLICATION_REVISION_IDENTITY = "speech_publications_revision_identity_key"
ADVANCE_FUNCTION = "speech_catalog_advance_revision"
PUBLICATION_TRIGGER = "speech_publications_advance_catalog_revision"
RETIREMENT_TRIGGER = "speech_publication_retirements_advance_catalog_revision"

_IDENTIFIER = r"'^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"

CATALOG_REVISION_DDL = (
    f"""ALTER TABLE speech_publications
    ADD CONSTRAINT {PUBLICATION_REVISION_IDENTITY}
    UNIQUE (kind, object_id, revision, revision_id)""",
    f"""CREATE TABLE {STATE_TABLE} (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        revision BIGINT NOT NULL CHECK (revision >= 1),
        generated_at_us BIGINT NOT NULL
            CHECK (generated_at_us BETWEEN 0 AND 253402300799999999)
    )""",
    f"""INSERT INTO {STATE_TABLE} (singleton, revision, generated_at_us)
    SELECT TRUE, COUNT(*) + 1, COALESCE(MAX(published_at_us), 0)
    FROM speech_publications""",
    f"""CREATE TABLE {RETIREMENT_TABLE} (
        kind VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (kind IN ('profile', 'binding', 'voice', 'pronunciation')),
        object_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (object_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{{0,18}}$'),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (revision_id ~ {_IDENTIFIER}),
        retired_at_us BIGINT NOT NULL
            CHECK (retired_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (kind, object_id, revision),
        FOREIGN KEY (kind, object_id, revision, revision_id)
            REFERENCES speech_publications (kind, object_id, revision, revision_id)
    )""",
    f"""CREATE FUNCTION {ADVANCE_FUNCTION}() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        changed_at_us BIGINT;
    BEGIN
        changed_at_us := (to_jsonb(NEW) ->> TG_ARGV[0])::bigint;
        UPDATE {STATE_TABLE}
           SET revision = revision + 1,
               generated_at_us = GREATEST(generated_at_us, changed_at_us);
        IF NOT FOUND THEN
            RAISE EXCEPTION 'catalogue revision state is missing';
        END IF;
        RETURN NULL;
    END;
    $$""",
    f"""CREATE TRIGGER {PUBLICATION_TRIGGER}
    AFTER INSERT ON speech_publications
    FOR EACH ROW EXECUTE FUNCTION {ADVANCE_FUNCTION}('published_at_us')""",
    f"""CREATE TRIGGER {RETIREMENT_TRIGGER}
    AFTER INSERT ON {RETIREMENT_TABLE}
    FOR EACH ROW EXECUTE FUNCTION {ADVANCE_FUNCTION}('retired_at_us')""",
)

RETIREMENTS_DOWNGRADE_MESSAGE = "Publication retirements require preservation"
REVISION_DOWNGRADE_MESSAGE = "Catalogue revision state cannot be reconstructed"
