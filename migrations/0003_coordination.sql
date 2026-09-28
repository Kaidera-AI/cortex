DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'coordination migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

CREATE SCHEMA IF NOT EXISTS cortex_coord AUTHORIZATION cortex_v2_migrator;

-- ---------------------------------------------------------------------------
-- W2 coordination plane: handoffs with fenced claim generations, returns,
-- reviews, approvals and human gates, cross-project relays with immutable
-- targets and durable receipts, epics/waves/boards/tasks with deterministic
-- dispatch eligibility, work-product receipts with pinned freshness, and the
-- typed audit log plus transactional outbox every command must write.
--
-- Depends only on 0001_core.sql and 0002_w1_identity_memory.sql: scope
-- policy helpers (cortex_core.scope_read_policy/scope_write_policy), scope
-- grants/memberships/actors, writer-policy revisions, command receipts and
-- the canonical content spine for work-product references.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_coord.require_declared_policy_revision(p_scope_id uuid)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
DECLARE
    declared_revision text;
    current_revision text;
BEGIN
    SELECT COALESCE(max(revision), 0)::text
      INTO current_revision
      FROM cortex_auth.writer_policies
     WHERE scope_id = p_scope_id;
    declared_revision := NULLIF(
        current_setting('cortex.policy_revision', true), ''
    );
    IF declared_revision IS DISTINCT FROM current_revision THEN
        RAISE EXCEPTION 'writer-policy revision recheck failed; re-resolve scope policy'
            USING ERRCODE = '55000';
    END IF;
END
$function$;

CREATE FUNCTION cortex_coord.require_current_principal(p_principal_id uuid)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_principal_id IS DISTINCT FROM NULLIF(
        current_setting('cortex.principal_id', true), ''
    )::uuid THEN
        RAISE EXCEPTION 'coordination writes are bound to the authenticated principal'
            USING ERRCODE = '42501';
    END IF;
END
$function$;

CREATE FUNCTION cortex_coord.require_human_lead(
    p_scope_id uuid,
    p_principal_id uuid
)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_auth.memberships AS m
          JOIN cortex_auth.actor_bindings AS b ON b.actor_id = m.actor_id
          JOIN cortex_auth.actors AS a ON a.actor_id = m.actor_id
         WHERE m.scope_id = p_scope_id
           AND b.principal_id = p_principal_id
           AND m.status = 'active'
           AND a.status = 'active'
           AND a.actor_kind = 'human'
           AND m.membership_role IN ('owner', 'lead')
    ) THEN
        RAISE EXCEPTION 'operation requires a human owner or lead of the scope'
            USING ERRCODE = '42501';
    END IF;
END
$function$;

CREATE FUNCTION cortex_coord.reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

-- ---------------------------------------------------------------------------
-- Epics and waves (ordered, optionally human-gated).
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.epics (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    epic_id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    description text CHECK (description IS NULL OR length(description) BETWEEN 1 AND 512),
    status text NOT NULL DEFAULT 'active' CHECK (status = 'active'),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, epic_id),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_coord.waves (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    wave_id uuid NOT NULL,
    epic_id uuid NOT NULL,
    wave_index integer NOT NULL CHECK (wave_index > 0),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'open', 'completed')),
    requires_human_gate boolean NOT NULL DEFAULT false,
    gate_approval_id uuid,
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, wave_id),
    UNIQUE (scope_id, epic_id, wave_index),
    FOREIGN KEY (scope_id, epic_id)
        REFERENCES cortex_coord.epics(scope_id, epic_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK (gate_approval_id IS NULL OR requires_human_gate)
);

CREATE INDEX waves_scope_status ON cortex_coord.waves(scope_id, status);

-- ---------------------------------------------------------------------------
-- Tasks, dependencies, assignments and boards.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.tasks (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    task_id uuid NOT NULL,
    epic_id uuid,
    wave_id uuid,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
    brief text CHECK (brief IS NULL OR length(brief) BETWEEN 1 AND 65536),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'dispatched', 'completed', 'cancelled')),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, task_id),
    FOREIGN KEY (scope_id, epic_id)
        REFERENCES cortex_coord.epics(scope_id, epic_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, wave_id)
        REFERENCES cortex_coord.waves(scope_id, wave_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX tasks_scope_status ON cortex_coord.tasks(scope_id, status);
CREATE INDEX tasks_scope_wave_status ON cortex_coord.tasks(scope_id, wave_id, status);
CREATE INDEX tasks_scope_epic ON cortex_coord.tasks(scope_id, epic_id);

CREATE TABLE cortex_coord.task_dependencies (
    scope_id uuid NOT NULL,
    task_id uuid NOT NULL,
    depends_on_task_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, task_id, depends_on_task_id),
    FOREIGN KEY (scope_id, task_id)
        REFERENCES cortex_coord.tasks(scope_id, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, depends_on_task_id)
        REFERENCES cortex_coord.tasks(scope_id, task_id)
        ON DELETE RESTRICT,
    CHECK (task_id <> depends_on_task_id)
);

CREATE INDEX task_dependencies_target
    ON cortex_coord.task_dependencies(scope_id, depends_on_task_id);

