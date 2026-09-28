DO $bootstrap$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'feed/verification migration must run as cortex_v2_migrator';
    END IF;
END
$bootstrap$;

CREATE SCHEMA IF NOT EXISTS cortex_feed AUTHORIZATION cortex_v2_migrator;
CREATE SCHEMA IF NOT EXISTS cortex_verification AUTHORIZATION cortex_v2_migrator;

-- ---------------------------------------------------------------------------
-- W4-phase feed plane (R20): scoped durable feed entries, publication
-- checkpoints carrying the cursor generation, retention pruning and honest
-- resync. Depends only on migrations 0001-0003: it consumes the committed
-- outbox tables those migrations define and marks their delivery state.
-- Nothing here assumes anything about migrations 0004-0006.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_feed.require_declared_policy_revision(p_scope_id uuid)
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

CREATE TABLE cortex_feed.publication_checkpoints (
    scope_id uuid PRIMARY KEY REFERENCES cortex_core.scopes(scope_id),
    feed_generation integer NOT NULL DEFAULT 1 CHECK (feed_generation > 0),
    last_published_seq bigint NOT NULL DEFAULT 0
        CHECK (last_published_seq >= 0),
    oldest_available_seq bigint NOT NULL DEFAULT 0
        CHECK (oldest_available_seq >= 0),
    last_published_at timestamptz,
    total_published bigint NOT NULL DEFAULT 0 CHECK (total_published >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (oldest_available_seq <= last_published_seq + 1)
);

CREATE TABLE cortex_feed.feed_entries (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    feed_generation integer NOT NULL CHECK (feed_generation > 0),
    feed_seq bigint NOT NULL CHECK (feed_seq > 0),
    source_event_id uuid NOT NULL,
    source_table text NOT NULL,
    aggregate_kind text NOT NULL CHECK (length(aggregate_kind) BETWEEN 1 AND 64),
    aggregate_id uuid NOT NULL,
    aggregate_version bigint,
    event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    payload jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, feed_generation, feed_seq),
    UNIQUE (scope_id, source_event_id)
);

CREATE INDEX feed_entries_prune
    ON cortex_feed.feed_entries(scope_id, feed_generation, published_at);

CREATE FUNCTION cortex_feed.validate_entry_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_feed, pg_temp
AS $function$
BEGIN
    PERFORM cortex_feed.require_declared_policy_revision(NEW.scope_id);
    RETURN NEW;
END
$function$;

CREATE TRIGGER feed_entries_validate_insert
    BEFORE INSERT ON cortex_feed.feed_entries
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_entry_insert();

CREATE FUNCTION cortex_feed.reject_entry_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'feed entries are immutable; retention prunes whole rows'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER feed_entries_reject_update
    BEFORE UPDATE ON cortex_feed.feed_entries
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.reject_entry_mutation();

CREATE FUNCTION cortex_feed.validate_checkpoint_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_feed, pg_temp
AS $function$
BEGIN
    PERFORM cortex_feed.require_declared_policy_revision(NEW.scope_id);
    IF NEW.feed_generation <> 1 OR NEW.last_published_seq <> 0
       OR NEW.oldest_available_seq <> 0 OR NEW.total_published <> 0 THEN
        RAISE EXCEPTION 'checkpoints start at generation 1 with an empty feed'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER publication_checkpoints_validate_insert
    BEFORE INSERT ON cortex_feed.publication_checkpoints
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_checkpoint_insert();

CREATE FUNCTION cortex_feed.validate_checkpoint_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_feed, pg_temp
AS $function$
BEGIN
    PERFORM cortex_feed.require_declared_policy_revision(NEW.scope_id);
    IF NEW.scope_id <> OLD.scope_id THEN
        RAISE EXCEPTION 'checkpoint scope is immutable' USING ERRCODE = '55000';
    END IF;

    IF NEW.feed_generation = OLD.feed_generation THEN
        -- Publication and pruning never move the cursor backwards.
        IF NEW.last_published_seq < OLD.last_published_seq
           OR NEW.oldest_available_seq < OLD.oldest_available_seq THEN
            RAISE EXCEPTION 'feed cursors are monotonic within a generation'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.feed_generation = OLD.feed_generation + 1 THEN
        -- Explicit generation advance resets the feed exactly once.
        IF NEW.last_published_seq <> 0
           OR NEW.oldest_available_seq <> 0
           OR NEW.last_published_at IS NOT NULL THEN
            RAISE EXCEPTION 'a new feed generation starts empty'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'feed generation must advance by exactly one'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER publication_checkpoints_validate_update
    BEFORE UPDATE ON cortex_feed.publication_checkpoints
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_checkpoint_update();

