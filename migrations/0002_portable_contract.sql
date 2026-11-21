-- SpeechNexus ADR 0016 canonical schema revision 0002 (PostgreSQL first release).
-- Apply with the deployment role to a new database; runtime roles hold DML only and
-- never create tables. Physical DDL may differ from another target's profile in
-- equivalent type spelling, index / constraint syntax and deployment attributes, but
-- the logical facts must equal packages/portable-sql/schema/logical.json.
--
-- `schema_meta` is the single common FROM anchor: schema revision, logical schema
-- fingerprint and authority epoch. It stores no database time; the authority probe
-- reads CURRENT_TIMESTAMP from this row and never persists it.
--
-- The seeded fingerprint is the SHA-256 of the canonical logical schema for this
-- revision. Changing logical.json without promoting a revision turns the schema guard
-- red, which is the intended ratchet.

CREATE TABLE schema_meta (
    meta_id VARCHAR(32) NOT NULL,
    schema_rev VARCHAR(64) NOT NULL,
    schema_fingerprint CHAR(64) NOT NULL,
    authority_epoch DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (meta_id)
);

INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint, authority_epoch)
VALUES ('portable', '0002', '69cf627208c9cafb7ffb496f44c0cf318534a5d7a39125cbaadb1544b669b6fe', 1);
