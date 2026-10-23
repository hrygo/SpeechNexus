-- SpeechNexus P0-B Distributed Coordination & Capacity Governance Schema Draft
-- Target: PostgreSQL 16+
-- Alignment: ADR 0004 (Cluster-Ready Architecture & Single-Node MVP Isomorphism),
--            docs/architecture/runtime-governance.md,
--            docs/architecture/technical-blueprint.md
--
-- Table ownership boundaries:
--   1. Speech control plane (`speech` role):
--      - host_nodes
--      - resource_pools
--      - scheduler_pool_leases
--      - engine_instances
--      - runtime_instance_intents
--      - admission_leases
--   2. Execution gateway control plane (`gateway_control` role):
--      - gateway_fencing_barriers
--      - submit_intents
--
-- Lock acquisition order across multi-row transactions:
--   resource_pools -> scheduler_pool_leases -> host_nodes -> engine_instances
--   -> runtime_instance_intents -> admission_leases
--   -> gateway_fencing_barriers -> submit_intents

BEGIN;

-- The interval ledger uses an exclusion constraint, which requires the
-- btree_gist operator classes for scalar equality. The migration role needs
-- permission to install extensions; runtime roles do not.
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

-- ============================================================================
-- 1. host_nodes: Tier-1 Physical / Virtual Compute Node Parent Budget
-- ============================================================================
CREATE TABLE host_nodes (
    host_node_id TEXT PRIMARY KEY,
    host_node_profile_id TEXT NOT NULL,
    memory_architecture TEXT NOT NULL,
    node_state TEXT NOT NULL,
    topology_digest TEXT NOT NULL,

    -- Allocatable parent physical capacities on this node
    allocatable_unified_memory_bytes BIGINT NOT NULL,
    allocatable_device_memory_bytes BIGINT NOT NULL,
    allocatable_host_memory_bytes BIGINT NOT NULL,
    allocatable_cpu_decode_millicores INTEGER NOT NULL,
    allocatable_host_pcm_buffer_bytes BIGINT NOT NULL,
    allocatable_scratch_disk_bytes BIGINT NOT NULL,

    -- Currently reserved budgets across all instances/slots on this node
    reserved_resident_unified_memory_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_peak_loading_unified_memory_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_incremental_unified_memory_bytes BIGINT NOT NULL DEFAULT 0,

    reserved_device_memory_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_host_memory_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_cpu_decode_millicores INTEGER NOT NULL DEFAULT 0,
    reserved_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0,
    reserved_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0,

    quarantined_unified_memory_bytes BIGINT NOT NULL DEFAULT 0,
    quarantined_device_memory_bytes BIGINT NOT NULL DEFAULT 0,
    quarantined_host_memory_bytes BIGINT NOT NULL DEFAULT 0,
    quarantined_cpu_decode_millicores INTEGER NOT NULL DEFAULT 0,
    quarantined_host_pcm_buffer_bytes BIGINT NOT NULL DEFAULT 0,
    quarantined_scratch_disk_bytes BIGINT NOT NULL DEFAULT 0,
    quarantine_threshold_ratio NUMERIC(5, 4) NOT NULL DEFAULT 0.2500,

    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT ck_host_nodes_id_format
        CHECK (host_node_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
    CONSTRAINT ck_host_nodes_memory_arch
        CHECK (memory_architecture IN ('unified_memory', 'discrete_device_memory')),
    -- Unified-memory nodes charge the single physical pool through the unified
    -- counters only. Discrete nodes use device/host counters and leave unified at zero.
    CONSTRAINT ck_host_nodes_memory_architecture_counters
        CHECK (
            (
                memory_architecture = 'unified_memory'
                AND allocatable_device_memory_bytes = 0
                AND allocatable_host_memory_bytes = 0
                AND reserved_device_memory_bytes = 0
                AND reserved_host_memory_bytes = 0
                AND quarantined_device_memory_bytes = 0
                AND quarantined_host_memory_bytes = 0
            )
            OR (
                memory_architecture = 'discrete_device_memory'
                AND allocatable_unified_memory_bytes = 0
                AND reserved_resident_unified_memory_bytes = 0
                AND reserved_peak_loading_unified_memory_bytes = 0
                AND reserved_incremental_unified_memory_bytes = 0
                AND quarantined_unified_memory_bytes = 0
            )
        ),
    CONSTRAINT ck_host_nodes_state
        CHECK (node_state IN ('ready', 'draining', 'quarantined', 'offline')),
    CONSTRAINT ck_host_nodes_non_negative_allocatable
        CHECK (
            allocatable_unified_memory_bytes >= 0
            AND allocatable_device_memory_bytes >= 0
            AND allocatable_host_memory_bytes >= 0
            AND allocatable_cpu_decode_millicores >= 0
            AND allocatable_host_pcm_buffer_bytes >= 0
            AND allocatable_scratch_disk_bytes >= 0
        ),
    CONSTRAINT ck_host_nodes_non_negative_reserved
        CHECK (
            reserved_resident_unified_memory_bytes >= 0
            AND reserved_peak_loading_unified_memory_bytes >= 0
            AND reserved_incremental_unified_memory_bytes >= 0
            AND reserved_device_memory_bytes >= 0
            AND reserved_host_memory_bytes >= 0
            AND reserved_cpu_decode_millicores >= 0
            AND reserved_host_pcm_buffer_bytes >= 0
            AND reserved_scratch_disk_bytes >= 0
            AND quarantined_unified_memory_bytes >= 0
            AND quarantined_device_memory_bytes >= 0
            AND quarantined_host_memory_bytes >= 0
            AND quarantined_cpu_decode_millicores >= 0
            AND quarantined_host_pcm_buffer_bytes >= 0
            AND quarantined_scratch_disk_bytes >= 0
        ),
    CONSTRAINT ck_host_nodes_quarantine_threshold
        CHECK (
            quarantine_threshold_ratio >= 0.0000
            AND quarantine_threshold_ratio <= 1.0000
        ),
    -- Parent physical budget invariant: resident + cold-load peak + active incremental + quarantined
    -- can never exceed the node's allocatable parent budget.
    CONSTRAINT ck_host_nodes_unified_budget_ceiling
        CHECK (
            reserved_resident_unified_memory_bytes
            + reserved_peak_loading_unified_memory_bytes
            + reserved_incremental_unified_memory_bytes
            + quarantined_unified_memory_bytes
            <= allocatable_unified_memory_bytes
        ),
    CONSTRAINT ck_host_nodes_device_budget_ceiling
        CHECK (
            reserved_device_memory_bytes + quarantined_device_memory_bytes
            <= allocatable_device_memory_bytes
        ),
    CONSTRAINT ck_host_nodes_host_budget_ceiling
        CHECK (
            reserved_host_memory_bytes + quarantined_host_memory_bytes
            <= allocatable_host_memory_bytes
        ),
    CONSTRAINT ck_host_nodes_cpu_budget_ceiling
        CHECK (
            reserved_cpu_decode_millicores + quarantined_cpu_decode_millicores
            <= allocatable_cpu_decode_millicores
        ),
    CONSTRAINT ck_host_nodes_pcm_budget_ceiling
        CHECK (
            reserved_host_pcm_buffer_bytes + quarantined_host_pcm_buffer_bytes
            <= allocatable_host_pcm_buffer_bytes
        ),
    CONSTRAINT ck_host_nodes_scratch_budget_ceiling
        CHECK (
            reserved_scratch_disk_bytes + quarantined_scratch_disk_bytes
            <= allocatable_scratch_disk_bytes
        ),
    -- A node with quarantined capacity above the configured ratio cannot remain
    -- in `ready`; the same transaction must move it to `quarantined`/`draining`.
    CONSTRAINT ck_host_nodes_quarantine_blocks_ready
        CHECK (
            node_state <> 'ready'
            OR (
                quarantined_unified_memory_bytes
                    <= allocatable_unified_memory_bytes * quarantine_threshold_ratio
                AND quarantined_device_memory_bytes
                    <= allocatable_device_memory_bytes * quarantine_threshold_ratio
                AND quarantined_host_memory_bytes
                    <= allocatable_host_memory_bytes * quarantine_threshold_ratio
                AND quarantined_cpu_decode_millicores
                    <= allocatable_cpu_decode_millicores * quarantine_threshold_ratio
                AND quarantined_host_pcm_buffer_bytes
                    <= allocatable_host_pcm_buffer_bytes * quarantine_threshold_ratio
                AND quarantined_scratch_disk_bytes
                    <= allocatable_scratch_disk_bytes * quarantine_threshold_ratio
            )
        ),
    CONSTRAINT uq_host_nodes_identity_memory_architecture
        UNIQUE (host_node_id, memory_architecture)
);

-- ============================================================================
-- 2. resource_pools: Tier-3 Logical Capability Pools & Monotonic Fencing Watermark
-- ============================================================================
CREATE TABLE resource_pools (
    pool_id TEXT PRIMARY KEY,
    service_class TEXT NOT NULL,
    pool_state TEXT NOT NULL DEFAULT 'active',
    high_water_fencing_token BIGINT NOT NULL DEFAULT 0,
    realtime_reserved_ratio NUMERIC(5, 4) NOT NULL DEFAULT 0.0000,
    allow_batch_borrowing BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- `resource_pools` is the database registry. The externally exposed pool
    -- identifier domain is frozen once in shared/identifiers.schema.json rather
    -- than copied into a CHECK constraint.
    CONSTRAINT ck_resource_pools_service_class
        CHECK (service_class IN ('realtime', 'interactive', 'batch')),
    CONSTRAINT ck_resource_pools_state
        CHECK (pool_state IN ('active', 'draining', 'quarantined', 'disabled')),
    CONSTRAINT ck_resource_pools_fencing_non_negative
        CHECK (high_water_fencing_token >= 0),
    CONSTRAINT ck_resource_pools_reserved_ratio
        CHECK (realtime_reserved_ratio >= 0.0000 AND realtime_reserved_ratio <= 1.0000),
    CONSTRAINT uq_resource_pools_pool_service_class
        UNIQUE (pool_id, service_class)
);

-- ============================================================================
-- 3. scheduler_pool_leases: Per-Pool Scheduler Owner Lease & Fencing Token
-- ============================================================================
CREATE TABLE scheduler_pool_leases (
    pool_id TEXT PRIMARY KEY REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    owner_scheduler_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_state TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    renewed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    previous_owner_reconciled BOOLEAN NOT NULL DEFAULT TRUE,

    CONSTRAINT ck_scheduler_pool_leases_fencing_positive
        CHECK (fencing_token >= 1),
    CONSTRAINT ck_scheduler_pool_leases_state
        CHECK (lease_state IN ('reconciling_takeover', 'active', 'draining', 'relinquished')),
    CONSTRAINT ck_scheduler_pool_leases_expiry_order
        CHECK (expires_at > renewed_at AND renewed_at >= acquired_at),
    -- A new scheduler owner cannot enter `active` admission until the previous owner's
    -- gateway barrier and in-flight/submitting intents have been reconciled.
    CONSTRAINT ck_scheduler_pool_leases_active_requires_reconciled
        CHECK (lease_state <> 'active' OR previous_owner_reconciled = TRUE)
);

CREATE INDEX idx_scheduler_pool_leases_owner
    ON scheduler_pool_leases (owner_scheduler_id, lease_state);

-- ============================================================================
-- 4. engine_instances: Tier-2 Dynamic Runtime Instances & Slot Accounting
-- ============================================================================
CREATE TABLE engine_instances (
    instance_id TEXT NOT NULL,
    instance_generation INTEGER NOT NULL,
    host_node_id TEXT NOT NULL,
    memory_architecture TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    binding_revision_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    approved_template_id TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    loading_policy TEXT NOT NULL,
    management_mode TEXT NOT NULL,
    instance_state TEXT NOT NULL,

    -- Steady-state resident & loading peak budgets charged to host_node_id
    resident_unified_memory_bytes BIGINT NOT NULL,
    loading_peak_unified_memory_bytes BIGINT NOT NULL,
    per_slot_unified_memory_bytes BIGINT NOT NULL,
    resident_device_memory_bytes BIGINT NOT NULL DEFAULT 0,
    loading_peak_device_memory_bytes BIGINT NOT NULL DEFAULT 0,
    per_slot_device_memory_bytes BIGINT NOT NULL DEFAULT 0,
    resident_host_memory_bytes BIGINT NOT NULL DEFAULT 0,
    loading_peak_host_memory_bytes BIGINT NOT NULL DEFAULT 0,
    per_slot_host_memory_bytes BIGINT NOT NULL DEFAULT 0,
    peak_reservation_active BOOLEAN NOT NULL DEFAULT TRUE,

    max_active_slots INTEGER NOT NULL,
    realtime_reserved_slots INTEGER NOT NULL DEFAULT 0,
    reserved_active_slots INTEGER NOT NULL DEFAULT 0,
    quarantined_slots INTEGER NOT NULL DEFAULT 0,

    last_fencing_token BIGINT NOT NULL,
    ready_at TIMESTAMPTZ,
    idle_since_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (instance_id, instance_generation),
    CONSTRAINT uq_engine_instances_identity_architecture
        UNIQUE (instance_id, instance_generation, memory_architecture),
    CONSTRAINT uq_engine_instances_identity_host
        UNIQUE (instance_id, instance_generation, host_node_id),
    CONSTRAINT uq_engine_instances_execution_identity
        UNIQUE (
            instance_id,
            instance_generation,
            pool_id,
            gateway_id,
            binding_revision_id
        ),
    CONSTRAINT fk_engine_instances_host_architecture
        FOREIGN KEY (host_node_id, memory_architecture)
        REFERENCES host_nodes (host_node_id, memory_architecture)
        ON DELETE RESTRICT,
    CONSTRAINT ck_engine_instances_generation_positive
        CHECK (instance_generation >= 1),
    CONSTRAINT ck_engine_instances_fencing_positive
        CHECK (last_fencing_token >= 1),
    CONSTRAINT ck_engine_instances_memory_architecture
        CHECK (memory_architecture IN ('unified_memory', 'discrete_device_memory')),
    CONSTRAINT ck_engine_instances_loading_policy
        CHECK (loading_policy IN ('resident', 'on_demand')),
    CONSTRAINT ck_engine_instances_management_mode
        CHECK (management_mode IN ('managed', 'external')),
    CONSTRAINT ck_engine_instances_state
        CHECK (
            instance_state IN (
                'preparing',
                'loading',
                'warming_up',
                'ready',
                'draining',
                'stopping',
                'stopped',
                'failed',
                'quarantined'
            )
        ),
    CONSTRAINT ck_engine_instances_slots_bounds
        CHECK (
            max_active_slots >= 1
            AND realtime_reserved_slots >= 0
            AND realtime_reserved_slots <= max_active_slots
            AND reserved_active_slots >= 0
            AND quarantined_slots >= 0
            AND reserved_active_slots + quarantined_slots <= max_active_slots
        ),
    CONSTRAINT ck_engine_instances_peak_vs_resident
        CHECK (
            resident_unified_memory_bytes >= 0
            AND loading_peak_unified_memory_bytes >= resident_unified_memory_bytes
            AND per_slot_unified_memory_bytes >= 0
            AND resident_device_memory_bytes >= 0
            AND loading_peak_device_memory_bytes >= resident_device_memory_bytes
            AND per_slot_device_memory_bytes >= 0
            AND resident_host_memory_bytes >= 0
            AND loading_peak_host_memory_bytes >= resident_host_memory_bytes
            AND per_slot_host_memory_bytes >= 0
        ),
    -- The instance's memory vector must use only the counters of its host's
    -- memory architecture. The composite FK prevents the architecture flag from
    -- drifting away from the referenced host node.
    CONSTRAINT ck_engine_instances_memory_architecture_counters
        CHECK (
            (
                memory_architecture = 'unified_memory'
                AND resident_device_memory_bytes = 0
                AND loading_peak_device_memory_bytes = 0
                AND per_slot_device_memory_bytes = 0
                AND resident_host_memory_bytes = 0
                AND loading_peak_host_memory_bytes = 0
                AND per_slot_host_memory_bytes = 0
            )
            OR (
                memory_architecture = 'discrete_device_memory'
                AND resident_unified_memory_bytes = 0
                AND loading_peak_unified_memory_bytes = 0
                AND per_slot_unified_memory_bytes = 0
            )
        ),
    -- Steady-state budget can replace loading peak only after instance reaches `ready` or later
    CONSTRAINT ck_engine_instances_peak_release_requires_ready
        CHECK (
            peak_reservation_active = TRUE
            OR instance_state IN ('ready', 'draining', 'stopping', 'stopped')
        ),
    -- `stopped` releases the active identity only after a stop time is observed
    -- and the loading peak, active slots, and quarantined slots are all released.
    CONSTRAINT ck_engine_instances_stopped_evidence
        CHECK (
            instance_state <> 'stopped'
            OR (
                stopped_at IS NOT NULL
                AND peak_reservation_active = FALSE
                AND reserved_active_slots = 0
                AND quarantined_slots = 0
            )
        )
);

CREATE INDEX idx_engine_instances_node_state
    ON engine_instances (host_node_id, instance_state);
CREATE INDEX idx_engine_instances_pool_ready
    ON engine_instances (pool_id, binding_revision_id, instance_state);
-- An instance identity may have historical stopped generations, but at most one
-- non-stopped generation may own the identity at any time.
CREATE UNIQUE INDEX uq_engine_instances_active_identity
    ON engine_instances (instance_id)
    WHERE instance_state <> 'stopped';

-- ============================================================================
-- 5. runtime_instance_intents: Durable Node-Scoped Controller Reconcile Intents
-- ============================================================================
CREATE TABLE runtime_instance_intents (
    target_node_id TEXT NOT NULL REFERENCES host_nodes (host_node_id) ON DELETE RESTRICT,
    intent_id TEXT NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    gateway_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    instance_generation INTEGER NOT NULL,
    memory_architecture TEXT NOT NULL,
    approved_template_id TEXT NOT NULL,
    binding_revision_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    action TEXT NOT NULL,
    fencing_token BIGINT NOT NULL,

    intent_state TEXT NOT NULL,
    observed_instance_state TEXT NOT NULL,
    reserved_peak_device_bytes BIGINT NOT NULL,
    reserved_steady_device_bytes BIGINT NOT NULL,
    reserved_peak_host_bytes BIGINT NOT NULL,
    reserved_steady_host_bytes BIGINT NOT NULL,
    reserved_peak_unified_bytes BIGINT NOT NULL,
    reserved_steady_unified_bytes BIGINT NOT NULL,
    observed_peak_device_bytes BIGINT,
    observed_peak_host_bytes BIGINT,
    observed_peak_unified_bytes BIGINT,

    process_or_container_exited BOOLEAN NOT NULL DEFAULT FALSE,
    resources_released_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    exit_code INTEGER,
    runtime_container_id TEXT,
    stop_verified_at TIMESTAMPTZ,
    error_code TEXT,

    deadline_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- The idempotency scope is one node controller. The same caller-generated
    -- key may be used independently on another node, but not twice here.
    PRIMARY KEY (target_node_id, intent_id),
    CONSTRAINT fk_runtime_intents_instance
        FOREIGN KEY (instance_id, instance_generation, memory_architecture)
        REFERENCES engine_instances (instance_id, instance_generation, memory_architecture)
        ON DELETE RESTRICT,
    CONSTRAINT fk_runtime_intents_instance_host
        FOREIGN KEY (instance_id, instance_generation, target_node_id)
        REFERENCES engine_instances (instance_id, instance_generation, host_node_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_runtime_intents_execution_identity
        FOREIGN KEY (
            instance_id,
            instance_generation,
            pool_id,
            gateway_id,
            binding_revision_id
        )
        REFERENCES engine_instances (
            instance_id,
            instance_generation,
            pool_id,
            gateway_id,
            binding_revision_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT ck_runtime_intents_action
        CHECK (action IN ('prepare', 'stop')),
    CONSTRAINT ck_runtime_intents_fencing_positive
        CHECK (fencing_token >= 1),
    CONSTRAINT ck_runtime_intents_state
        CHECK (intent_state IN ('pending', 'reconciling', 'succeeded', 'failed', 'quarantined')),
    CONSTRAINT ck_runtime_intents_observed_state
        CHECK (
            observed_instance_state IN (
                'absent',
                'preparing',
                'loading',
                'warming_up',
                'ready',
                'draining',
                'stopping',
                'stopped',
                'failed',
                'quarantined'
            )
        ),
    CONSTRAINT ck_runtime_intents_memory_architecture
        CHECK (memory_architecture IN ('unified_memory', 'discrete_device_memory')),
    CONSTRAINT ck_runtime_intents_memory_architecture_counters
        CHECK (
            (
                memory_architecture = 'unified_memory'
                AND reserved_peak_device_bytes = 0
                AND reserved_steady_device_bytes = 0
                AND reserved_peak_host_bytes = 0
                AND reserved_steady_host_bytes = 0
                AND (observed_peak_device_bytes IS NULL OR observed_peak_device_bytes = 0)
                AND (observed_peak_host_bytes IS NULL OR observed_peak_host_bytes = 0)
            )
            OR (
                memory_architecture = 'discrete_device_memory'
                AND reserved_peak_unified_bytes = 0
                AND reserved_steady_unified_bytes = 0
                AND (observed_peak_unified_bytes IS NULL OR observed_peak_unified_bytes = 0)
            )
        ),
    CONSTRAINT ck_runtime_intents_memory_non_negative
        CHECK (
            reserved_peak_device_bytes >= 0
            AND reserved_steady_device_bytes >= 0
            AND reserved_peak_host_bytes >= 0
            AND reserved_steady_host_bytes >= 0
            AND reserved_peak_unified_bytes >= 0
            AND reserved_steady_unified_bytes >= 0
            AND (observed_peak_device_bytes IS NULL OR observed_peak_device_bytes >= 0)
            AND (observed_peak_host_bytes IS NULL OR observed_peak_host_bytes >= 0)
            AND (observed_peak_unified_bytes IS NULL OR observed_peak_unified_bytes >= 0)
        ),
    CONSTRAINT ck_runtime_intents_peak_vs_steady
        CHECK (
            reserved_peak_device_bytes >= reserved_steady_device_bytes
            AND reserved_peak_host_bytes >= reserved_steady_host_bytes
            AND reserved_peak_unified_bytes >= reserved_steady_unified_bytes
        ),
    CONSTRAINT ck_runtime_intents_prepare_evidence
        CHECK (
            action <> 'prepare'
            OR intent_state <> 'succeeded'
            OR observed_instance_state = 'ready'
        ),
    -- Stop intent cannot succeed unless both process exit and resource release are confirmed
    CONSTRAINT ck_runtime_intents_stop_evidence
        CHECK (
            action <> 'stop'
            OR intent_state <> 'succeeded'
            OR (
                process_or_container_exited = TRUE
                AND resources_released_confirmed = TRUE
                AND stop_verified_at IS NOT NULL
                AND observed_instance_state IN ('stopped', 'absent')
            )
        ),
    CONSTRAINT ck_runtime_intents_failed_error
        CHECK (
            intent_state <> 'failed'
            OR (
                error_code IS NOT NULL
                AND error_code ~ '^[a-z][a-z0-9_]{2,63}$'
            )
        ),
    CONSTRAINT ck_runtime_intents_succeeded_error
        CHECK (intent_state <> 'succeeded' OR error_code IS NULL),
    CONSTRAINT ck_runtime_intents_stop_verified
        CHECK (
            stop_verified_at IS NULL
            OR (
                process_or_container_exited = TRUE
                AND resources_released_confirmed = TRUE
            )
        )
);

CREATE INDEX idx_runtime_instance_intents_node_pending
    ON runtime_instance_intents (target_node_id, intent_state, created_at)
    WHERE intent_state IN ('pending', 'reconciling');
-- Historical intent rows are retained. Only one unresolved intent may exist for
-- a given generation/action; `(target_node_id, intent_id)` is the
-- node-controller-scoped idempotency key.
CREATE UNIQUE INDEX uq_runtime_instance_intents_active_action
    ON runtime_instance_intents (instance_id, instance_generation, action)
    WHERE intent_state IN ('pending', 'reconciling', 'quarantined');

-- ============================================================================
-- 6. admission_leases: Cross-Tier Atomic Capacity Reservations & Cleanup State
-- ============================================================================
CREATE TABLE admission_leases (
    lease_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    service_class TEXT NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    host_node_id TEXT NOT NULL,
    memory_architecture TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    instance_generation INTEGER NOT NULL,
    binding_revision_id TEXT NOT NULL,
    policy_revision_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL,

    lease_state TEXT NOT NULL,
    cleanup_state TEXT NOT NULL,

    -- Incremental resource vector reserved for this lease
    reserved_slots INTEGER NOT NULL DEFAULT 1,
    reserved_unified_memory_bytes BIGINT NOT NULL,
    reserved_device_memory_bytes BIGINT NOT NULL,
    reserved_host_memory_bytes BIGINT NOT NULL,
    reserved_cpu_decode_millicores INTEGER NOT NULL,
    reserved_host_pcm_buffer_bytes BIGINT NOT NULL,
    reserved_scratch_disk_bytes BIGINT NOT NULL,

    admitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline_at TIMESTAMPTZ NOT NULL,
    quarantined_at TIMESTAMPTZ,
    quarantine_release_evidence_kind TEXT,
    quarantine_release_evidence_digest TEXT,
    quarantine_release_observed_at TIMESTAMPTZ,
    quarantine_release_reason TEXT,
    budget_released_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_admission_leases_instance_host
        FOREIGN KEY (instance_id, instance_generation, host_node_id)
        REFERENCES engine_instances (instance_id, instance_generation, host_node_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_admission_leases_instance_architecture
        FOREIGN KEY (instance_id, instance_generation, memory_architecture)
        REFERENCES engine_instances (instance_id, instance_generation, memory_architecture)
        ON DELETE RESTRICT,
    CONSTRAINT fk_admission_leases_pool_service_class
        FOREIGN KEY (pool_id, service_class)
        REFERENCES resource_pools (pool_id, service_class)
        ON DELETE RESTRICT,
    CONSTRAINT fk_admission_leases_execution_identity
        FOREIGN KEY (
            instance_id,
            instance_generation,
            pool_id,
            gateway_id,
            binding_revision_id
        )
        REFERENCES engine_instances (
            instance_id,
            instance_generation,
            pool_id,
            gateway_id,
            binding_revision_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT uq_admission_leases_identity
        UNIQUE (lease_id, operation_id, attempt_id),
    CONSTRAINT uq_admission_leases_identity_pool
        UNIQUE (lease_id, pool_id),
    CONSTRAINT uq_admission_leases_identity_stage
        UNIQUE (lease_id, stage_id),
    CONSTRAINT uq_admission_leases_identity_workspace
        UNIQUE (lease_id, workspace_id),
    -- A complete execution recipe owns one lease per required stage. Multiple
    -- stages may use the same pool or instance, so uniqueness is by stage.
    CONSTRAINT uq_admission_leases_operation_attempt_stage
        UNIQUE (operation_id, attempt_id, stage_id),
    CONSTRAINT uq_admission_leases_execution_identity
        UNIQUE (
            lease_id,
            instance_id,
            instance_generation,
            gateway_id,
            binding_revision_id
        ),
    CONSTRAINT ck_admission_leases_service_class
        CHECK (service_class IN ('realtime', 'interactive', 'batch')),
    CONSTRAINT ck_admission_leases_stage_id_format
        CHECK (stage_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
    CONSTRAINT ck_admission_leases_fencing_positive
        CHECK (fencing_token >= 1),
    CONSTRAINT ck_admission_leases_reservations_non_negative
        CHECK (
            reserved_slots >= 1
            AND reserved_unified_memory_bytes >= 0
            AND reserved_device_memory_bytes >= 0
            AND reserved_host_memory_bytes >= 0
            AND reserved_cpu_decode_millicores >= 0
            AND reserved_host_pcm_buffer_bytes >= 0
            AND reserved_scratch_disk_bytes >= 0
        ),
    CONSTRAINT ck_admission_leases_memory_architecture
        CHECK (memory_architecture IN ('unified_memory', 'discrete_device_memory')),
    CONSTRAINT ck_admission_leases_memory_architecture_counters
        CHECK (
            (
                memory_architecture = 'unified_memory'
                AND reserved_device_memory_bytes = 0
                AND reserved_host_memory_bytes = 0
            )
            OR (
                memory_architecture = 'discrete_device_memory'
                AND reserved_unified_memory_bytes = 0
            )
        ),
    CONSTRAINT ck_admission_leases_state
        CHECK (lease_state IN ('reserved', 'executing', 'stopping', 'released', 'quarantined')),
    CONSTRAINT ck_admission_leases_cleanup_state
        CHECK (cleanup_state IN ('not_required', 'pending', 'complete', 'quarantined')),
    CONSTRAINT ck_admission_leases_quarantine_release_evidence_kind
        CHECK (
            quarantine_release_evidence_kind IS NULL
            OR quarantine_release_evidence_kind IN (
                'engine_abort_ack',
                'engine_terminal_response',
                'instance_terminated_by_controller',
                'operator_verified_instance_terminated'
            )
        ),
    CONSTRAINT ck_admission_leases_quarantine_evidence_pair
        CHECK (
            (quarantine_release_evidence_kind IS NULL
                AND quarantine_release_evidence_digest IS NULL
                AND quarantine_release_observed_at IS NULL
                AND quarantine_release_reason IS NULL)
            OR (
                quarantine_release_evidence_kind IS NOT NULL
                AND quarantine_release_evidence_digest ~ '^sha256:[0-9a-f]{64}$'
                AND quarantine_release_observed_at IS NOT NULL
                AND quarantined_at IS NOT NULL
                AND (
                    (
                        quarantine_release_evidence_kind = 'operator_verified_instance_terminated'
                        AND quarantine_release_reason IS NOT NULL
                        AND char_length(btrim(quarantine_release_reason)) BETWEEN 1 AND 2000
                    )
                    OR (
                        quarantine_release_evidence_kind <> 'operator_verified_instance_terminated'
                        AND quarantine_release_reason IS NULL
                    )
                )
            )
        ),
    -- Budget release is strictly forbidden while lease is quarantined or cleanup is pending
    CONSTRAINT ck_admission_leases_release_invariant
        CHECK (
            (
                lease_state = 'released'
                AND cleanup_state IN ('not_required', 'complete')
                AND budget_released_at IS NOT NULL
                AND (
                    quarantine_release_observed_at IS NULL
                    OR quarantine_release_observed_at <= budget_released_at
                )
            )
            OR (
                lease_state = 'quarantined'
                AND cleanup_state = 'quarantined'
                AND budget_released_at IS NULL
            )
            OR (
                lease_state IN ('reserved', 'executing', 'stopping')
                AND cleanup_state IN ('not_required', 'pending')
                AND budget_released_at IS NULL
            )
        ),
    CONSTRAINT ck_admission_leases_quarantine_opened
        CHECK (lease_state <> 'quarantined' OR quarantined_at IS NOT NULL),
    CONSTRAINT ck_admission_leases_quarantine_release
        CHECK (
            quarantined_at IS NULL
            OR lease_state = 'quarantined'
            OR (
                lease_state = 'released'
                AND cleanup_state = 'complete'
                AND quarantine_release_evidence_kind IS NOT NULL
                AND quarantine_release_evidence_digest IS NOT NULL
            )
        )
);

CREATE INDEX idx_admission_leases_active_instance
    ON admission_leases (instance_id, instance_generation, lease_state)
    WHERE lease_state IN ('reserved', 'executing', 'stopping', 'quarantined');

-- ============================================================================
-- 7. gateway_fencing_barriers: Execution Gateway Ingress Seal for Owner Takeover
-- ============================================================================
CREATE TABLE gateway_fencing_barriers (
    gateway_id TEXT NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    host_node_id TEXT NOT NULL REFERENCES host_nodes (host_node_id) ON DELETE RESTRICT,
    enforced_min_fencing_token BIGINT NOT NULL,
    new_owner_scheduler_id TEXT NOT NULL,
    barrier_sealed BOOLEAN NOT NULL DEFAULT TRUE,
    enforced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (gateway_id, pool_id),
    CONSTRAINT ck_gateway_barriers_fencing_positive
        CHECK (enforced_min_fencing_token >= 1)
);

-- ============================================================================
-- 8. submit_intents: Authoritative Two-Phase Engine Submission & Watermark Ledger
-- ============================================================================
CREATE TABLE submit_intents (
    submit_intent_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    pool_id TEXT NOT NULL REFERENCES resource_pools (pool_id) ON DELETE RESTRICT,
    host_node_id TEXT NOT NULL REFERENCES host_nodes (host_node_id) ON DELETE RESTRICT,
    gateway_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    instance_generation INTEGER NOT NULL,
    binding_revision_id TEXT NOT NULL,
    policy_revision_id TEXT NOT NULL,
    voice_revision_id TEXT,
    fencing_token BIGINT NOT NULL,
    command_digest TEXT NOT NULL,

    engine_request_id TEXT,
    execution_state TEXT NOT NULL,
    cleanup_state TEXT NOT NULL,
    stop_evidence_kind TEXT NOT NULL DEFAULT 'none',

    -- Output metering watermark (persisted across retries and cancellations)
    output_stream_id TEXT,
    generated_pcm_samples BIGINT NOT NULL DEFAULT 0,
    sample_rate_hz INTEGER,
    hard_output_ceiling_samples BIGINT,

    staging_output_object_key TEXT,
    error_code TEXT,
    deadline_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dispatched_at TIMESTAMPTZ,
    terminated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_submit_intents_lease_identity
        FOREIGN KEY (lease_id, operation_id, attempt_id)
        REFERENCES admission_leases (lease_id, operation_id, attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_submit_intents_lease_stage
        FOREIGN KEY (lease_id, stage_id)
        REFERENCES admission_leases (lease_id, stage_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_submit_intents_lease_pool
        FOREIGN KEY (lease_id, pool_id)
        REFERENCES admission_leases (lease_id, pool_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_submit_intents_lease_workspace
        FOREIGN KEY (lease_id, workspace_id)
        REFERENCES admission_leases (lease_id, workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_submit_intents_instance
        FOREIGN KEY (instance_id, instance_generation, host_node_id)
        REFERENCES engine_instances (instance_id, instance_generation, host_node_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_submit_intents_lease_execution_identity
        FOREIGN KEY (
            lease_id,
            instance_id,
            instance_generation,
            gateway_id,
            binding_revision_id
        )
        REFERENCES admission_leases (
            lease_id,
            instance_id,
            instance_generation,
            gateway_id,
            binding_revision_id
        )
        ON DELETE RESTRICT,
    -- Guarantees a single authoritative submit intent per (operation_id, attempt_id, stage_id)
    CONSTRAINT uq_submit_intents_operation_attempt_stage
        UNIQUE (operation_id, attempt_id, stage_id),
    CONSTRAINT uq_submit_intents_identity
        UNIQUE (submit_intent_id, operation_id, attempt_id),
    CONSTRAINT uq_submit_intents_output_identity
        UNIQUE (submit_intent_id, output_stream_id, sample_rate_hz),
    CONSTRAINT ck_submit_intents_fencing_positive
        CHECK (fencing_token >= 1),
    CONSTRAINT ck_submit_intents_execution_state
        CHECK (
            execution_state IN (
                'submitting',
                'executing',
                'stopping',
                'succeeded',
                'failed',
                'aborted',
                'unknown_quarantined'
            )
        ),
    CONSTRAINT ck_submit_intents_cleanup_state
        CHECK (cleanup_state IN ('not_required', 'pending', 'complete', 'quarantined')),
    CONSTRAINT ck_submit_intents_stop_evidence_kind
        CHECK (
            stop_evidence_kind IN (
                'none',
                'engine_abort_ack',
                'engine_terminal_response',
                'instance_terminated_by_controller',
                'operator_verified_instance_terminated',
                'unconfirmed_timeout'
            )
        ),
    -- Stop evidence is a terminal observation. It cannot be attached to a
    -- still-active submission before the corresponding state transition.
    CONSTRAINT ck_submit_intents_active_stop_evidence
        CHECK (
            execution_state NOT IN ('submitting', 'executing', 'stopping')
            OR stop_evidence_kind = 'none'
        ),
    -- Active executions still own cleanup. Successful and failed executions
    -- may not silently retain pending capacity; unknown execution is the only
    -- quarantined execution state and is constrained separately below.
    CONSTRAINT ck_submit_intents_execution_cleanup_matrix
        CHECK (
            (
                execution_state IN ('submitting', 'executing', 'stopping')
                AND cleanup_state = 'pending'
            )
            OR (
                execution_state = 'succeeded'
                AND cleanup_state = 'complete'
                AND stop_evidence_kind = 'engine_terminal_response'
                AND error_code IS NULL
                AND terminated_at IS NOT NULL
            )
            OR (
                execution_state = 'failed'
                AND error_code IS NOT NULL
                AND (
                    (
                        cleanup_state = 'not_required'
                        AND stop_evidence_kind = 'none'
                        AND dispatched_at IS NULL
                        AND engine_request_id IS NULL
                        AND generated_pcm_samples = 0
                        AND staging_output_object_key IS NULL
                        AND terminated_at IS NULL
                    )
                    OR (
                        cleanup_state = 'complete'
                        AND stop_evidence_kind IN (
                            'engine_terminal_response',
                            'instance_terminated_by_controller',
                            'operator_verified_instance_terminated'
                        )
                        AND terminated_at IS NOT NULL
                    )
                )
            )
            OR execution_state IN ('aborted', 'unknown_quarantined')
        ),
    -- `aborted` is strictly forbidden without verified stop evidence from the engine or controller
    CONSTRAINT ck_submit_intents_aborted_requires_evidence
        CHECK (
            execution_state <> 'aborted'
            OR (
                stop_evidence_kind IN (
                    'engine_abort_ack',
                    'engine_terminal_response',
                    'instance_terminated_by_controller',
                    'operator_verified_instance_terminated'
                )
                AND cleanup_state = 'complete'
                AND terminated_at IS NOT NULL
            )
        ),
    -- `unknown_quarantined` must keep cleanup_state = 'quarantined' so capacity is not recycled
    CONSTRAINT ck_submit_intents_quarantine_invariant
        CHECK (
            execution_state <> 'unknown_quarantined'
            OR (
                cleanup_state = 'quarantined'
                AND stop_evidence_kind = 'unconfirmed_timeout'
            )
        ),
    -- A request is dispatched at most once and only after the durable
    -- `submitting` row exists. Execution, terminal success, and verified abort
    -- therefore require the observed dispatch time; an untouched submission
    -- has no output or staging fact.
    CONSTRAINT ck_submit_intents_dispatch_matrix
        CHECK (
            (
                execution_state = 'submitting'
                AND dispatched_at IS NULL
                AND generated_pcm_samples = 0
                AND staging_output_object_key IS NULL
            )
            OR (
                execution_state IN ('executing', 'stopping', 'succeeded', 'aborted')
                AND dispatched_at IS NOT NULL
            )
            OR execution_state = 'unknown_quarantined'
            OR (
                execution_state = 'failed'
                AND (
                    (
                        cleanup_state = 'not_required'
                        AND dispatched_at IS NULL
                    )
                    OR (
                        cleanup_state = 'complete'
                        AND dispatched_at IS NOT NULL
                    )
                )
            )
        ),
    CONSTRAINT ck_submit_intents_watermark_non_negative
        CHECK (
            generated_pcm_samples >= 0
            AND (sample_rate_hz IS NULL OR sample_rate_hz IN (16000, 24000, 48000))
            AND (hard_output_ceiling_samples IS NULL OR sample_rate_hz IS NOT NULL)
            AND (
                hard_output_ceiling_samples IS NULL
                OR hard_output_ceiling_samples >= generated_pcm_samples
            )
        )
);

CREATE INDEX idx_submit_intents_gateway_active
    ON submit_intents (gateway_id, pool_id, execution_state)
    WHERE execution_state IN ('submitting', 'executing', 'stopping', 'unknown_quarantined');

CREATE INDEX idx_submit_intents_instance_active
    ON submit_intents (instance_id, instance_generation, execution_state)
    WHERE execution_state IN ('submitting', 'executing', 'stopping', 'unknown_quarantined');

CREATE INDEX idx_submit_intents_lease_state
    ON submit_intents (lease_id, execution_state);

-- ============================================================================
-- 9. output_sample_intervals: Interval-Authoritative Output Metering Ledger
-- ============================================================================
CREATE TABLE output_sample_intervals (
    interval_id TEXT PRIMARY KEY,
    submit_intent_id TEXT NOT NULL REFERENCES submit_intents (submit_intent_id) ON DELETE RESTRICT,
    operation_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    output_stream_id TEXT NOT NULL,
    sample_rate_hz INTEGER NOT NULL,
    sample_start BIGINT NOT NULL,
    sample_end BIGINT NOT NULL,
    meter_kind TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_output_sample_intervals_submit_identity
        FOREIGN KEY (submit_intent_id, operation_id, attempt_id)
        REFERENCES submit_intents (submit_intent_id, operation_id, attempt_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_output_sample_intervals_output_identity
        FOREIGN KEY (submit_intent_id, output_stream_id, sample_rate_hz)
        REFERENCES submit_intents (submit_intent_id, output_stream_id, sample_rate_hz)
        ON DELETE RESTRICT,
    CONSTRAINT ex_output_sample_intervals_no_overlap
        EXCLUDE USING gist (
            operation_id WITH =,
            attempt_id WITH =,
            output_stream_id WITH =,
            meter_kind WITH =,
            int8range(sample_start, sample_end, '[)') WITH &&
        ),
    CONSTRAINT ck_output_sample_intervals_range
        CHECK (sample_start >= 0 AND sample_end > sample_start),
    CONSTRAINT ck_output_sample_intervals_rate
        CHECK (sample_rate_hz IN (16000, 24000, 48000)),
    CONSTRAINT ck_output_sample_intervals_meter_kind
        CHECK (meter_kind IN ('generated', 'delivered', 'played'))
);

CREATE INDEX idx_output_sample_intervals_attempt
    ON output_sample_intervals (operation_id, attempt_id, output_stream_id, sample_start);

-- Interval rows are authoritative. Reject intervals beyond the frozen hard
-- ceiling and advance the generated high-water projection only from generated
-- intervals.
CREATE FUNCTION enforce_output_interval_ceiling() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    output_ceiling BIGINT;
BEGIN
    SELECT hard_output_ceiling_samples
    INTO output_ceiling
    FROM submit_intents
    WHERE submit_intent_id = NEW.submit_intent_id;

    IF output_ceiling IS NOT NULL AND NEW.sample_end > output_ceiling THEN
        RAISE EXCEPTION 'output interval exceeds hard output ceiling'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_output_intervals_ceiling
    BEFORE INSERT ON output_sample_intervals
    FOR EACH ROW EXECUTE FUNCTION enforce_output_interval_ceiling();

CREATE FUNCTION advance_output_sample_watermark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.meter_kind = 'generated' THEN
        UPDATE submit_intents
        SET generated_pcm_samples = GREATEST(generated_pcm_samples, NEW.sample_end)
        WHERE submit_intent_id = NEW.submit_intent_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_output_intervals_watermark
    AFTER INSERT ON output_sample_intervals
    FOR EACH ROW EXECUTE FUNCTION advance_output_sample_watermark();

CREATE FUNCTION reject_output_interval_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'output sample intervals are append-only'
        USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER trg_output_intervals_append_only
    BEFORE UPDATE OR DELETE ON output_sample_intervals
    FOR EACH ROW EXECUTE FUNCTION reject_output_interval_mutation();

CREATE FUNCTION reject_output_interval_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'output sample intervals are append-only'
        USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER trg_output_intervals_no_truncate
    BEFORE TRUNCATE ON output_sample_intervals
    FOR EACH STATEMENT EXECUTE FUNCTION reject_output_interval_truncate();

-- PostgreSQL CHECK constraints cannot compare old and new row values. Keep the
-- gateway fencing watermark monotonic with a trigger, using CHECK_VIOLATION so
-- callers observe a stable SQLSTATE.
CREATE FUNCTION enforce_resource_pool_fencing_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.high_water_fencing_token < OLD.high_water_fencing_token THEN
        RAISE EXCEPTION 'resource-pool fencing token cannot decrease'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_resource_pools_fencing_monotonic
    BEFORE UPDATE ON resource_pools
    FOR EACH ROW EXECUTE FUNCTION enforce_resource_pool_fencing_monotonic();

CREATE FUNCTION enforce_gateway_fencing_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.enforced_min_fencing_token < OLD.enforced_min_fencing_token THEN
        RAISE EXCEPTION 'gateway fencing token cannot decrease'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_gateway_fencing_monotonic
    BEFORE UPDATE ON gateway_fencing_barriers
    FOR EACH ROW EXECUTE FUNCTION enforce_gateway_fencing_monotonic();

CREATE FUNCTION enforce_scheduler_lease_fencing_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fencing_token < OLD.fencing_token THEN
        RAISE EXCEPTION 'scheduler lease fencing token cannot decrease'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.owner_scheduler_id IS DISTINCT FROM OLD.owner_scheduler_id
        AND NEW.fencing_token <= OLD.fencing_token THEN
        RAISE EXCEPTION 'scheduler takeover requires a higher fencing token'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_scheduler_pool_leases_fencing_monotonic
    BEFORE UPDATE ON scheduler_pool_leases
    FOR EACH ROW EXECUTE FUNCTION enforce_scheduler_lease_fencing_monotonic();

CREATE FUNCTION enforce_engine_instance_fencing_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.last_fencing_token < OLD.last_fencing_token THEN
        RAISE EXCEPTION 'engine-instance fencing token cannot decrease'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_engine_instances_fencing_monotonic
    BEFORE UPDATE ON engine_instances
    FOR EACH ROW EXECUTE FUNCTION enforce_engine_instance_fencing_monotonic();

-- A generation is an immutable runtime identity. Once stopped, it remains a
-- historical fact and cannot be revived; a restart must allocate a new
-- generation so stale fencing tokens and late evidence cannot target it.
CREATE FUNCTION enforce_engine_instance_identity_and_terminality() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.instance_state = 'stopped' THEN
        RAISE EXCEPTION 'stopped engine-instance generation is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.instance_id IS DISTINCT FROM OLD.instance_id
        OR NEW.instance_generation IS DISTINCT FROM OLD.instance_generation
        OR NEW.host_node_id IS DISTINCT FROM OLD.host_node_id
        OR NEW.memory_architecture IS DISTINCT FROM OLD.memory_architecture
        OR NEW.gateway_id IS DISTINCT FROM OLD.gateway_id
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.binding_revision_id IS DISTINCT FROM OLD.binding_revision_id
        OR NEW.manifest_id IS DISTINCT FROM OLD.manifest_id
        OR NEW.approved_template_id IS DISTINCT FROM OLD.approved_template_id
        OR NEW.config_digest IS DISTINCT FROM OLD.config_digest
        OR NEW.loading_policy IS DISTINCT FROM OLD.loading_policy
        OR NEW.management_mode IS DISTINCT FROM OLD.management_mode
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'engine-instance generation identity is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_engine_instances_identity_and_terminality
    BEFORE UPDATE ON engine_instances
    FOR EACH ROW EXECUTE FUNCTION enforce_engine_instance_identity_and_terminality();

-- Runtime intents retain their full history. Succeeded and failed intents are
-- terminal facts; a retry is a new intent_id. Quarantined intents stay mutable
-- so later controller evidence can close the same reconciliation attempt.
CREATE FUNCTION enforce_runtime_intent_identity_and_terminality() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.intent_state IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'terminal runtime intent is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.intent_id IS DISTINCT FROM OLD.intent_id
        OR NEW.target_node_id IS DISTINCT FROM OLD.target_node_id
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.gateway_id IS DISTINCT FROM OLD.gateway_id
        OR NEW.instance_id IS DISTINCT FROM OLD.instance_id
        OR NEW.instance_generation IS DISTINCT FROM OLD.instance_generation
        OR NEW.memory_architecture IS DISTINCT FROM OLD.memory_architecture
        OR NEW.approved_template_id IS DISTINCT FROM OLD.approved_template_id
        OR NEW.binding_revision_id IS DISTINCT FROM OLD.binding_revision_id
        OR NEW.manifest_id IS DISTINCT FROM OLD.manifest_id
        OR NEW.config_digest IS DISTINCT FROM OLD.config_digest
        OR NEW.action IS DISTINCT FROM OLD.action
        OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
        OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'runtime intent identity is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_runtime_instance_intents_identity_and_terminality
    BEFORE UPDATE ON runtime_instance_intents
    FOR EACH ROW EXECUTE FUNCTION enforce_runtime_intent_identity_and_terminality();

-- Releasing a lease is an irreversible accounting fact. Identity and reserved
-- vector are frozen from admission so later updates cannot redirect a lease to
-- another execution or silently change the amount released.
CREATE FUNCTION enforce_admission_lease_identity_and_release_terminality() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.lease_state = 'released' THEN
        RAISE EXCEPTION 'released admission lease is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.lease_id IS DISTINCT FROM OLD.lease_id
        OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
        OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.stage_id IS DISTINCT FROM OLD.stage_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.service_class IS DISTINCT FROM OLD.service_class
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.host_node_id IS DISTINCT FROM OLD.host_node_id
        OR NEW.memory_architecture IS DISTINCT FROM OLD.memory_architecture
        OR NEW.gateway_id IS DISTINCT FROM OLD.gateway_id
        OR NEW.instance_id IS DISTINCT FROM OLD.instance_id
        OR NEW.instance_generation IS DISTINCT FROM OLD.instance_generation
        OR NEW.binding_revision_id IS DISTINCT FROM OLD.binding_revision_id
        OR NEW.policy_revision_id IS DISTINCT FROM OLD.policy_revision_id
        OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
        OR NEW.reserved_slots IS DISTINCT FROM OLD.reserved_slots
        OR NEW.reserved_unified_memory_bytes IS DISTINCT FROM OLD.reserved_unified_memory_bytes
        OR NEW.reserved_device_memory_bytes IS DISTINCT FROM OLD.reserved_device_memory_bytes
        OR NEW.reserved_host_memory_bytes IS DISTINCT FROM OLD.reserved_host_memory_bytes
        OR NEW.reserved_cpu_decode_millicores IS DISTINCT FROM OLD.reserved_cpu_decode_millicores
        OR NEW.reserved_host_pcm_buffer_bytes IS DISTINCT FROM OLD.reserved_host_pcm_buffer_bytes
        OR NEW.reserved_scratch_disk_bytes IS DISTINCT FROM OLD.reserved_scratch_disk_bytes
        OR NEW.admitted_at IS DISTINCT FROM OLD.admitted_at
        OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at THEN
        RAISE EXCEPTION 'admission lease identity and reservation are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_admission_leases_identity_and_release_terminality
    BEFORE UPDATE ON admission_leases
    FOR EACH ROW EXECUTE FUNCTION enforce_admission_lease_identity_and_release_terminality();

-- Submit intent identity links one admitted execution to one engine request.
-- Final execution results cannot be revived, and quarantine can only close as
-- aborted after verified stop evidence; cleanup facts remain updateable.
CREATE FUNCTION enforce_submit_intent_identity_and_terminality() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.execution_state IN ('succeeded', 'failed', 'aborted')
        AND NEW.execution_state IS DISTINCT FROM OLD.execution_state THEN
        RAISE EXCEPTION 'terminal submit intent execution state is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.execution_state = 'unknown_quarantined'
        AND NEW.execution_state NOT IN ('unknown_quarantined', 'aborted') THEN
        RAISE EXCEPTION 'quarantined submit intent may only close as aborted'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.submit_intent_id IS DISTINCT FROM OLD.submit_intent_id
        OR NEW.lease_id IS DISTINCT FROM OLD.lease_id
        OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
        OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
        OR NEW.stage_id IS DISTINCT FROM OLD.stage_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.pool_id IS DISTINCT FROM OLD.pool_id
        OR NEW.host_node_id IS DISTINCT FROM OLD.host_node_id
        OR NEW.gateway_id IS DISTINCT FROM OLD.gateway_id
        OR NEW.instance_id IS DISTINCT FROM OLD.instance_id
        OR NEW.instance_generation IS DISTINCT FROM OLD.instance_generation
        OR NEW.binding_revision_id IS DISTINCT FROM OLD.binding_revision_id
        OR NEW.policy_revision_id IS DISTINCT FROM OLD.policy_revision_id
        OR NEW.voice_revision_id IS DISTINCT FROM OLD.voice_revision_id
        OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
        OR NEW.command_digest IS DISTINCT FROM OLD.command_digest
        OR NEW.recorded_at IS DISTINCT FROM OLD.recorded_at
        OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at THEN
        RAISE EXCEPTION 'submit intent identity is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.engine_request_id IS NOT NULL
        AND NEW.engine_request_id IS DISTINCT FROM OLD.engine_request_id THEN
        RAISE EXCEPTION 'engine request association is immutable once observed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.dispatched_at IS NOT NULL
        AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at THEN
        RAISE EXCEPTION 'execution dispatch time is immutable once observed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.staging_output_object_key IS NOT NULL
        AND NEW.staging_output_object_key IS DISTINCT FROM OLD.staging_output_object_key THEN
        RAISE EXCEPTION 'staging output identity is immutable once observed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.terminated_at IS NOT NULL
        AND NEW.terminated_at IS DISTINCT FROM OLD.terminated_at THEN
        RAISE EXCEPTION 'execution termination time is immutable once observed'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.execution_state IN ('succeeded', 'failed', 'aborted') THEN
        IF NEW.stop_evidence_kind IS DISTINCT FROM OLD.stop_evidence_kind THEN
            RAISE EXCEPTION 'terminal execution evidence is immutable'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.error_code IS DISTINCT FROM OLD.error_code THEN
            RAISE EXCEPTION 'terminal execution error is immutable'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_submit_intents_identity_and_terminality
    BEFORE UPDATE ON submit_intents
    FOR EACH ROW EXECUTE FUNCTION enforce_submit_intent_identity_and_terminality();

-- A submit intent may only be created while its admission lease is still
-- executable. Locking the lease row also serializes this check with lease
-- release / quarantine transitions, so a concurrent release cannot win after
-- observing no submit intent and then let a late insert through.
CREATE FUNCTION enforce_submit_intent_live_lease() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    lease_state_value TEXT;
    cleanup_state_value TEXT;
BEGIN
    SELECT lease_state, cleanup_state
    INTO lease_state_value, cleanup_state_value
    FROM admission_leases
    WHERE lease_id = NEW.lease_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'submit intent requires an existing admission lease'
            USING ERRCODE = '23503';
    END IF;
    IF lease_state_value NOT IN ('reserved', 'executing')
        OR cleanup_state_value <> 'pending' THEN
        RAISE EXCEPTION 'submit intent requires a live admission lease'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_submit_intents_live_lease
    BEFORE INSERT ON submit_intents
    FOR EACH ROW EXECUTE FUNCTION enforce_submit_intent_live_lease();

-- A lease releases capacity only after every execution intent attached to it
-- has reached a terminal execution state. A quarantined lease may retain an
-- unknown execution, but it cannot be marked released until that execution is
-- closed with verified evidence.
CREATE FUNCTION enforce_admission_lease_terminality() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lease_state IS NOT DISTINCT FROM OLD.lease_state
        OR NEW.lease_state NOT IN ('released', 'quarantined') THEN
        RETURN NEW;
    END IF;

    PERFORM 1
    FROM submit_intents
    WHERE lease_id = NEW.lease_id
      AND execution_state IN ('submitting', 'executing', 'stopping')
    FOR UPDATE;
    IF FOUND THEN
        RAISE EXCEPTION 'lease cannot release while submit intent is active'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.lease_state = 'released' THEN
        PERFORM 1
        FROM submit_intents
        WHERE lease_id = NEW.lease_id
          AND execution_state = 'unknown_quarantined'
        FOR UPDATE;
        IF FOUND THEN
            RAISE EXCEPTION 'lease cannot release an unresolved quarantined execution'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_admission_leases_terminality
    BEFORE UPDATE ON admission_leases
    FOR EACH ROW EXECUTE FUNCTION enforce_admission_lease_terminality();

-- Marking a runtime generation stopped is the final capacity-release fact.
-- No admission lease or non-terminal submit intent may still reference it.
CREATE FUNCTION enforce_engine_instance_stop_cleanup() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.instance_state <> 'stopped'
        OR OLD.instance_state = 'stopped' THEN
        RETURN NEW;
    END IF;

    PERFORM 1
    FROM admission_leases
    WHERE instance_id = NEW.instance_id
      AND instance_generation = NEW.instance_generation
      AND lease_state <> 'released'
    FOR UPDATE;
    IF FOUND THEN
        RAISE EXCEPTION 'instance cannot stop with an unreleased admission lease'
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1
    FROM submit_intents
    WHERE instance_id = NEW.instance_id
      AND instance_generation = NEW.instance_generation
      AND execution_state NOT IN ('succeeded', 'failed', 'aborted')
    FOR UPDATE;
    IF FOUND THEN
        RAISE EXCEPTION 'instance cannot stop with a non-terminal submit intent'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_engine_instances_stop_cleanup
    BEFORE UPDATE ON engine_instances
    FOR EACH ROW EXECUTE FUNCTION enforce_engine_instance_stop_cleanup();

-- The scalar submit watermark is a query projection over interval rows. Keep
-- its position equal to the generated interval high-water mark, and freeze
-- stream identity once it is declared so a retry cannot rewrite the
-- authoritative sample coordinate system.
CREATE FUNCTION enforce_submit_output_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    interval_high_water BIGINT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.generated_pcm_samples <> 0 THEN
            RAISE EXCEPTION 'generated PCM sample watermark must start at zero'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.generated_pcm_samples IS DISTINCT FROM OLD.generated_pcm_samples THEN
        SELECT COALESCE(MAX(sample_end), 0)
        INTO interval_high_water
        FROM output_sample_intervals
        WHERE submit_intent_id = NEW.submit_intent_id
          AND meter_kind = 'generated';

        IF NEW.generated_pcm_samples IS DISTINCT FROM interval_high_water THEN
            RAISE EXCEPTION 'generated PCM watermark must equal the generated interval high-water mark'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.generated_pcm_samples < OLD.generated_pcm_samples THEN
        RAISE EXCEPTION 'generated PCM sample watermark cannot decrease'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.output_stream_id IS NOT NULL
        AND NEW.output_stream_id IS DISTINCT FROM OLD.output_stream_id THEN
        RAISE EXCEPTION 'output stream identity is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.sample_rate_hz IS NOT NULL
        AND NEW.sample_rate_hz IS DISTINCT FROM OLD.sample_rate_hz THEN
        RAISE EXCEPTION 'output sample rate is immutable'
            USING ERRCODE = '23514';
    END IF;
    -- The hard ceiling is part of the admitted execution envelope. It may be
    -- initialized before output evidence exists, but cannot move afterwards.
    IF OLD.hard_output_ceiling_samples IS NOT NULL
        AND NEW.hard_output_ceiling_samples IS DISTINCT FROM OLD.hard_output_ceiling_samples THEN
        RAISE EXCEPTION 'hard output ceiling is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.hard_output_ceiling_samples IS NULL
        AND OLD.generated_pcm_samples > 0
        AND NEW.hard_output_ceiling_samples IS NOT NULL THEN
        RAISE EXCEPTION 'hard output ceiling cannot be introduced after generated output'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_submit_output_monotonic
    BEFORE UPDATE ON submit_intents
    FOR EACH ROW EXECUTE FUNCTION enforce_submit_output_monotonic();
CREATE TRIGGER trg_submit_output_projection_insert
    BEFORE INSERT ON submit_intents
    FOR EACH ROW EXECUTE FUNCTION enforce_submit_output_monotonic();

-- Keep audit timestamps current even when application code forgets to set them.
CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_host_nodes_updated_at
    BEFORE UPDATE ON host_nodes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_resource_pools_updated_at
    BEFORE UPDATE ON resource_pools
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_engine_instances_updated_at
    BEFORE UPDATE ON engine_instances
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_runtime_instance_intents_updated_at
    BEFORE UPDATE ON runtime_instance_intents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_admission_leases_updated_at
    BEFORE UPDATE ON admission_leases
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_gateway_fencing_barriers_updated_at
    BEFORE UPDATE ON gateway_fencing_barriers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_submit_intents_updated_at
    BEFORE UPDATE ON submit_intents
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