CREATE FUNCTION cortex_feed.reject_checkpoint_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'publication checkpoints are permanent per scope'
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER publication_checkpoints_reject_delete
    BEFORE DELETE ON cortex_feed.publication_checkpoints
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.reject_checkpoint_delete();

-- ---------------------------------------------------------------------------
-- Delivery marking on the 0001/0002 outbox tables. This is additive only:
-- the frozen W1 relations keep their names, columns and existing policies;
-- the dispatcher needs a delivery-only UPDATE path with the same scope
-- discipline as the coordination outbox established in 0003.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_feed.validate_outbox_delivery_update()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.delivered_at IS NOT NULL OR NEW.delivered_at IS NULL THEN
        RAISE EXCEPTION 'outbox delivery is marked exactly once'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.event_id <> OLD.event_id
       OR NEW.scope_id <> OLD.scope_id
       OR NEW.event_type <> OLD.event_type
       OR NEW.payload <> OLD.payload
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'outbox events are immutable apart from delivery'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER core_outbox_delivery_update
    BEFORE UPDATE ON cortex_core.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_outbox_delivery_update();

CREATE TRIGGER content_outbox_delivery_update
    BEFORE UPDATE ON cortex_core.content_outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_outbox_delivery_update();

CREATE POLICY outbox_events_scope_update ON cortex_core.outbox_events
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));

CREATE POLICY content_outbox_scope_update ON cortex_core.content_outbox_events
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));

-- ---------------------------------------------------------------------------
-- Verification plane (R19): append-only verification records with typed
-- subjects, exact source/revision citations, readback evidence, producer
-- independence and an outbox for feed publication.
-- ---------------------------------------------------------------------------

CREATE FUNCTION cortex_verification.require_declared_policy_revision(
    p_scope_id uuid
)
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

CREATE FUNCTION cortex_verification.require_current_principal(
    p_principal_id uuid
)
RETURNS void
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_principal_id IS DISTINCT FROM NULLIF(
        current_setting('cortex.principal_id', true), ''
    )::uuid THEN
        RAISE EXCEPTION 'verification writes are bound to the authenticated principal'
            USING ERRCODE = '42501';
    END IF;
END
$function$;