CREATE TABLE cortex_coord.task_assignments (
    scope_id uuid NOT NULL,
    task_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    assignment_role text NOT NULL
        CHECK (assignment_role IN ('responsible', 'supporting')),
    assigned_by_principal uuid NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz,
    PRIMARY KEY (scope_id, task_id, actor_id),
    FOREIGN KEY (scope_id, task_id)
        REFERENCES cortex_coord.tasks(scope_id, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, actor_id)
        REFERENCES cortex_auth.memberships(scope_id, actor_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (assigned_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX task_assignments_active_responsible
    ON cortex_coord.task_assignments(scope_id, task_id)
    WHERE retired_at IS NULL;

CREATE TABLE cortex_coord.boards (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    board_id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, board_id),
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_coord.board_tasks (
    scope_id uuid NOT NULL,
    board_id uuid NOT NULL,
    task_id uuid NOT NULL,
    column_name text NOT NULL DEFAULT 'backlog'
        CHECK (length(column_name) BETWEEN 1 AND 64),
    added_by_principal uuid NOT NULL,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, board_id, task_id),
    FOREIGN KEY (scope_id, board_id)
        REFERENCES cortex_coord.boards(scope_id, board_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, task_id)
        REFERENCES cortex_coord.tasks(scope_id, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (added_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX board_tasks_scope_task ON cortex_coord.board_tasks(scope_id, task_id);

-- ---------------------------------------------------------------------------
-- Handoffs: canonical row, fenced claims, returns, reviews, work-product
-- receipts.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.handoffs (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    handoff_id uuid NOT NULL,
    dedup_key text CHECK (dedup_key IS NULL OR length(dedup_key) BETWEEN 1 AND 128),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 256),
    brief text NOT NULL CHECK (length(brief) BETWEEN 1 AND 65536),
    addressed_role text CHECK (addressed_role IS NULL OR length(addressed_role) BETWEEN 1 AND 64),
    addressed_actor_id uuid,
    task_id uuid,
    status text NOT NULL DEFAULT 'open' CHECK (status IN (
        'open', 'claimed', 'returned', 'accepted',
        'rework', 'failed', 'abandoned', 'withdrawn'
    )),
    claim_generation integer NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    active_lease_expires_at timestamptz,
    require_human_accept boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    relay_id uuid,
    relay_source_scope_id uuid REFERENCES cortex_core.scopes(scope_id),
    relay_source_handoff_id uuid,
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, handoff_id),
    FOREIGN KEY (scope_id, task_id)
        REFERENCES cortex_coord.tasks(scope_id, task_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, addressed_actor_id)
        REFERENCES cortex_auth.memberships(scope_id, actor_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK ((status = 'claimed') = (active_lease_expires_at IS NOT NULL)),
    CHECK (relay_id IS NULL
           OR (relay_source_scope_id IS NOT NULL
               AND relay_source_handoff_id IS NOT NULL)),
    CHECK (relay_id IS NOT NULL
           OR (relay_source_scope_id IS NULL
               AND relay_source_handoff_id IS NULL))
);

CREATE UNIQUE INDEX handoffs_unique_dedup_key
    ON cortex_coord.handoffs(scope_id, dedup_key)
    WHERE dedup_key IS NOT NULL;

CREATE INDEX handoffs_claimable_queue
    ON cortex_coord.handoffs(scope_id, created_at, handoff_id)
    WHERE status IN ('open', 'rework');

CREATE INDEX handoffs_scope_created
    ON cortex_coord.handoffs(scope_id, created_at DESC, handoff_id DESC);

CREATE TABLE cortex_coord.claims (
    scope_id uuid NOT NULL,
    handoff_id uuid NOT NULL,
    claim_generation integer NOT NULL CHECK (claim_generation > 0),
    claimant_principal_id uuid NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    lease_expires_at timestamptz NOT NULL,
    ended_at timestamptz,
    end_reason text CHECK (end_reason IS NULL OR end_reason IN (
        'released', 'returned', 'abandoned', 'lease_expired',
        'failed', 'withdrawn'
    )),
    PRIMARY KEY (scope_id, handoff_id, claim_generation),
    FOREIGN KEY (scope_id, handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (claimant_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);

CREATE INDEX claims_active_leases
    ON cortex_coord.claims(lease_expires_at)
    WHERE ended_at IS NULL;

CREATE TABLE cortex_coord.returns (
    scope_id uuid NOT NULL,
    handoff_id uuid NOT NULL,
    return_seq integer NOT NULL CHECK (return_seq > 0),
    claim_generation integer NOT NULL CHECK (claim_generation > 0),
    returned_by_principal uuid NOT NULL,
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, handoff_id, return_seq),
    FOREIGN KEY (scope_id, handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (returned_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_coord.work_product_receipts (
    scope_id uuid NOT NULL,
    receipt_id uuid NOT NULL,
    handoff_id uuid NOT NULL,
    return_seq integer NOT NULL,
    content_id uuid NOT NULL,
    content_revision integer NOT NULL CHECK (content_revision > 0),
    content_hash bytea NOT NULL CHECK (octet_length(content_hash) = 32),
    evidence_class text NOT NULL CHECK (evidence_class IN (
        'work_product', 'test_output', 'review_note',
        'artifact', 'document', 'other'
    )),
    attestation text NOT NULL CHECK (attestation = 'self_reported'),
    created_by_principal uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, receipt_id),
    FOREIGN KEY (scope_id, handoff_id, return_seq)
        REFERENCES cortex_coord.returns(scope_id, handoff_id, return_seq)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (created_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX work_product_receipts_handoff
    ON cortex_coord.work_product_receipts(scope_id, handoff_id);
CREATE INDEX work_product_receipts_content
    ON cortex_coord.work_product_receipts(scope_id, content_id);

-- ---------------------------------------------------------------------------
-- Approvals and human gates. Subjects and relay targets are immutable
-- inputs; only the decision lifecycle mutates, and only by a human
-- owner/lead of the scope.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.approvals (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    approval_id uuid NOT NULL,
    gate_kind text NOT NULL
        CHECK (gate_kind IN ('relay', 'wave', 'handoff_accept')),
    subject_handoff_id uuid,
    subject_wave_id uuid,
    relay_source_handoff_id uuid,
    relay_target_scope_id uuid REFERENCES cortex_core.scopes(scope_id),
    relay_target_actor_id uuid,
    requested_by_principal uuid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now(),
    decision text NOT NULL DEFAULT 'pending'
        CHECK (decision IN ('pending', 'approved', 'denied', 'revoked')),
    decided_by_principal uuid,
    decided_at timestamptz,
    expires_at timestamptz,
    note text CHECK (note IS NULL OR length(note) BETWEEN 1 AND 512),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (scope_id, approval_id),
    FOREIGN KEY (scope_id, subject_handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, subject_wave_id)
        REFERENCES cortex_coord.waves(scope_id, wave_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, relay_source_handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (requested_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (decided_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK ((gate_kind = 'handoff_accept') = (subject_handoff_id IS NOT NULL)),
    CHECK ((gate_kind = 'wave') = (subject_wave_id IS NOT NULL)),
    CHECK ((gate_kind = 'relay') = (relay_source_handoff_id IS NOT NULL)),
    CHECK ((relay_source_handoff_id IS NULL) = (relay_target_scope_id IS NULL)),
    CHECK ((relay_source_handoff_id IS NULL) = (relay_target_actor_id IS NULL)),
    CHECK (relay_target_scope_id IS NULL OR relay_target_scope_id <> scope_id),
    CHECK ((decision = 'pending') = (decided_by_principal IS NULL)),
    CHECK ((decision = 'pending') = (decided_at IS NULL))
);

CREATE INDEX approvals_scope_pending
    ON cortex_coord.approvals(scope_id, requested_at)
    WHERE decision = 'pending';

ALTER TABLE cortex_coord.waves
    ADD CONSTRAINT waves_gate_approval_fk
    FOREIGN KEY (scope_id, gate_approval_id)
    REFERENCES cortex_coord.approvals(scope_id, approval_id)
    ON DELETE RESTRICT;

CREATE TABLE cortex_coord.reviews (
    scope_id uuid NOT NULL,
    handoff_id uuid NOT NULL,
    review_seq integer NOT NULL CHECK (review_seq > 0),
    decision text NOT NULL CHECK (decision IN ('accept', 'rework')),
    approval_id uuid,
    reviewer_principal_id uuid NOT NULL,
    note text CHECK (note IS NULL OR length(note) BETWEEN 1 AND 65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, handoff_id, review_seq),
    FOREIGN KEY (scope_id, handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, approval_id)
        REFERENCES cortex_coord.approvals(scope_id, approval_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (reviewer_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Cross-project relays: explicit authorization bound to an approved exact
-- immutable target, durable source-side dispatch receipt, single
-- materialization in the target scope.
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.relay_authorizations (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    relay_id uuid NOT NULL,
    source_handoff_id uuid NOT NULL,
    target_scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    target_actor_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'authorized'
        CHECK (status IN ('authorized', 'consumed', 'revoked')),
    authorized_by_principal uuid NOT NULL,
    authorized_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (scope_id, relay_id),
    FOREIGN KEY (scope_id, source_handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, approval_id)
        REFERENCES cortex_coord.approvals(scope_id, approval_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (authorized_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK (target_scope_id <> scope_id),
    CHECK ((status = 'consumed') = (consumed_at IS NOT NULL)),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE UNIQUE INDEX relay_authorizations_unique_target
    ON cortex_coord.relay_authorizations(
        scope_id, source_handoff_id, target_scope_id, target_actor_id
    );

CREATE INDEX relay_authorizations_scope_status
    ON cortex_coord.relay_authorizations(scope_id, status);

CREATE TABLE cortex_coord.relay_receipts (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    relay_id uuid NOT NULL,
    source_handoff_id uuid NOT NULL,
    target_scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    target_actor_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    handoff_snapshot jsonb NOT NULL,
    dispatched_by_principal uuid NOT NULL,
    dispatched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, relay_id),
    FOREIGN KEY (scope_id, relay_id)
        REFERENCES cortex_coord.relay_authorizations(scope_id, relay_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (dispatched_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX relay_receipts_unique_relay
    ON cortex_coord.relay_receipts(relay_id);

CREATE TABLE cortex_coord.relay_materializations (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    relay_id uuid NOT NULL,
    source_scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    source_handoff_id uuid NOT NULL,
    handoff_id uuid NOT NULL,
    materialized_by_principal uuid NOT NULL,
    materialized_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, relay_id),
    FOREIGN KEY (scope_id, handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (materialized_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX relay_materializations_unique_relay
    ON cortex_coord.relay_materializations(relay_id);

-- ---------------------------------------------------------------------------
-- Typed audit log and transactional outbox. Every coordination command
-- commits its canonical row, one audit row and one outbox event together
-- with its idempotency receipt (cortex_core.command_receipts).
-- ---------------------------------------------------------------------------

CREATE TABLE cortex_coord.audit_log (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    aggregate_kind text NOT NULL CHECK (aggregate_kind IN (
        'handoff', 'task', 'wave', 'epic', 'board', 'approval', 'relay'
    )),
    aggregate_id uuid NOT NULL,
    audit_seq integer NOT NULL CHECK (audit_seq > 0),
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 128),
    actor_principal_id uuid NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, aggregate_kind, aggregate_id, audit_seq),
    FOREIGN KEY (actor_principal_id, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT
);

CREATE INDEX audit_log_scope_created
    ON cortex_coord.audit_log(scope_id, created_at DESC);

CREATE TABLE cortex_coord.outbox_events (
    event_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    aggregate_kind text NOT NULL CHECK (aggregate_kind IN (
        'handoff', 'task', 'wave', 'epic', 'board', 'approval', 'relay'
    )),
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

CREATE INDEX coordination_outbox_scope_created
    ON cortex_coord.outbox_events(scope_id, created_at, event_id);

CREATE INDEX coordination_outbox_undelivered
    ON cortex_coord.outbox_events(scope_id, created_at)
    WHERE delivered_at IS NULL;

-- ---------------------------------------------------------------------------
-- Trigger enforcement: the concurrency-safe boundary behind the Python
-- pre-validation. Transition legality, claim generation and lease rules,
-- review authority, relay consumption and writer-policy rechecks are all
-- enforced here so no path can bypass them.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_coord.validate_handoff_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);
    IF NEW.status <> 'open' OR NEW.claim_generation <> 0
       OR NEW.revision <> 1 OR NEW.active_lease_expires_at IS NOT NULL THEN
        RAISE EXCEPTION 'handoffs are created open, unclaimed, at revision 1'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER handoffs_validate_insert
    BEFORE INSERT ON cortex_coord.handoffs
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_handoff_insert();

CREATE FUNCTION cortex_coord.validate_handoff_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    legal boolean := false;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.handoff_id <> OLD.handoff_id
       OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key
       OR NEW.title <> OLD.title
       OR NEW.brief <> OLD.brief
       OR NEW.addressed_role IS DISTINCT FROM OLD.addressed_role
       OR NEW.addressed_actor_id IS DISTINCT FROM OLD.addressed_actor_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.require_human_accept <> OLD.require_human_accept
       OR NEW.created_by_principal <> OLD.created_by_principal
       OR NEW.created_at <> OLD.created_at
       OR NEW.relay_id IS DISTINCT FROM OLD.relay_id
       OR NEW.relay_source_scope_id IS DISTINCT FROM OLD.relay_source_scope_id
       OR NEW.relay_source_handoff_id IS DISTINCT FROM OLD.relay_source_handoff_id THEN
        RAISE EXCEPTION 'handoff identity and acceptance rules are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'handoff revision must advance by exactly one'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.claim_generation < OLD.claim_generation THEN
        RAISE EXCEPTION 'claim generation is monotonically increasing'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status IN ('accepted', 'abandoned', 'withdrawn') THEN
        RAISE EXCEPTION 'terminal handoffs cannot change' USING ERRCODE = '23514';
    END IF;

    IF NEW.status = 'claimed' THEN
        IF NEW.active_lease_expires_at IS NULL
           OR NEW.active_lease_expires_at <= now() THEN
            RAISE EXCEPTION 'a claim requires a future lease'
                USING ERRCODE = '23514';
        END IF;
        IF OLD.status = 'claimed' THEN
            IF NEW.claim_generation = OLD.claim_generation THEN
                -- Explicit lease renewal of the active claim.
                IF OLD.active_lease_expires_at <= now() THEN
                    RAISE EXCEPTION 'an expired lease cannot be renewed'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.active_lease_expires_at <= OLD.active_lease_expires_at THEN
                    RAISE EXCEPTION 'lease renewal must extend the lease'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.claim_generation = OLD.claim_generation + 1 THEN
                -- Reclaim strictly after lease expiry (fencing advances).
                IF OLD.active_lease_expires_at > now() THEN
                    RAISE EXCEPTION 'a live claim cannot be reclaimed'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'claim generation must advance by exactly one per claim'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF OLD.status IN ('open', 'rework') THEN
            IF NEW.claim_generation <> OLD.claim_generation + 1 THEN
                RAISE EXCEPTION 'claim generation must advance by exactly one per claim'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'handoffs are claimed only from open or rework'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.active_lease_expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'only claimed handoffs carry an active lease'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.claim_generation <> OLD.claim_generation THEN
            RAISE EXCEPTION 'claim generation changes only through claims'
                USING ERRCODE = '23514';
        END IF;
        legal := (OLD.status = 'claimed'
                  AND NEW.status IN ('open', 'returned', 'failed',
                                     'abandoned', 'withdrawn'))
              OR (OLD.status = 'returned'
                  AND NEW.status IN ('accepted', 'rework', 'withdrawn'))
              OR (OLD.status = 'failed'
                  AND NEW.status IN ('open', 'withdrawn'));
        IF NOT legal THEN
            RAISE EXCEPTION 'handoff transition % -> % is not allowed',
                OLD.status, NEW.status
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END
$function$;

CREATE TRIGGER handoffs_validate_update
    BEFORE UPDATE ON cortex_coord.handoffs
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_handoff_update();

CREATE TRIGGER handoffs_reject_delete
    BEFORE DELETE ON cortex_coord.handoffs
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_claim_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    handoff_status text;
    handoff_generation integer;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.claimant_principal_id);

    SELECT status, claim_generation
      INTO handoff_status, handoff_generation
      FROM cortex_coord.handoffs
     WHERE scope_id = NEW.scope_id AND handoff_id = NEW.handoff_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a claim requires its handoff' USING ERRCODE = '23503';
    END IF;
    IF handoff_status <> 'claimed'
       OR handoff_generation <> NEW.claim_generation THEN
        RAISE EXCEPTION 'claims record exactly the active claim generation'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.lease_expires_at <= now() THEN
        RAISE EXCEPTION 'a claim requires a future lease' USING ERRCODE = '23514';
    END IF;
    IF NEW.ended_at IS NOT NULL OR NEW.end_reason IS NOT NULL THEN
        RAISE EXCEPTION 'claims start active' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER claims_validate_insert
    BEFORE INSERT ON cortex_coord.claims
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_claim_insert();

CREATE FUNCTION cortex_coord.validate_claim_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.handoff_id <> OLD.handoff_id
       OR NEW.claim_generation <> OLD.claim_generation
       OR NEW.claimant_principal_id <> OLD.claimant_principal_id
       OR NEW.claimed_at <> OLD.claimed_at THEN
        RAISE EXCEPTION 'claim identity is immutable' USING ERRCODE = '55000';
    END IF;

    IF OLD.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'ended claims are immutable' USING ERRCODE = '55000';
    END IF;

    IF NEW.ended_at IS NULL THEN
        -- Forward-only lease renewal while the claim is active.
        IF NEW.lease_expires_at <= OLD.lease_expires_at THEN
            RAISE EXCEPTION 'lease renewal must extend the lease'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.ended_at < OLD.claimed_at OR NEW.end_reason IS NULL THEN
            RAISE EXCEPTION 'claim termination requires a reason after claiming'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER claims_validate_update
    BEFORE UPDATE ON cortex_coord.claims
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_claim_update();

CREATE TRIGGER claims_reject_delete
    BEFORE DELETE ON cortex_coord.claims
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_return_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    handoff_status text;
    handoff_generation integer;
    claim_end_reason text;
    claim_ended_at timestamptz;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.returned_by_principal);

    SELECT status, claim_generation
      INTO handoff_status, handoff_generation
      FROM cortex_coord.handoffs
     WHERE scope_id = NEW.scope_id AND handoff_id = NEW.handoff_id;
    IF NOT FOUND OR handoff_status <> 'returned' THEN
        RAISE EXCEPTION 'returns attach to a returned handoff'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.claim_generation <> handoff_generation THEN
        RAISE EXCEPTION 'a stale claim generation can never publish a return'
            USING ERRCODE = '23514';
    END IF;

    SELECT ended_at, end_reason
      INTO claim_ended_at, claim_end_reason
      FROM cortex_coord.claims
     WHERE scope_id = NEW.scope_id
       AND handoff_id = NEW.handoff_id
       AND claim_generation = NEW.claim_generation;
    IF NOT FOUND OR claim_ended_at IS NULL OR claim_end_reason <> 'returned' THEN
        RAISE EXCEPTION 'a return requires its claim ended as returned'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(return_seq), 0) + 1
      INTO NEW.return_seq
      FROM cortex_coord.returns
     WHERE scope_id = NEW.scope_id AND handoff_id = NEW.handoff_id;
    RETURN NEW;
END
$function$;

CREATE TRIGGER returns_validate_insert
    BEFORE INSERT ON cortex_coord.returns
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_return_insert();

CREATE TRIGGER returns_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.returns
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_receipt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, cortex_core, pg_temp
AS $function$
DECLARE
    pinned_hash bytea;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);

    IF NEW.attestation <> 'self_reported' THEN
        RAISE EXCEPTION 'coordination receipts never claim independent verification'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM cortex_coord.returns
         WHERE scope_id = NEW.scope_id
           AND handoff_id = NEW.handoff_id
           AND return_seq = NEW.return_seq
    ) THEN
        RAISE EXCEPTION 'work-product receipts attach to a return'
            USING ERRCODE = '23503';
    END IF;

    SELECT content_hash
      INTO pinned_hash
      FROM cortex_core.content_revisions
     WHERE scope_id = NEW.scope_id
       AND content_id = NEW.content_id
       AND revision = NEW.content_revision;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'receipts pin an existing content revision'
            USING ERRCODE = '23503';
    END IF;
    IF pinned_hash <> NEW.content_hash THEN
        RAISE EXCEPTION 'receipts pin the exact content hash of the revision'
            USING ERRCODE = '23514';
    END IF;
    IF cortex_core.content_current_status(NEW.scope_id, NEW.content_id)
       <> 'current' THEN
        RAISE EXCEPTION 'receipts pin content in the current status'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER work_product_receipts_validate_insert
    BEFORE INSERT ON cortex_coord.work_product_receipts
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_receipt_insert();

CREATE TRIGGER work_product_receipts_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.work_product_receipts
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_review_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    handoff_status text;
    requires_human boolean;
    approval record;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.reviewer_principal_id);

    SELECT status, require_human_accept
      INTO handoff_status, requires_human
      FROM cortex_coord.handoffs
     WHERE scope_id = NEW.scope_id AND handoff_id = NEW.handoff_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reviews attach to a handoff' USING ERRCODE = '23503';
    END IF;
    IF (NEW.decision = 'accept' AND handoff_status <> 'accepted')
       OR (NEW.decision = 'rework' AND handoff_status <> 'rework') THEN
        RAISE EXCEPTION 'review decisions match the committed handoff state'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.decision = 'accept' AND requires_human THEN
        IF NEW.approval_id IS NULL THEN
            RAISE EXCEPTION 'human-gated acceptance requires an approved gate'
                USING ERRCODE = '23514';
        END IF;
        SELECT decision, gate_kind, subject_handoff_id, expires_at
          INTO approval
          FROM cortex_coord.approvals
         WHERE scope_id = NEW.scope_id AND approval_id = NEW.approval_id;
        IF NOT FOUND
           OR approval.decision <> 'approved'
           OR approval.gate_kind <> 'handoff_accept'
           OR approval.subject_handoff_id <> NEW.handoff_id
           OR (approval.expires_at IS NOT NULL
               AND approval.expires_at <= now()) THEN
            RAISE EXCEPTION 'human-gated acceptance requires a live approved gate for this handoff'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    SELECT COALESCE(max(review_seq), 0) + 1
      INTO NEW.review_seq
      FROM cortex_coord.reviews
     WHERE scope_id = NEW.scope_id AND handoff_id = NEW.handoff_id;
    RETURN NEW;
END
$function$;

CREATE TRIGGER reviews_validate_insert
    BEFORE INSERT ON cortex_coord.reviews
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_review_insert();

CREATE TRIGGER reviews_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.reviews
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_approval_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.requested_by_principal);
    IF NEW.decision <> 'pending' THEN
        RAISE EXCEPTION 'approvals start pending' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER approvals_validate_insert
    BEFORE INSERT ON cortex_coord.approvals
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_approval_insert();

CREATE FUNCTION cortex_coord.validate_approval_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.approval_id <> OLD.approval_id
       OR NEW.gate_kind <> OLD.gate_kind
       OR NEW.subject_handoff_id IS DISTINCT FROM OLD.subject_handoff_id
       OR NEW.subject_wave_id IS DISTINCT FROM OLD.subject_wave_id
       OR NEW.relay_source_handoff_id IS DISTINCT FROM OLD.relay_source_handoff_id
       OR NEW.relay_target_scope_id IS DISTINCT FROM OLD.relay_target_scope_id
       OR NEW.relay_target_actor_id IS DISTINCT FROM OLD.relay_target_actor_id
       OR NEW.requested_by_principal <> OLD.requested_by_principal
       OR NEW.requested_at <> OLD.requested_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.detail <> OLD.detail THEN
        RAISE EXCEPTION 'approval subjects and targets are immutable inputs'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        (OLD.decision = 'pending' AND NEW.decision IN ('approved', 'denied'))
        OR (OLD.decision = 'approved' AND NEW.decision = 'revoked')
    ) THEN
        RAISE EXCEPTION 'approval decision transition % -> % is not allowed',
            OLD.decision, NEW.decision
            USING ERRCODE = '23514';
    END IF;

    PERFORM cortex_coord.require_current_principal(NEW.decided_by_principal);
    PERFORM cortex_coord.require_human_lead(NEW.scope_id, NEW.decided_by_principal);

    IF NEW.decision = 'approved'
       AND NEW.expires_at IS NOT NULL
       AND NEW.expires_at <= now() THEN
        RAISE EXCEPTION 'an expired approval cannot be granted'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.decided_at IS NULL THEN
        RAISE EXCEPTION 'decisions require a decision time' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER approvals_validate_update
    BEFORE UPDATE ON cortex_coord.approvals
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_approval_update();

CREATE TRIGGER approvals_reject_delete
    BEFORE DELETE ON cortex_coord.approvals
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_relay_authorization_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    approval record;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.authorized_by_principal);

    IF NEW.status <> 'authorized'
       OR NEW.consumed_at IS NOT NULL
       OR NEW.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'relay authorizations start authorized'
            USING ERRCODE = '23514';
    END IF;

    SELECT decision, gate_kind, relay_source_handoff_id, relay_target_scope_id,
           relay_target_actor_id, expires_at
      INTO approval
      FROM cortex_coord.approvals
     WHERE scope_id = NEW.scope_id AND approval_id = NEW.approval_id;
    IF NOT FOUND
       OR approval.decision <> 'approved'
       OR approval.gate_kind <> 'relay'
       OR approval.relay_source_handoff_id <> NEW.source_handoff_id
       OR approval.relay_target_scope_id <> NEW.target_scope_id
       OR approval.relay_target_actor_id <> NEW.target_actor_id
       OR (approval.expires_at IS NOT NULL AND approval.expires_at <= now()) THEN
        RAISE EXCEPTION 'relays require a live approval of these exact immutable targets'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER relay_authorizations_validate_insert
    BEFORE INSERT ON cortex_coord.relay_authorizations
    FOR EACH ROW
    EXECUTE FUNCTION cortex_coord.validate_relay_authorization_insert();

CREATE FUNCTION cortex_coord.validate_relay_authorization_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    approval_decision text;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.relay_id <> OLD.relay_id
       OR NEW.source_handoff_id <> OLD.source_handoff_id
       OR NEW.target_scope_id <> OLD.target_scope_id
       OR NEW.target_actor_id <> OLD.target_actor_id
       OR NEW.approval_id <> OLD.approval_id
       OR NEW.authorized_by_principal <> OLD.authorized_by_principal
       OR NEW.authorized_at <> OLD.authorized_at THEN
        RAISE EXCEPTION 'relay authorization identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status <> 'authorized' THEN
        RAISE EXCEPTION 'consumed and revoked relays are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = 'consumed' THEN
        IF OLD.consumed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL
           OR NEW.consumed_at IS NULL THEN
            RAISE EXCEPTION 'consumption sets exactly consumed_at'
                USING ERRCODE = '23514';
        END IF;
        -- Consume/revoke ordering shares this transaction: the approval must
        -- still be approved at consumption time.
        SELECT decision
          INTO approval_decision
          FROM cortex_coord.approvals
         WHERE scope_id = NEW.scope_id AND approval_id = NEW.approval_id;
        IF approval_decision IS DISTINCT FROM 'approved' THEN
            RAISE EXCEPTION 'relay consumption requires the still-approved gate'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.status = 'revoked' THEN
        IF OLD.revoked_at IS NOT NULL OR NEW.consumed_at IS NOT NULL
           OR NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION 'revocation sets exactly revoked_at'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'relay status transition % -> % is not allowed',
            OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER relay_authorizations_validate_update
    BEFORE UPDATE ON cortex_coord.relay_authorizations
    FOR EACH ROW
    EXECUTE FUNCTION cortex_coord.validate_relay_authorization_update();

CREATE TRIGGER relay_authorizations_reject_delete
    BEFORE DELETE ON cortex_coord.relay_authorizations
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_relay_receipt_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    relay_auth record;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.dispatched_by_principal);

    SELECT source_handoff_id, target_scope_id, target_actor_id, approval_id,
           status
      INTO relay_auth
      FROM cortex_coord.relay_authorizations
     WHERE scope_id = NEW.scope_id AND relay_id = NEW.relay_id;
    IF NOT FOUND OR relay_auth.status <> 'consumed' THEN
        RAISE EXCEPTION 'relay receipts record a consumed authorization'
            USING ERRCODE = '23514';
    END IF;
    IF relay_auth.source_handoff_id <> NEW.source_handoff_id
       OR relay_auth.target_scope_id <> NEW.target_scope_id
       OR relay_auth.target_actor_id <> NEW.target_actor_id
       OR relay_auth.approval_id <> NEW.approval_id THEN
        RAISE EXCEPTION 'relay receipts carry the authorized immutable identifiers'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER relay_receipts_validate_insert
    BEFORE INSERT ON cortex_coord.relay_receipts
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_relay_receipt_insert();

CREATE TRIGGER relay_receipts_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.relay_receipts
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_relay_materialization_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    receipt_target uuid;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(
        NEW.materialized_by_principal
    );

    IF NOT EXISTS (
        SELECT 1
          FROM cortex_coord.handoffs AS h
         WHERE h.scope_id = NEW.scope_id
           AND h.handoff_id = NEW.handoff_id
           AND h.relay_id = NEW.relay_id
           AND h.relay_source_scope_id = NEW.source_scope_id
           AND h.relay_source_handoff_id = NEW.source_handoff_id
    ) THEN
        RAISE EXCEPTION 'materializations link the relay-provenance handoff'
            USING ERRCODE = '23514';
    END IF;

    -- Reading the dispatch receipt is RLS-filtered: an unreadable source
    -- scope fails closed here.
    SELECT target_scope_id
      INTO receipt_target
      FROM cortex_coord.relay_receipts
     WHERE relay_id = NEW.relay_id AND scope_id = NEW.source_scope_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'materialization requires the readable dispatch receipt'
            USING ERRCODE = '23514';
    END IF;
    IF receipt_target <> NEW.scope_id THEN
        RAISE EXCEPTION 'the receipt was dispatched to a different scope'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER relay_materializations_validate_insert
    BEFORE INSERT ON cortex_coord.relay_materializations
    FOR EACH ROW
    EXECUTE FUNCTION cortex_coord.validate_relay_materialization_insert();

CREATE TRIGGER relay_materializations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.relay_materializations
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_wave_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);
    IF NEW.status <> 'pending' OR NEW.gate_approval_id IS NOT NULL THEN
        RAISE EXCEPTION 'waves start pending without a gate approval'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER waves_validate_insert
    BEFORE INSERT ON cortex_coord.waves
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_wave_insert();

CREATE FUNCTION cortex_coord.validate_wave_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    approval record;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.wave_id <> OLD.wave_id
       OR NEW.epic_id <> OLD.epic_id
       OR NEW.wave_index <> OLD.wave_index
       OR NEW.requires_human_gate <> OLD.requires_human_gate
       OR NEW.created_by_principal <> OLD.created_by_principal
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'wave identity and gate requirement are immutable'
            USING ERRCODE = '55000';
    END IF;

    IF OLD.status = 'pending' AND NEW.status = 'open' THEN
        IF EXISTS (
            SELECT 1 FROM cortex_coord.waves
             WHERE scope_id = NEW.scope_id
               AND epic_id = NEW.epic_id
               AND wave_index < NEW.wave_index
               AND status <> 'completed'
        ) THEN
            RAISE EXCEPTION 'earlier waves must complete before opening'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.requires_human_gate THEN
            IF NEW.gate_approval_id IS NULL THEN
                RAISE EXCEPTION 'human-gated waves open only with an approved gate'
                    USING ERRCODE = '23514';
            END IF;
            SELECT decision, gate_kind, subject_wave_id, expires_at
              INTO approval
              FROM cortex_coord.approvals
             WHERE scope_id = NEW.scope_id
               AND approval_id = NEW.gate_approval_id;
            IF NOT FOUND
               OR approval.decision <> 'approved'
               OR approval.gate_kind <> 'wave'
               OR approval.subject_wave_id <> NEW.wave_id
               OR (approval.expires_at IS NOT NULL
                   AND approval.expires_at <= now()) THEN
                RAISE EXCEPTION 'human-gated waves open only with a live approved gate'
                    USING ERRCODE = '23514';
            END IF;
        ELSIF NEW.gate_approval_id IS DISTINCT FROM OLD.gate_approval_id THEN
            RAISE EXCEPTION 'ungated waves carry no gate approval'
                USING ERRCODE = '23514';
        END IF;
    ELSIF OLD.status = 'open' AND NEW.status = 'completed' THEN
        IF EXISTS (
            SELECT 1 FROM cortex_coord.tasks
             WHERE scope_id = NEW.scope_id
               AND wave_id = NEW.wave_id
               AND status NOT IN ('completed', 'cancelled')
        ) THEN
            RAISE EXCEPTION 'waves complete only when every task is terminal'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.gate_approval_id IS DISTINCT FROM OLD.gate_approval_id THEN
            RAISE EXCEPTION 'gate approval is immutable after opening'
                USING ERRCODE = '55000';
        END IF;
    ELSE
        RAISE EXCEPTION 'wave transition % -> % is not allowed',
            OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER waves_validate_update
    BEFORE UPDATE ON cortex_coord.waves
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_wave_update();

CREATE TRIGGER waves_reject_delete
    BEFORE DELETE ON cortex_coord.waves
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_task_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
DECLARE
    wave_epic uuid;
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);
    IF NEW.status <> 'open' OR NEW.revision <> 1 THEN
        RAISE EXCEPTION 'tasks start open at revision 1' USING ERRCODE = '23514';
    END IF;
    IF NEW.wave_id IS NOT NULL THEN
        SELECT epic_id
          INTO wave_epic
          FROM cortex_coord.waves
         WHERE scope_id = NEW.scope_id AND wave_id = NEW.wave_id;
        IF NOT FOUND OR wave_epic IS DISTINCT FROM NEW.epic_id THEN
            RAISE EXCEPTION 'a task in a wave belongs to that wave''s epic'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER tasks_validate_insert
    BEFORE INSERT ON cortex_coord.tasks
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_task_insert();

CREATE FUNCTION cortex_coord.validate_task_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);

    IF NEW.scope_id <> OLD.scope_id
       OR NEW.task_id <> OLD.task_id
       OR NEW.epic_id IS DISTINCT FROM OLD.epic_id
       OR NEW.wave_id IS DISTINCT FROM OLD.wave_id
       OR NEW.title <> OLD.title
       OR NEW.brief IS DISTINCT FROM OLD.brief
       OR NEW.created_by_principal <> OLD.created_by_principal
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'task identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'task revision must advance by exactly one'
            USING ERRCODE = '23514';
    END IF;
    IF NOT (
        (OLD.status = 'open'
         AND NEW.status IN ('dispatched', 'completed', 'cancelled'))
        OR (OLD.status = 'dispatched'
            AND NEW.status IN ('completed', 'cancelled'))
    ) THEN
        RAISE EXCEPTION 'task transition % -> % is not allowed',
            OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER tasks_validate_update
    BEFORE UPDATE ON cortex_coord.tasks
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_task_update();

CREATE TRIGGER tasks_reject_delete
    BEFORE DELETE ON cortex_coord.tasks
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_task_dependency_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    IF NEW.task_id = NEW.depends_on_task_id THEN
        RAISE EXCEPTION 'tasks cannot depend on themselves' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        WITH RECURSIVE reach AS (
            SELECT d.depends_on_task_id AS reached_id
              FROM cortex_coord.task_dependencies AS d
             WHERE d.scope_id = NEW.scope_id
               AND d.task_id = NEW.depends_on_task_id
            UNION
            SELECT d.depends_on_task_id
              FROM cortex_coord.task_dependencies AS d
              JOIN reach ON reach.reached_id = d.task_id
             WHERE d.scope_id = NEW.scope_id
        )
        SELECT 1 FROM reach WHERE reached_id = NEW.task_id
    ) THEN
        RAISE EXCEPTION 'task dependencies must stay acyclic'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER task_dependencies_validate_insert
    BEFORE INSERT ON cortex_coord.task_dependencies
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_task_dependency_insert();

CREATE TRIGGER task_dependencies_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.task_dependencies
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_task_assignment_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.assigned_by_principal);
    IF NEW.retired_at IS NOT NULL THEN
        RAISE EXCEPTION 'assignments start active' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER task_assignments_validate_insert
    BEFORE INSERT ON cortex_coord.task_assignments
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_task_assignment_insert();

CREATE FUNCTION cortex_coord.validate_task_assignment_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    IF NEW.scope_id <> OLD.scope_id
       OR NEW.task_id <> OLD.task_id
       OR NEW.actor_id <> OLD.actor_id
       OR NEW.assignment_role <> OLD.assignment_role
       OR NEW.assigned_by_principal <> OLD.assigned_by_principal
       OR NEW.assigned_at <> OLD.assigned_at THEN
        RAISE EXCEPTION 'assignment identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.retired_at IS NOT NULL
       OR NEW.retired_at IS NULL
       OR NEW.retired_at < OLD.assigned_at THEN
        RAISE EXCEPTION 'assignments retire exactly once, after assignment'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER task_assignments_validate_update
    BEFORE UPDATE ON cortex_coord.task_assignments
    FOR EACH ROW
    EXECUTE FUNCTION cortex_coord.validate_task_assignment_update();

CREATE TRIGGER task_assignments_reject_delete
    BEFORE DELETE ON cortex_coord.task_assignments
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_audit_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.actor_principal_id);
    SELECT COALESCE(max(audit_seq), 0) + 1
      INTO NEW.audit_seq
      FROM cortex_coord.audit_log
     WHERE scope_id = NEW.scope_id
       AND aggregate_kind = NEW.aggregate_kind
       AND aggregate_id = NEW.aggregate_id;
    RETURN NEW;
END
$function$;

CREATE TRIGGER audit_log_validate_insert
    BEFORE INSERT ON cortex_coord.audit_log
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_audit_insert();

CREATE TRIGGER audit_log_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.audit_log
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_outbox_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    IF NEW.delivered_at IS NOT NULL THEN
        RAISE EXCEPTION 'outbox events start undelivered' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER outbox_events_validate_insert
    BEFORE INSERT ON cortex_coord.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_outbox_insert();

CREATE FUNCTION cortex_coord.validate_outbox_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    IF OLD.delivered_at IS NOT NULL OR NEW.delivered_at IS NULL THEN
        RAISE EXCEPTION 'outbox delivery is marked exactly once'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.event_id <> OLD.event_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.aggregate_kind <> OLD.aggregate_kind
       OR NEW.aggregate_id <> OLD.aggregate_id
       OR NEW.event_type <> OLD.event_type
       OR NEW.payload <> OLD.payload
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'outbox events are immutable apart from delivery'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER outbox_events_validate_update
    BEFORE UPDATE ON cortex_coord.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_outbox_update();

CREATE TRIGGER outbox_events_reject_delete
    BEFORE DELETE ON cortex_coord.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_epic_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);
    RETURN NEW;
END
$function$;

CREATE TRIGGER epics_validate_insert
    BEFORE INSERT ON cortex_coord.epics
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_epic_insert();

CREATE TRIGGER epics_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.epics
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_board_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.created_by_principal);
    RETURN NEW;
END
$function$;

CREATE TRIGGER boards_validate_insert
    BEFORE INSERT ON cortex_coord.boards
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_board_insert();

CREATE TRIGGER boards_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.boards
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

CREATE FUNCTION cortex_coord.validate_board_task_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_coord, pg_temp
AS $function$
BEGIN
    PERFORM cortex_coord.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_coord.require_current_principal(NEW.added_by_principal);
    RETURN NEW;
END
$function$;

CREATE TRIGGER board_tasks_validate_insert
    BEFORE INSERT ON cortex_coord.board_tasks
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.validate_board_task_insert();

CREATE TRIGGER board_tasks_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_coord.board_tasks
    FOR EACH ROW EXECUTE FUNCTION cortex_coord.reject_mutation();

-- ---------------------------------------------------------------------------
-- Row-level security: forced for every role including the runtime credential
-- owner. Policies reuse the 0001 scope helpers; writes bind the row scope to
-- the authorized write scope and the actor column to the authenticated
-- principal. UPDATE policies constrain the old row to the write scope.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_coord.epics ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.epics FORCE ROW LEVEL SECURITY;
CREATE POLICY epics_scope_read ON cortex_coord.epics
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY epics_scope_insert ON cortex_coord.epics
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY epics_migrator_all ON cortex_coord.epics
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.waves ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.waves FORCE ROW LEVEL SECURITY;
CREATE POLICY waves_scope_read ON cortex_coord.waves
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY waves_scope_insert ON cortex_coord.waves
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY waves_scope_update ON cortex_coord.waves
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY waves_migrator_all ON cortex_coord.waves
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tasks_scope_read ON cortex_coord.tasks
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY tasks_scope_insert ON cortex_coord.tasks
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY tasks_scope_update ON cortex_coord.tasks
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY tasks_migrator_all ON cortex_coord.tasks
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.task_dependencies ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.task_dependencies FORCE ROW LEVEL SECURITY;
CREATE POLICY task_dependencies_scope_read ON cortex_coord.task_dependencies
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY task_dependencies_scope_insert ON cortex_coord.task_dependencies
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY task_dependencies_migrator_all ON cortex_coord.task_dependencies
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.task_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.task_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY task_assignments_scope_read ON cortex_coord.task_assignments
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY task_assignments_scope_insert ON cortex_coord.task_assignments
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND assigned_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY task_assignments_scope_update ON cortex_coord.task_assignments
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY task_assignments_migrator_all ON cortex_coord.task_assignments
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.boards ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.boards FORCE ROW LEVEL SECURITY;
CREATE POLICY boards_scope_read ON cortex_coord.boards
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY boards_scope_insert ON cortex_coord.boards
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY boards_migrator_all ON cortex_coord.boards
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.board_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.board_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY board_tasks_scope_read ON cortex_coord.board_tasks
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY board_tasks_scope_insert ON cortex_coord.board_tasks
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND added_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY board_tasks_migrator_all ON cortex_coord.board_tasks
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.handoffs FORCE ROW LEVEL SECURITY;
CREATE POLICY handoffs_scope_read ON cortex_coord.handoffs
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY handoffs_scope_insert ON cortex_coord.handoffs
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY handoffs_scope_update ON cortex_coord.handoffs
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY handoffs_migrator_all ON cortex_coord.handoffs
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.claims FORCE ROW LEVEL SECURITY;
CREATE POLICY claims_scope_read ON cortex_coord.claims
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY claims_scope_insert ON cortex_coord.claims
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND claimant_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY claims_scope_update ON cortex_coord.claims
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY claims_migrator_all ON cortex_coord.claims
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.returns ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.returns FORCE ROW LEVEL SECURITY;
CREATE POLICY returns_scope_read ON cortex_coord.returns
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY returns_scope_insert ON cortex_coord.returns
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND returned_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY returns_migrator_all ON cortex_coord.returns
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.reviews FORCE ROW LEVEL SECURITY;
CREATE POLICY reviews_scope_read ON cortex_coord.reviews
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY reviews_scope_insert ON cortex_coord.reviews
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND reviewer_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY reviews_migrator_all ON cortex_coord.reviews
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.work_product_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.work_product_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY work_product_receipts_scope_read
    ON cortex_coord.work_product_receipts
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY work_product_receipts_scope_insert
    ON cortex_coord.work_product_receipts
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND created_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY work_product_receipts_migrator_all
    ON cortex_coord.work_product_receipts
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY approvals_scope_read ON cortex_coord.approvals
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY approvals_scope_insert ON cortex_coord.approvals
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND requested_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY approvals_scope_update ON cortex_coord.approvals
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY approvals_migrator_all ON cortex_coord.approvals
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.relay_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.relay_authorizations FORCE ROW LEVEL SECURITY;
CREATE POLICY relay_authorizations_scope_read ON cortex_coord.relay_authorizations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY relay_authorizations_scope_insert ON cortex_coord.relay_authorizations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND authorized_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY relay_authorizations_scope_update ON cortex_coord.relay_authorizations
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY relay_authorizations_migrator_all ON cortex_coord.relay_authorizations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.relay_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.relay_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY relay_receipts_scope_read ON cortex_coord.relay_receipts
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY relay_receipts_scope_insert ON cortex_coord.relay_receipts
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND dispatched_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY relay_receipts_migrator_all ON cortex_coord.relay_receipts
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.relay_materializations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.relay_materializations FORCE ROW LEVEL SECURITY;
CREATE POLICY relay_materializations_scope_read
    ON cortex_coord.relay_materializations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY relay_materializations_scope_insert
    ON cortex_coord.relay_materializations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND materialized_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY relay_materializations_migrator_all
    ON cortex_coord.relay_materializations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_log_scope_read ON cortex_coord.audit_log
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY audit_log_scope_insert ON cortex_coord.audit_log
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND actor_principal_id = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY audit_log_migrator_all ON cortex_coord.audit_log
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_coord.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_coord.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY coordination_outbox_scope_read ON cortex_coord.outbox_events
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY coordination_outbox_scope_insert ON cortex_coord.outbox_events
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY coordination_outbox_scope_update ON cortex_coord.outbox_events
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY coordination_outbox_migrator_all ON cortex_coord.outbox_events
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Grants: least DML required. No deletes are granted anywhere; append-only
-- tables receive SELECT+INSERT only. No default privileges.
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA cortex_coord TO cortex_v2_app;

GRANT SELECT, INSERT ON cortex_coord.epics TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.waves TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.tasks TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.task_dependencies TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.task_assignments TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.boards TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.board_tasks TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.handoffs TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.claims TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.returns TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.reviews TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.work_product_receipts TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.approvals TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.relay_authorizations
    TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.relay_receipts TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.relay_materializations TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_coord.audit_log TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_coord.outbox_events TO cortex_v2_app;

-- Trigger functions execute as the calling role; the policy/principal/human
-- helpers they invoke need EXECUTE for the app role, mirroring the 0002
-- grants on cortex_core.content_payload_violation and friends.
GRANT EXECUTE ON FUNCTION cortex_coord.require_declared_policy_revision(uuid)
    TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_coord.require_current_principal(uuid)
    TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_coord.require_human_lead(uuid, uuid)
    TO cortex_v2_app;

REVOKE ALL ON FUNCTION
    cortex_coord.require_declared_policy_revision(uuid),
    cortex_coord.require_current_principal(uuid),
    cortex_coord.require_human_lead(uuid, uuid),
    cortex_coord.reject_mutation(),
    cortex_coord.validate_handoff_insert(),
    cortex_coord.validate_handoff_update(),
    cortex_coord.validate_claim_insert(),
    cortex_coord.validate_claim_update(),
    cortex_coord.validate_return_insert(),
    cortex_coord.validate_receipt_insert(),
    cortex_coord.validate_review_insert(),
    cortex_coord.validate_approval_insert(),
    cortex_coord.validate_approval_update(),
    cortex_coord.validate_relay_authorization_insert(),
    cortex_coord.validate_relay_authorization_update(),
    cortex_coord.validate_relay_receipt_insert(),
    cortex_coord.validate_relay_materialization_insert(),
    cortex_coord.validate_wave_insert(),
    cortex_coord.validate_wave_update(),
    cortex_coord.validate_task_insert(),
    cortex_coord.validate_task_update(),
    cortex_coord.validate_task_dependency_insert(),
    cortex_coord.validate_task_assignment_insert(),
    cortex_coord.validate_task_assignment_update(),
    cortex_coord.validate_audit_insert(),
    cortex_coord.validate_outbox_insert(),
    cortex_coord.validate_outbox_update(),
    cortex_coord.validate_epic_insert(),
    cortex_coord.validate_board_insert(),
    cortex_coord.validate_board_task_insert()
    FROM PUBLIC;
