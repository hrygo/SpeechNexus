-- SpeechNexus ADR 0016 canonical schema revision 0003 (PostgreSQL first release).
-- Apply after 0002 with the deployment role. This revision promotes the objects the
-- claim path owns into the canonical schema:
--
--   * coordination_jobs        - the per-Job authority row: claim owner/expiry/version,
--                                bucket assignment and the per-Job event head.
--   * coordination_claim_buckets - fixed-number buckets that replace SKIP LOCKED.
--   * job_events               - continuous (job_id, event_seq), the only public SSE order.
--   * coordination_outbox      - reliable delivery and de-duplication, not event order.
--
-- Claim expiry is UTC TIMESTAMP(6); the draft schema stored epoch microseconds. The
-- conversion belongs to the data migration (#1102), not to this schema definition.

CREATE TABLE coordination_jobs (
    job_id VARCHAR(128) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    attempt_id VARCHAR(128) NOT NULL,
    request_digest VARCHAR(71) NOT NULL,
    bucket_no DECIMAL(38,0) NOT NULL,
    due_at TIMESTAMP(6) NOT NULL,
    job_state VARCHAR(24) NOT NULL,
    claim_owner_id VARCHAR(128),
    claim_expires_at TIMESTAMP(6),
    claim_version DECIMAL(38,0) NOT NULL,
    last_event_seq DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (job_id),
    UNIQUE (operation_id, attempt_id)
);

CREATE TABLE coordination_claim_buckets (
    bucket_no DECIMAL(38,0) NOT NULL,
    claim_owner_id VARCHAR(128),
    lease_until TIMESTAMP(6),
    version DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (bucket_no)
);

CREATE TABLE job_events (
    job_id VARCHAR(128) NOT NULL,
    event_seq DECIMAL(38,0) NOT NULL,
    event_digest VARCHAR(71) NOT NULL,
    payload_ref VARCHAR(512),
    PRIMARY KEY (job_id, event_seq),
    FOREIGN KEY (job_id) REFERENCES coordination_jobs (job_id)
);

CREATE TABLE coordination_outbox (
    event_id VARCHAR(128) NOT NULL,
    job_id VARCHAR(128) NOT NULL,
    event_digest VARCHAR(71) NOT NULL,
    delivered_at TIMESTAMP(6),
    PRIMARY KEY (event_id),
    FOREIGN KEY (job_id) REFERENCES coordination_jobs (job_id)
);

CREATE INDEX coordination_jobs_bucket_due
    ON coordination_jobs (bucket_no, job_state, due_at, job_id);

DELETE FROM schema_meta WHERE meta_id = 'portable';

INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint, authority_epoch)
VALUES ('portable', '0003', '64c491b544b7e5ed24c1e424e83be316e803c142b5345c405a52288fde071343', 1);