CREATE TABLE cortex_verification.verifications (
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    verification_id uuid NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN (
        'work_product_receipt', 'handoff_return', 'claim'
    )),
    subject_receipt_id uuid,
    subject_handoff_id uuid,
    subject_claim text
        CHECK (subject_claim IS NULL
               OR length(subject_claim) BETWEEN 1 AND 65536),
    verdict text NOT NULL CHECK (verdict IN (
        'verified', 'unverifiable', 'contradicted'
    )),
    method text NOT NULL CHECK (method IN (
        'content_readback', 'test_execution', 'command_output',
        'independent_recomputation', 'external_source', 'manual_review'
    )),
    unverifiable_reason text
        CHECK (unverifiable_reason IS NULL
               OR length(unverifiable_reason) BETWEEN 1 AND 512),
    readback_evidence jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(readback_evidence) = 'array'),
    tool_name text CHECK (tool_name IS NULL OR length(tool_name) BETWEEN 1 AND 64),
    tool_version text
        CHECK (tool_version IS NULL OR length(tool_version) BETWEEN 1 AND 64),
    verified_by_principal uuid NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (scope_id, verification_id),
    FOREIGN KEY (scope_id, subject_receipt_id)
        REFERENCES cortex_coord.work_product_receipts(scope_id, receipt_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (scope_id, subject_handoff_id)
        REFERENCES cortex_coord.handoffs(scope_id, handoff_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (verified_by_principal, scope_id)
        REFERENCES cortex_auth.scope_grants(principal_id, scope_id)
        ON DELETE RESTRICT,
    CHECK ((subject_kind = 'work_product_receipt')
           = (subject_receipt_id IS NOT NULL)),
    CHECK ((subject_kind = 'handoff_return')
           = (subject_handoff_id IS NOT NULL)),
    CHECK ((subject_kind = 'claim') = (subject_claim IS NOT NULL)),
    CHECK ((verdict = 'unverifiable')
           = (unverifiable_reason IS NOT NULL)),
    CHECK (verdict <> 'verified'
           OR jsonb_array_length(readback_evidence) > 0)
);

CREATE INDEX verifications_subject_receipt
    ON cortex_verification.verifications(scope_id, subject_receipt_id)
    WHERE subject_receipt_id IS NOT NULL;
CREATE INDEX verifications_subject_handoff
    ON cortex_verification.verifications(scope_id, subject_handoff_id)
    WHERE subject_handoff_id IS NOT NULL;
CREATE INDEX verifications_scope_time
    ON cortex_verification.verifications(scope_id, verified_at DESC);

CREATE TABLE cortex_verification.verification_citations (
    scope_id uuid NOT NULL,
    verification_id uuid NOT NULL,
    citation_seq integer NOT NULL CHECK (citation_seq > 0),
    cited_scope_id uuid NOT NULL,
    cited_content_id uuid NOT NULL,
    cited_revision integer NOT NULL CHECK (cited_revision > 0),
    note text CHECK (note IS NULL OR length(note) BETWEEN 1 AND 512),
    PRIMARY KEY (scope_id, verification_id, citation_seq),
    FOREIGN KEY (scope_id, verification_id)
        REFERENCES cortex_verification.verifications(scope_id, verification_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (cited_scope_id, cited_content_id)
        REFERENCES cortex_core.content_items(scope_id, content_id)
        ON DELETE RESTRICT
);

CREATE TABLE cortex_verification.outbox_events (
    event_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    aggregate_kind text NOT NULL CHECK (aggregate_kind = 'verification'),
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

CREATE INDEX verification_outbox_undelivered
    ON cortex_verification.outbox_events(scope_id, created_at)
    WHERE delivered_at IS NULL;

CREATE FUNCTION cortex_verification.validate_verification_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_verification, cortex_coord, pg_temp
AS $function$
DECLARE
    producer uuid;
BEGIN
    PERFORM cortex_verification.require_declared_policy_revision(NEW.scope_id);
    PERFORM cortex_verification.require_current_principal(
        NEW.verified_by_principal
    );
    IF NEW.subject_kind = 'work_product_receipt' THEN
        SELECT created_by_principal
          INTO producer
          FROM cortex_coord.work_product_receipts
         WHERE scope_id = NEW.scope_id
           AND receipt_id = NEW.subject_receipt_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'verification subject receipt must exist'
                USING ERRCODE = '23503';
        END IF;
        IF producer = NEW.verified_by_principal THEN
            RAISE EXCEPTION 'the producer of a work product cannot independently verify it'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER verifications_validate_insert
    BEFORE INSERT ON cortex_verification.verifications
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.validate_verification_insert();

CREATE FUNCTION cortex_verification.require_evidence_citations()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_verification, pg_temp
AS $function$
BEGIN
    IF NEW.verdict IN ('verified', 'contradicted')
       AND NOT EXISTS (
           SELECT 1
             FROM cortex_verification.verification_citations AS c
            WHERE c.scope_id = NEW.scope_id
              AND c.verification_id = NEW.verification_id
       ) THEN
        RAISE EXCEPTION 'evidence-backed verdicts require source citations'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END
$function$;

-- Deferred to commit time: citations are child rows of the verification.
CREATE CONSTRAINT TRIGGER verifications_require_citations
    AFTER INSERT ON cortex_verification.verifications
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.require_evidence_citations();

CREATE FUNCTION cortex_verification.reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME
        USING ERRCODE = '55000';
    RETURN NULL;
END
$function$;

CREATE TRIGGER verifications_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_verification.verifications
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.reject_mutation();

CREATE FUNCTION cortex_verification.validate_citation_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_verification, cortex_core, pg_temp
AS $function$
BEGIN
    PERFORM cortex_verification.require_declared_policy_revision(NEW.scope_id);
    IF NOT EXISTS (
        SELECT 1
          FROM cortex_core.content_revisions AS r
         WHERE r.scope_id = NEW.cited_scope_id
           AND r.content_id = NEW.cited_content_id
           AND r.revision = NEW.cited_revision
    ) THEN
        RAISE EXCEPTION 'citations name an exact readable source revision'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER verification_citations_validate_insert
    BEFORE INSERT ON cortex_verification.verification_citations
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.validate_citation_insert();

CREATE TRIGGER verification_citations_reject_mutation
    BEFORE UPDATE OR DELETE ON cortex_verification.verification_citations
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.reject_mutation();

CREATE FUNCTION cortex_verification.validate_outbox_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, cortex_verification, pg_temp
AS $function$
BEGIN
    PERFORM cortex_verification.require_declared_policy_revision(NEW.scope_id);
    IF NEW.delivered_at IS NOT NULL THEN
        RAISE EXCEPTION 'outbox events start undelivered' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER verification_outbox_validate_insert
    BEFORE INSERT ON cortex_verification.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.validate_outbox_insert();

CREATE TRIGGER verification_outbox_delivery_update
    BEFORE UPDATE ON cortex_verification.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_feed.validate_outbox_delivery_update();

CREATE TRIGGER verification_outbox_reject_delete
    BEFORE DELETE ON cortex_verification.outbox_events
    FOR EACH ROW EXECUTE FUNCTION cortex_verification.reject_mutation();

-- ---------------------------------------------------------------------------
-- Row-level security: forced for every role. Reads follow the granted read
-- scopes; writes bind the row scope to the authorized write scope, and
-- verification inserts bind the verifier to the authenticated principal.
-- ---------------------------------------------------------------------------

ALTER TABLE cortex_feed.publication_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_feed.publication_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY publication_checkpoints_scope_read
    ON cortex_feed.publication_checkpoints
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY publication_checkpoints_scope_insert
    ON cortex_feed.publication_checkpoints
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY publication_checkpoints_scope_update
    ON cortex_feed.publication_checkpoints
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY publication_checkpoints_migrator_all
    ON cortex_feed.publication_checkpoints
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_feed.feed_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_feed.feed_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY feed_entries_scope_read ON cortex_feed.feed_entries
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY feed_entries_scope_insert ON cortex_feed.feed_entries
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY feed_entries_scope_delete ON cortex_feed.feed_entries
    FOR DELETE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id));
CREATE POLICY feed_entries_migrator_all ON cortex_feed.feed_entries
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_verification.verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_verification.verifications FORCE ROW LEVEL SECURITY;
CREATE POLICY verifications_scope_read ON cortex_verification.verifications
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY verifications_scope_insert ON cortex_verification.verifications
    FOR INSERT TO cortex_v2_app
    WITH CHECK (
        cortex_core.scope_write_policy(scope_id)
        AND verified_by_principal = NULLIF(
            current_setting('cortex.principal_id', true), ''
        )::uuid
    );
CREATE POLICY verifications_migrator_all ON cortex_verification.verifications
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_verification.verification_citations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_verification.verification_citations FORCE ROW LEVEL SECURITY;
CREATE POLICY verification_citations_scope_read
    ON cortex_verification.verification_citations
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY verification_citations_scope_insert
    ON cortex_verification.verification_citations
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY verification_citations_migrator_all
    ON cortex_verification.verification_citations
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

ALTER TABLE cortex_verification.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cortex_verification.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY verification_outbox_scope_read
    ON cortex_verification.outbox_events
    FOR SELECT TO cortex_v2_app
    USING (cortex_core.scope_read_policy(scope_id));
CREATE POLICY verification_outbox_scope_insert
    ON cortex_verification.outbox_events
    FOR INSERT TO cortex_v2_app
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY verification_outbox_scope_update
    ON cortex_verification.outbox_events
    FOR UPDATE TO cortex_v2_app
    USING (cortex_core.scope_write_policy(scope_id))
    WITH CHECK (cortex_core.scope_write_policy(scope_id));
CREATE POLICY verification_outbox_migrator_all
    ON cortex_verification.outbox_events
    FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Grants: least DML required. Feed entries are never updated by the app;
-- pruning deletes whole rows. Verification records are append-only.
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA cortex_feed, cortex_verification TO cortex_v2_app;

GRANT SELECT, INSERT, DELETE ON cortex_feed.feed_entries TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_feed.publication_checkpoints
    TO cortex_v2_app;

GRANT SELECT, INSERT ON cortex_verification.verifications TO cortex_v2_app;
GRANT SELECT, INSERT ON cortex_verification.verification_citations
    TO cortex_v2_app;
GRANT SELECT, INSERT, UPDATE ON cortex_verification.outbox_events
    TO cortex_v2_app;

GRANT UPDATE ON cortex_core.outbox_events TO cortex_v2_app;
GRANT UPDATE ON cortex_core.content_outbox_events TO cortex_v2_app;

-- Trigger functions execute as the calling role; the policy/principal
-- helpers they invoke need EXECUTE for the app role (0002 precedent).
GRANT EXECUTE ON FUNCTION cortex_feed.require_declared_policy_revision(uuid)
    TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION
    cortex_verification.require_declared_policy_revision(uuid)
    TO cortex_v2_app;
GRANT EXECUTE ON FUNCTION cortex_verification.require_current_principal(uuid)
    TO cortex_v2_app;

REVOKE ALL ON FUNCTION
    cortex_feed.require_declared_policy_revision(uuid),
    cortex_feed.validate_entry_insert(),
    cortex_feed.reject_entry_mutation(),
    cortex_feed.validate_checkpoint_insert(),
    cortex_feed.validate_checkpoint_update(),
    cortex_feed.reject_checkpoint_delete(),
    cortex_feed.validate_outbox_delivery_update(),
    cortex_verification.require_declared_policy_revision(uuid),
    cortex_verification.require_current_principal(uuid),
    cortex_verification.validate_verification_insert(),
    cortex_verification.require_evidence_citations(),
    cortex_verification.reject_mutation(),
    cortex_verification.validate_citation_insert(),
    cortex_verification.validate_outbox_insert()
    FROM PUBLIC;
