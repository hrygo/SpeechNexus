-- SpeechNexus ADR 0016 canonical schema revision 0005 (PostgreSQL first release).
-- Apply after 0004. This revision adds the metered usage facts a realtime gateway appends at
-- segment and utterance boundaries:
--
--   * output_stream_heads - the per stream and meter head. Usage writers lock this row before
--                           they look for overlap, so two writers cannot interleave a partial
--                           overlap check. `stream_id` is the canonical identity of the
--                           (operation, attempt, output_stream) triple the gateway owns.
--   * usage_intervals     - append only, half open `[interval_start, interval_end)`, with the
--                           fact hash of the frozen input or generated samples. Replaying the
--                           same identity returns the original interval; a partial overlap is
--                           rejected even when no unique key would catch it.
--
-- Units are integer samples or codepoints; there is no floating point unit anywhere.

CREATE TABLE output_stream_heads (
    stream_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(24) NOT NULL,
    last_end DECIMAL(38,0) NOT NULL,
    version DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (stream_id, meter_kind)
);

CREATE TABLE usage_intervals (
    interval_id VARCHAR(128) NOT NULL,
    stream_id VARCHAR(128) NOT NULL,
    meter_kind VARCHAR(24) NOT NULL,
    interval_start DECIMAL(38,0) NOT NULL,
    interval_end DECIMAL(38,0) NOT NULL,
    fact_hash VARCHAR(71) NOT NULL,
    units DECIMAL(38,0) NOT NULL,
    PRIMARY KEY (stream_id, meter_kind, interval_start),
    UNIQUE (interval_id),
    FOREIGN KEY (stream_id, meter_kind)
        REFERENCES output_stream_heads (stream_id, meter_kind)
);

DELETE FROM schema_meta WHERE meta_id = 'portable';

INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint, authority_epoch)
VALUES ('portable', '0005', 'd99bfdf39af69477376fbe5ef2f96ac752a82f2b03a3fe4c745e2e961fda4ed6', 1);
