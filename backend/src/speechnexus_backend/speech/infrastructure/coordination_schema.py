"""Reviewed storage foundation for coordination owners; no application transactions.

0002 freezes the remaining thirteen table shapes from the ADR 0006 draft,
reviewed with C identifier comparison and nonnegative required timestamps.
The draft/test inputs are never loaded by product code or deployed migrations.
Keep these versioned DDL statements immutable; changes need a new migration.
"""

COORDINATION_DDL: tuple[str, ...] = (
    """CREATE TABLE resource_pools (
    pool_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    service_class VARCHAR(32) COLLATE "C" NOT NULL,
    pool_state VARCHAR(24) COLLATE "C" NOT NULL,
    owner_id VARCHAR(128) COLLATE "C",
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    owner_expires_at_us BIGINT NOT NULL DEFAULT 0 CHECK (owner_expires_at_us >= 0),
    owner_state VARCHAR(32) COLLATE "C" NOT NULL DEFAULT 'reconciling_takeover',
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1)
)""",
    """CREATE TABLE workspace_quotas (
    workspace_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    max_attempts BIGINT NOT NULL CHECK (max_attempts >= 0),
    held_attempts BIGINT NOT NULL DEFAULT 0 CHECK (held_attempts >= 0),
    quarantined_attempts BIGINT NOT NULL DEFAULT 0 CHECK (quarantined_attempts >= 0),
    policy_revision VARCHAR(128) COLLATE "C" NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    CHECK (held_attempts <= max_attempts - quarantined_attempts)
)""",
    """CREATE TABLE host_nodes (
    host_node_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    memory_architecture VARCHAR(32) COLLATE "C" NOT NULL,
    node_state VARCHAR(24) COLLATE "C" NOT NULL,
    topology_digest VARCHAR(71) COLLATE "C" NOT NULL,
    quarantine_threshold_ppm BIGINT NOT NULL DEFAULT 250000,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    allocatable_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_unified_physical_memory_bytes >= 0),
    allocatable_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_device_memory_bytes >= 0),
    allocatable_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_host_memory_bytes >= 0),
    allocatable_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_cpu_decode_millicores >= 0),
    allocatable_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_host_pcm_buffer_bytes >= 0),
    allocatable_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (allocatable_scratch_disk_bytes >= 0),
    held_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (held_unified_physical_memory_bytes >= 0),
    held_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (held_device_memory_bytes >=
        0),
    held_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (held_host_memory_bytes >= 0),
    held_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK (held_cpu_decode_millicores
        >= 0),
    held_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK (held_host_pcm_buffer_bytes
        >= 0),
    held_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK (held_scratch_disk_bytes >= 0),
    quarantined_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_unified_physical_memory_bytes >= 0),
    quarantined_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_device_memory_bytes >= 0),
    quarantined_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_host_memory_bytes >= 0),
    quarantined_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_cpu_decode_millicores >= 0),
    quarantined_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_host_pcm_buffer_bytes >= 0),
    quarantined_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (quarantined_scratch_disk_bytes >= 0),
    CHECK (held_unified_physical_memory_bytes <= allocatable_unified_physical_memory_bytes -
        quarantined_unified_physical_memory_bytes),
    CHECK (held_device_memory_bytes <= allocatable_device_memory_bytes -
        quarantined_device_memory_bytes),
    CHECK (held_host_memory_bytes <= allocatable_host_memory_bytes -
        quarantined_host_memory_bytes),
    CHECK (held_cpu_decode_millicores <= allocatable_cpu_decode_millicores -
        quarantined_cpu_decode_millicores),
    CHECK (held_host_pcm_buffer_bytes <= allocatable_host_pcm_buffer_bytes -
        quarantined_host_pcm_buffer_bytes),
    CHECK (held_scratch_disk_bytes <= allocatable_scratch_disk_bytes -
        quarantined_scratch_disk_bytes),
    CHECK (quarantine_threshold_ppm BETWEEN 0 AND 1000000)
)""",
    """CREATE TABLE engine_instances (
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL CHECK (instance_generation >= 1),
    host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    pool_id VARCHAR(128) COLLATE "C" NOT NULL,
    gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
    binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
    manifest_id VARCHAR(128) COLLATE "C" NOT NULL,
    approved_template_id VARCHAR(128) COLLATE "C" NOT NULL,
    management_mode VARCHAR(24) COLLATE "C" NOT NULL,
    loading_policy VARCHAR(24) COLLATE "C" NOT NULL,
    config_digest VARCHAR(71) COLLATE "C" NOT NULL,
    instance_state VARCHAR(24) COLLATE "C" NOT NULL,
    max_slots BIGINT NOT NULL CHECK (max_slots >= 1),
    realtime_reserved_slots BIGINT NOT NULL DEFAULT 0 CHECK (realtime_reserved_slots >= 0),
    held_slots BIGINT NOT NULL DEFAULT 0 CHECK (held_slots >= 0),
    quarantined_slots BIGINT NOT NULL DEFAULT 0 CHECK (quarantined_slots >= 0),
    base_budget_kind VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'peak',
    ready_at_us BIGINT,
    stopped_at_us BIGINT,
    quarantine_reason VARCHAR(2000) COLLATE "C",
    last_fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (last_fencing_token >= 0),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    peak_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (peak_unified_physical_memory_bytes >= 0),
    peak_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (peak_device_memory_bytes >=
        0),
    peak_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (peak_host_memory_bytes >= 0),
    peak_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK (peak_cpu_decode_millicores
        >= 0),
    peak_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK (peak_host_pcm_buffer_bytes
        >= 0),
    peak_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK (peak_scratch_disk_bytes >= 0),
    steady_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (steady_unified_physical_memory_bytes >= 0),
    steady_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (steady_device_memory_bytes
        >= 0),
    steady_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (steady_host_memory_bytes >=
        0),
    steady_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK
        (steady_cpu_decode_millicores >= 0),
    steady_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (steady_host_pcm_buffer_bytes >= 0),
    steady_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK (steady_scratch_disk_bytes >=
        0),
    per_slot_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (per_slot_unified_physical_memory_bytes >= 0),
    per_slot_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (per_slot_device_memory_bytes >= 0),
    per_slot_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (per_slot_host_memory_bytes
        >= 0),
    per_slot_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK
        (per_slot_cpu_decode_millicores >= 0),
    per_slot_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (per_slot_host_pcm_buffer_bytes >= 0),
    per_slot_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK (per_slot_scratch_disk_bytes
        >= 0),
    PRIMARY KEY (instance_id, instance_generation),
    FOREIGN KEY (host_node_id) REFERENCES host_nodes (host_node_id),
    FOREIGN KEY (pool_id) REFERENCES resource_pools (pool_id),
    UNIQUE (instance_id, instance_generation, host_node_id, pool_id, gateway_id),
    CHECK (held_slots <= max_slots - quarantined_slots),
    CHECK (realtime_reserved_slots <= max_slots)
)""",
    """CREATE INDEX idx_instances_host_state ON engine_instances (host_node_id, instance_state)""",
    """CREATE INDEX idx_instances_pool_state ON engine_instances (pool_id, instance_state)""",
    """CREATE TABLE runtime_instance_heads (
    instance_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    current_generation BIGINT NOT NULL,
    generation_high_water BIGINT NOT NULL,
    current_prepare_intent_id VARCHAR(128) COLLATE "C",
    current_stop_intent_id VARCHAR(128) COLLATE "C",
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (instance_id, current_generation)
        REFERENCES engine_instances (instance_id, instance_generation),
    CHECK (generation_high_water >= current_generation)
)""",
    """CREATE TABLE runtime_instance_intents (
    target_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    intent_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL,
    action VARCHAR(16) COLLATE "C" NOT NULL,
    request_digest VARCHAR(71) COLLATE "C" NOT NULL,
    request_document TEXT NOT NULL,
    intent_state VARCHAR(24) COLLATE "C" NOT NULL,
    stop_evidence_document TEXT,
    error_code VARCHAR(64) COLLATE "C",
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (target_node_id, intent_id),
    FOREIGN KEY (target_node_id) REFERENCES host_nodes (host_node_id),
    FOREIGN KEY (instance_id, instance_generation)
        REFERENCES engine_instances (instance_id, instance_generation)
)""",
    """CREATE TABLE admission_attempts (
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
    binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
    manifest_id VARCHAR(128) COLLATE "C" NOT NULL,
    recipe_digest VARCHAR(71) COLLATE "C" NOT NULL,
    policy_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
    service_class VARCHAR(32) COLLATE "C" NOT NULL,
    request_digest VARCHAR(71) COLLATE "C" NOT NULL,
    required_stage_ids TEXT NOT NULL,
    quota_state VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'held',
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (operation_id, attempt_id),
    FOREIGN KEY (workspace_id) REFERENCES workspace_quotas (workspace_id)
)""",
    """CREATE TABLE admission_leases (
    lease_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    stage_id VARCHAR(128) COLLATE "C" NOT NULL,
    host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL,
    pool_id VARCHAR(128) COLLATE "C" NOT NULL,
    gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    reserved_slots BIGINT NOT NULL CHECK (reserved_slots >= 1),
    lease_state VARCHAR(24) COLLATE "C" NOT NULL DEFAULT 'reserved',
    cleanup_state VARCHAR(24) COLLATE "C" NOT NULL DEFAULT 'pending',
    deadline_at_us BIGINT NOT NULL CHECK (deadline_at_us >= 0),
    quarantine_observed_at_us BIGINT,
    quarantine_reason_code VARCHAR(64) COLLATE "C",
    budget_released_at_us BIGINT,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    reserved_unified_physical_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (reserved_unified_physical_memory_bytes >= 0),
    reserved_device_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (reserved_device_memory_bytes >= 0),
    reserved_host_memory_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_host_memory_bytes
        >= 0),
    reserved_cpu_decode_millicores BIGINT NOT NULL DEFAULT 0 CHECK
        (reserved_cpu_decode_millicores >= 0),
    reserved_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0 CHECK
        (reserved_host_pcm_buffer_bytes >= 0),
    reserved_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_scratch_disk_bytes
        >= 0),
    UNIQUE (operation_id, attempt_id, stage_id),
    UNIQUE (lease_id, operation_id, attempt_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES admission_attempts (operation_id, attempt_id),
    FOREIGN KEY (instance_id, instance_generation, host_node_id, pool_id, gateway_id)
        REFERENCES engine_instances
            (instance_id, instance_generation, host_node_id, pool_id, gateway_id)
)""",
    """CREATE INDEX idx_leases_host_state ON admission_leases (host_node_id, lease_state)""",
    """CREATE INDEX idx_leases_instance_state ON admission_leases
    (instance_id, instance_generation, lease_state)""",
    """CREATE TABLE gateway_fencing_barriers (
    gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
    pool_id VARCHAR(128) COLLATE "C" NOT NULL,
    min_fencing_token BIGINT NOT NULL CHECK (min_fencing_token >= 1),
    sealed_evidence_digest VARCHAR(71) COLLATE "C" NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (gateway_id, pool_id),
    FOREIGN KEY (pool_id) REFERENCES resource_pools (pool_id)
)""",
    """CREATE TABLE submit_intents (
    submit_intent_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    lease_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    command_digest VARCHAR(71) COLLATE "C" NOT NULL,
    request_digest VARCHAR(71) COLLATE "C" NOT NULL,
    request_document TEXT,
    command_redacted_at_us BIGINT,
    command_deletion_job_id VARCHAR(128) COLLATE "C",
    command_deletion_evidence_digest VARCHAR(71) COLLATE "C",
    voice_revision_id VARCHAR(128) COLLATE "C",
    deadline_at_us BIGINT NOT NULL CHECK (deadline_at_us >= 0),
    recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us >= 0),
    updated_at_us BIGINT NOT NULL CHECK (updated_at_us >= 0),
    execution_state VARCHAR(32) COLLATE "C" NOT NULL DEFAULT 'submitting',
    cleanup_state VARCHAR(24) COLLATE "C" NOT NULL DEFAULT 'pending',
    engine_request_id TEXT,
    dispatched_at_us BIGINT,
    staging_output_object_key VARCHAR(512) COLLATE "C",
    terminated_at_us BIGINT,
    output_stream_id VARCHAR(128) COLLATE "C",
    sample_rate_hz BIGINT CHECK (sample_rate_hz BETWEEN 1 AND 384000),
    hard_output_ceiling_samples BIGINT,
    generated_pcm_samples BIGINT NOT NULL DEFAULT 0 CHECK (generated_pcm_samples >= 0),
    error_code VARCHAR(64) COLLATE "C",
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (lease_id, operation_id, attempt_id)
        REFERENCES admission_leases (lease_id, operation_id, attempt_id),
    UNIQUE (submit_intent_id, operation_id, attempt_id, output_stream_id),
    CHECK (hard_output_ceiling_samples IS NULL
        OR generated_pcm_samples <= hard_output_ceiling_samples),
    CHECK (
        (request_document IS NOT NULL AND command_redacted_at_us IS NULL
            AND command_deletion_job_id IS NULL AND command_deletion_evidence_digest IS
        NULL)
        OR (request_document IS NULL AND command_redacted_at_us IS NOT NULL
            AND command_deletion_job_id IS NOT NULL AND command_deletion_evidence_digest IS
        NOT NULL)
    ),
    CHECK (
        (sample_rate_hz IS NULL AND output_stream_id IS NULL
            AND hard_output_ceiling_samples IS NULL AND generated_pcm_samples = 0)
        OR (sample_rate_hz IS NOT NULL AND output_stream_id IS NOT NULL)
    )
)""",
    """CREATE TABLE output_stream_heads (
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    output_stream_id VARCHAR(128) COLLATE "C" NOT NULL,
    sample_rate_hz BIGINT NOT NULL CHECK (sample_rate_hz BETWEEN 1 AND 384000),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (operation_id, attempt_id, output_stream_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES admission_attempts (operation_id, attempt_id)
)""",
    """CREATE TABLE execution_stop_evidence (
    evidence_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    submit_intent_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
    evidence_kind VARCHAR(64) COLLATE "C" NOT NULL,
    evidence_digest VARCHAR(71) COLLATE "C" NOT NULL,
    observed_at_us BIGINT NOT NULL CHECK (observed_at_us >= 0),
    reason VARCHAR(2000) COLLATE "C",
    execution_stopped BIGINT NOT NULL CHECK (execution_stopped = 1),
    resources_released BIGINT NOT NULL CHECK (resources_released = 1),
    FOREIGN KEY (submit_intent_id) REFERENCES submit_intents (submit_intent_id)
)""",
    """CREATE TABLE output_sample_intervals (
    interval_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
    submit_intent_id VARCHAR(128) COLLATE "C" NOT NULL,
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    output_stream_id VARCHAR(128) COLLATE "C" NOT NULL,
    meter_kind VARCHAR(16) COLLATE "C" NOT NULL,
    sample_start BIGINT NOT NULL CHECK (sample_start >= 0),
    sample_end BIGINT NOT NULL,
    PRIMARY KEY (operation_id, attempt_id, output_stream_id, meter_kind, sample_start),
    FOREIGN KEY (submit_intent_id, operation_id, attempt_id, output_stream_id)
        REFERENCES submit_intents
            (submit_intent_id, operation_id, attempt_id, output_stream_id),
    FOREIGN KEY (operation_id, attempt_id, output_stream_id)
        REFERENCES output_stream_heads (operation_id, attempt_id, output_stream_id),
    CHECK (sample_end > sample_start)
)""",
)
