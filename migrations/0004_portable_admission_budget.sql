-- SpeechNexus ADR 0016 canonical schema revision 0004 (PostgreSQL first release).
-- Apply after 0003. This revision adds the persistent admission budget objects:
--
--   * workspace_quotas        - per workspace and meter the budget authority rows a
--                               admission transaction locks, in fixed order.
--   * host_capacity_budgets   - the per host share locked after the workspace quota.
--   * admission_budget_grants - the committed reservation a gateway may spend locally:
--                               session, lease, authority epoch, renewal sequence,
--                               granted / consumed / returned units and expiry.
--
-- Admission locks workspace_quotas, then host_capacity_budgets, then inserts the lease
-- grant, then writes Job and outbox. Every later path must lock in this order.

CREATE TABLE workspace_quotas (
    workspace_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(24) NOT NULL,
    limit_units DECIMAL(38,0) NOT NULL,
    held_units DECIMAL(38,0) NOT NULL,
    committed_units DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (workspace_id, meter_kind)
);

CREATE TABLE host_capacity_budgets (
    host_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(24) NOT NULL,
    limit_units DECIMAL(38,0) NOT NULL,
    held_units DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (host_id, meter_kind)
);

CREATE TABLE admission_budget_grants (
    grant_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(128) NOT NULL,
    lease_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(24) NOT NULL,
    authority_epoch DECIMAL(38,0) NOT NULL,
    renew_seq DECIMAL(38,0) NOT NULL,
    granted_units DECIMAL(38,0) NOT NULL,
    consumed_units DECIMAL(38,0) NOT NULL,
    returned_units DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    grant_state VARCHAR(24) NOT NULL,
    expires_at TIMESTAMP(6) NOT NULL,
    PRIMARY KEY (grant_id),
    UNIQUE (session_id, meter_kind, authority_epoch, renew_seq)
);

DELETE FROM schema_meta WHERE meta_id = 'portable';

INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint, authority_epoch)
VALUES ('portable', '0004', '2e2bc483278b905b37587f0079e17b0234c1938c03ae4d174ae5ad54461b23be', 1);
