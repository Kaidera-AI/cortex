-- Boot storage foundation: descriptive metadata only; no authority/grant changes.
DO $guard$
BEGIN
    IF current_user <> 'cortex_v2_migrator' THEN
        RAISE EXCEPTION 'boot context migration requires cortex_v2_migrator';
    END IF;
END
$guard$;

CREATE FUNCTION cortex_context.valid_boot_manifest(p_kind text, p_manifest jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    required_keys text[] := ARRAY['schema_version','name','description','scope','permission','body_ref','version'];
    allowed_keys text[];
    field text;
    item jsonb;
    body_ref text;
BEGIN
    IF p_manifest IS NULL THEN RETURN true; END IF;
    IF p_kind NOT IN ('persona','rule','skill') OR jsonb_typeof(p_manifest) <> 'object' THEN RETURN false; END IF;
    IF p_kind = 'persona' THEN
        required_keys := required_keys || ARRAY['functional_roles','identity_text'];
    ELSIF p_kind = 'rule' THEN
        required_keys := required_keys || ARRAY['title','source_file'];
    END IF;
    allowed_keys := required_keys;
    IF p_kind = 'persona' THEN allowed_keys := allowed_keys || ARRAY['lane','not_lane','reports_to']; END IF;
    IF NOT p_manifest ?& required_keys OR EXISTS (
        SELECT 1 FROM jsonb_object_keys(p_manifest) AS keys(key)
        WHERE NOT key = ANY(allowed_keys)
    ) THEN RETURN false; END IF;
    IF p_manifest->>'schema_version' IS DISTINCT FROM 'cortex.boot-' || p_kind || '-manifest.v1'
       OR jsonb_typeof(p_manifest->'schema_version') <> 'string'
       OR jsonb_typeof(p_manifest->'scope') <> 'string'
       OR p_manifest->>'scope' NOT IN ('project','global') THEN RETURN false; END IF;
    FOREACH field IN ARRAY ARRAY['name','description','permission','body_ref','version','title','source_file','lane','not_lane','reports_to'] LOOP
        IF p_manifest ? field AND jsonb_typeof(p_manifest->field) NOT IN ('string','null') THEN RETURN false; END IF;
    END LOOP;
    IF jsonb_typeof(p_manifest->'body_ref') = 'string' THEN
        body_ref := p_manifest->>'body_ref';
        IF body_ref = '' OR left(body_ref,1) = '/' OR right(body_ref,1) = '/'
           OR strpos(body_ref,'//') > 0 OR strpos(body_ref,chr(92)) > 0
           OR body_ref ~ '(^|/)[.]{1,2}(/|$)' OR body_ref ~ '[[:cntrl:]]' THEN RETURN false; END IF;
    END IF;
    IF p_kind = 'persona' THEN
        IF jsonb_typeof(p_manifest->'identity_text') <> 'string'
           OR length(p_manifest->>'identity_text') NOT BETWEEN 1 AND 65536
           OR jsonb_typeof(p_manifest->'functional_roles') <> 'array' THEN RETURN false; END IF;
        IF jsonb_array_length(p_manifest->'functional_roles') NOT BETWEEN 1 AND 64 THEN RETURN false; END IF;
        FOR item IN SELECT value FROM jsonb_array_elements(p_manifest->'functional_roles') LOOP
            IF jsonb_typeof(item) <> 'string' OR length(item #>> '{}') NOT BETWEEN 1 AND 256 THEN RETURN false; END IF;
        END LOOP;
        IF (SELECT count(DISTINCT value) FROM jsonb_array_elements(p_manifest->'functional_roles'))
           <> jsonb_array_length(p_manifest->'functional_roles') THEN RETURN false; END IF;
    END IF;
    RETURN true;
END
$function$;

ALTER TABLE cortex_context.persona_revisions
    ADD COLUMN boot_manifest jsonb,
    ADD COLUMN audience text NOT NULL DEFAULT 'scope' CHECK (audience IN ('scope','agent_boot')),
    ADD CONSTRAINT persona_boot_manifest_valid CHECK (cortex_context.valid_boot_manifest('persona',boot_manifest));
ALTER TABLE cortex_context.rule_revisions
    ADD COLUMN boot_manifest jsonb,
    ADD COLUMN audience text NOT NULL DEFAULT 'scope' CHECK (audience IN ('scope','agent_boot')),
    ADD CONSTRAINT rule_boot_manifest_valid CHECK (cortex_context.valid_boot_manifest('rule',boot_manifest));
ALTER TABLE cortex_context.skill_revisions
    ADD COLUMN boot_manifest jsonb,
    ADD CONSTRAINT skill_boot_manifest_valid CHECK (cortex_context.valid_boot_manifest('skill',boot_manifest));

REVOKE ALL ON FUNCTION cortex_context.valid_boot_manifest(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_context.valid_boot_manifest(text,jsonb) TO cortex_v2_app;

-- Logical append streams. Canonical bodies remain in their existing revisions.
CREATE FUNCTION cortex_context.valid_boot_roles(p_roles text[])
RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(array_ndims(p_roles) = 1 AND cardinality(p_roles) BETWEEN 1 AND 64
        AND NOT EXISTS (SELECT 1 FROM unnest(p_roles) AS role(value)
                         WHERE value IS NULL OR length(value) NOT BETWEEN 1 AND 256)
        AND (SELECT count(DISTINCT value) FROM unnest(p_roles) AS role(value)) = cardinality(p_roles), false)
$function$;

ALTER TABLE cortex_core.project_identities ADD CONSTRAINT project_identity_exact_scope UNIQUE(identity_id,project_scope_id);

CREATE TABLE cortex_context.boot_agent_binding_revisions (
    project_scope_id uuid NOT NULL REFERENCES cortex_auth.project_installations(scope_id),
    actor_id uuid NOT NULL,
    revision integer NOT NULL CHECK(revision > 0),
    state text NOT NULL CHECK(state IN ('active','retired')),
    identity_id uuid NOT NULL,
    persona_id uuid NOT NULL,
    persona_revision integer NOT NULL CHECK(persona_revision > 0),
    functional_roles text[] NOT NULL CHECK(cortex_context.valid_boot_roles(functional_roles)),
    enacted_by_principal uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    enacted_at timestamptz NOT NULL DEFAULT now(),
    source_reference text NOT NULL CHECK(length(source_reference) BETWEEN 1 AND 4096),
    PRIMARY KEY(project_scope_id,actor_id,revision),
    FOREIGN KEY(project_scope_id,actor_id) REFERENCES cortex_auth.memberships(scope_id,actor_id),
    FOREIGN KEY(identity_id,project_scope_id) REFERENCES cortex_core.project_identities(identity_id,project_scope_id),
    FOREIGN KEY(project_scope_id,persona_id,persona_revision) REFERENCES cortex_context.persona_revisions(scope_id,persona_id,revision)
);

CREATE TABLE cortex_context.boot_entry_binding_revisions (
    project_scope_id uuid NOT NULL REFERENCES cortex_auth.project_installations(scope_id),
    binding_id uuid NOT NULL,
    revision integer NOT NULL CHECK(revision > 0),
    subject_kind text NOT NULL CHECK(subject_kind IN ('agent','functional_role','project')),
    actor_id uuid,
    role_slug text CHECK(role_slug IS NULL OR length(role_slug) BETWEEN 1 AND 256),
    entry_kind text NOT NULL CHECK(entry_kind IN ('skill','rule')),
    entry_scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    skill_id uuid,
    rule_id uuid,
    bound_revision integer NOT NULL CHECK(bound_revision > 0),
    priority integer NOT NULL,
    state text NOT NULL CHECK(state IN ('active','retired')),
    enacted_by_principal uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    enacted_at timestamptz NOT NULL DEFAULT now(),
    source_reference text NOT NULL CHECK(length(source_reference) BETWEEN 1 AND 4096),
    PRIMARY KEY(project_scope_id,binding_id,revision),
    CHECK((subject_kind='agent' AND actor_id IS NOT NULL AND role_slug IS NULL)
       OR (subject_kind='functional_role' AND actor_id IS NULL AND role_slug IS NOT NULL)
       OR (subject_kind='project' AND actor_id IS NULL AND role_slug IS NULL)),
    CHECK((entry_kind='skill' AND skill_id IS NOT NULL AND rule_id IS NULL)
       OR (entry_kind='rule' AND rule_id IS NOT NULL AND skill_id IS NULL)),
    FOREIGN KEY(project_scope_id,actor_id) REFERENCES cortex_auth.memberships(scope_id,actor_id),
    FOREIGN KEY(entry_scope_id,skill_id,bound_revision) REFERENCES cortex_context.skill_revisions(scope_id,skill_id,revision),
    FOREIGN KEY(entry_scope_id,rule_id,bound_revision) REFERENCES cortex_context.rule_revisions(scope_id,rule_id,revision)
);

CREATE TABLE cortex_context.boot_catalogue_publication_revisions (
    installation_id uuid NOT NULL REFERENCES cortex_auth.installations(installation_id),
    publication_id uuid NOT NULL,
    revision integer NOT NULL CHECK(revision > 0),
    catalogue_scope_id uuid NOT NULL REFERENCES cortex_core.scopes(scope_id),
    entry_kind text NOT NULL CHECK(entry_kind IN ('skill','rule')),
    entry_id uuid NOT NULL,
    entry_revision integer NOT NULL CHECK(entry_revision > 0),
    state text NOT NULL CHECK(state IN ('active','retired')),
    published_by_principal uuid NOT NULL REFERENCES cortex_auth.principals(principal_id),
    published_at timestamptz NOT NULL DEFAULT now(),
    source_reference text NOT NULL CHECK(length(source_reference) BETWEEN 1 AND 4096),
    skill_id uuid GENERATED ALWAYS AS (CASE WHEN entry_kind='skill' THEN entry_id END) STORED,
    rule_id uuid GENERATED ALWAYS AS (CASE WHEN entry_kind='rule' THEN entry_id END) STORED,
    PRIMARY KEY(installation_id,publication_id,revision),
    FOREIGN KEY(catalogue_scope_id,skill_id,entry_revision) REFERENCES cortex_context.skill_revisions(scope_id,skill_id,revision),
    FOREIGN KEY(catalogue_scope_id,rule_id,entry_revision) REFERENCES cortex_context.rule_revisions(scope_id,rule_id,revision)
);

CREATE FUNCTION cortex_context.boot_current_owner(p_installation uuid,p_principal uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM cortex_auth.installation_owners AS owner
        JOIN cortex_auth.principals AS principal ON principal.principal_id=owner.principal_id
        JOIN cortex_auth.installations AS installation ON installation.installation_id=owner.installation_id
        WHERE owner.installation_id=p_installation AND owner.principal_id=p_principal
          AND p_principal=NULLIF(current_setting('cortex.principal_id',true),'')::uuid
          AND owner.revoked_at IS NULL AND principal.status='active'
          AND principal.installation_id=owner.installation_id AND installation.status='active')
$function$;

CREATE FUNCTION cortex_context.boot_project_manager(p_scope uuid,p_principal uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, pg_temp
AS $function$
    SELECT COALESCE(p_principal=NULLIF(current_setting('cortex.principal_id',true),'')::uuid
        AND (cortex_auth.is_project_owner(p_principal,p_scope) OR cortex_auth.project_lead(p_principal,p_scope)),false)
$function$;

CREATE FUNCTION cortex_context.boot_project_reader(p_installation uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_auth, cortex_core, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM cortex_auth.principals AS principal
        JOIN cortex_auth.installations AS installation ON installation.installation_id=principal.installation_id
        JOIN cortex_auth.project_installations AS project ON project.installation_id=installation.installation_id
        JOIN cortex_core.scopes AS scope ON scope.scope_id=project.scope_id
        JOIN cortex_auth.scope_grants AS grant_row ON grant_row.scope_id=scope.scope_id AND grant_row.principal_id=principal.principal_id
        WHERE principal.principal_id=NULLIF(current_setting('cortex.principal_id',true),'')::uuid
          AND principal.installation_id=p_installation AND principal.status='active'
          AND installation.status='active' AND scope.is_active AND scope.scope_kind='project'
          AND scope.scope_id=ANY(string_to_array(NULLIF(current_setting('cortex.read_scope_ids',true),''),',')::uuid[])
          AND grant_row.can_read AND grant_row.revoked_at IS NULL
          AND (EXISTS (SELECT 1 FROM cortex_auth.installation_owners AS owner
                       WHERE owner.installation_id=installation.installation_id AND owner.principal_id=principal.principal_id AND owner.revoked_at IS NULL)
            OR EXISTS (SELECT 1 FROM cortex_auth.actor_bindings AS binding
                       JOIN cortex_auth.actors AS actor ON actor.actor_id=binding.actor_id
                       JOIN cortex_auth.memberships AS membership ON membership.actor_id=actor.actor_id
                       WHERE binding.principal_id=principal.principal_id AND actor.installation_id=installation.installation_id
                         AND actor.status='active' AND membership.scope_id=scope.scope_id AND membership.status='active')))
$function$;

CREATE FUNCTION cortex_context.boot_published_revision(p_kind text,p_scope uuid,p_entry uuid,p_revision integer)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, cortex_context, cortex_auth, cortex_core, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM cortex_context.boot_catalogue_publication_revisions AS publication
        JOIN cortex_auth.installation_owners AS owner ON owner.installation_id=publication.installation_id AND owner.principal_id=publication.published_by_principal
        JOIN cortex_auth.principals AS principal ON principal.principal_id=owner.principal_id
        JOIN cortex_auth.installations AS installation ON installation.installation_id=publication.installation_id
        JOIN cortex_core.scopes AS catalogue ON catalogue.scope_id=publication.catalogue_scope_id
        JOIN cortex_auth.scope_grants AS curator ON curator.scope_id=catalogue.scope_id AND curator.principal_id=owner.principal_id
        WHERE publication.revision=(SELECT max(head.revision) FROM cortex_context.boot_catalogue_publication_revisions AS head
                                    WHERE head.installation_id=publication.installation_id AND head.publication_id=publication.publication_id)
          AND publication.state='active' AND publication.entry_kind=p_kind AND publication.catalogue_scope_id=p_scope
          AND publication.entry_id=p_entry AND publication.entry_revision=p_revision
          AND owner.revoked_at IS NULL AND principal.status='active' AND principal.installation_id=installation.installation_id
          AND installation.status='active' AND catalogue.scope_kind='shared' AND catalogue.is_active
          AND curator.can_read AND curator.can_publish AND curator.revoked_at IS NULL
          AND cortex_context.boot_project_reader(publication.installation_id))
$function$;

CREATE FUNCTION cortex_context.validate_boot_append()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, cortex_context, cortex_auth, cortex_core, pg_temp
AS $function$
DECLARE
    caller uuid := NULLIF(current_setting('cortex.principal_id',true),'')::uuid;
    stream_scope uuid;
    stream_key uuid;
    previous_revision integer;
    installation uuid;
    canonical_manifest jsonb;
    canonical_creator uuid;
    registered_roles jsonb;
    registered_name text;
    same_binding boolean;
BEGIN
    IF TG_TABLE_NAME='boot_catalogue_publication_revisions' THEN
        stream_scope := NEW.installation_id; stream_key := NEW.publication_id;
        IF NOT cortex_context.boot_current_owner(NEW.installation_id,NEW.published_by_principal) THEN
            RAISE EXCEPTION 'current installation owner required' USING ERRCODE='42501';
        END IF;
    ELSE
        stream_scope := NEW.project_scope_id;
        IF TG_TABLE_NAME='boot_agent_binding_revisions' THEN stream_key := NEW.actor_id;
        ELSE stream_key := NEW.binding_id; END IF;
        IF NOT cortex_context.boot_project_manager(NEW.project_scope_id,NEW.enacted_by_principal) THEN
            RAISE EXCEPTION 'current project lead or owner required' USING ERRCODE='42501';
        END IF;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(TG_TABLE_NAME||':'||stream_scope::text||':'||stream_key::text,0));
    IF TG_TABLE_NAME='boot_agent_binding_revisions' THEN
        SELECT COALESCE(max(revision),0) INTO previous_revision FROM cortex_context.boot_agent_binding_revisions
          WHERE project_scope_id=stream_scope AND actor_id=stream_key;
    ELSIF TG_TABLE_NAME='boot_entry_binding_revisions' THEN
        SELECT COALESCE(max(revision),0) INTO previous_revision FROM cortex_context.boot_entry_binding_revisions
          WHERE project_scope_id=stream_scope AND binding_id=stream_key;
    ELSE
        SELECT COALESCE(max(revision),0) INTO previous_revision FROM cortex_context.boot_catalogue_publication_revisions
          WHERE installation_id=stream_scope AND publication_id=stream_key;
    END IF;
    IF previous_revision=2147483647 OR NEW.revision<>previous_revision+1 THEN
        RAISE EXCEPTION 'boot revision must immediately follow its complete stream head' USING ERRCODE='23514';
    END IF;

    -- A retirement withdraws the exact previous binding. It requires current
    -- enact authority, but not eligibility/curator rights that were revoked.
    IF NEW.state='retired' THEN
        IF TG_TABLE_NAME='boot_agent_binding_revisions' THEN
            SELECT ROW(identity_id,persona_id,persona_revision,functional_roles)
                IS NOT DISTINCT FROM ROW(NEW.identity_id,NEW.persona_id,NEW.persona_revision,NEW.functional_roles)
                INTO same_binding FROM cortex_context.boot_agent_binding_revisions
                WHERE project_scope_id=stream_scope AND actor_id=stream_key AND revision=previous_revision;
        ELSIF TG_TABLE_NAME='boot_entry_binding_revisions' THEN
            SELECT ROW(subject_kind,actor_id,role_slug,entry_kind,entry_scope_id,skill_id,rule_id,bound_revision,priority)
                IS NOT DISTINCT FROM ROW(NEW.subject_kind,NEW.actor_id,NEW.role_slug,NEW.entry_kind,NEW.entry_scope_id,NEW.skill_id,NEW.rule_id,NEW.bound_revision,NEW.priority)
                INTO same_binding FROM cortex_context.boot_entry_binding_revisions
                WHERE project_scope_id=stream_scope AND binding_id=stream_key AND revision=previous_revision;
        ELSE
            SELECT ROW(catalogue_scope_id,entry_kind,entry_id,entry_revision)
                IS NOT DISTINCT FROM ROW(NEW.catalogue_scope_id,NEW.entry_kind,NEW.entry_id,NEW.entry_revision)
                INTO same_binding FROM cortex_context.boot_catalogue_publication_revisions
                WHERE installation_id=stream_scope AND publication_id=stream_key AND revision=previous_revision;
        END IF;
        IF same_binding IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'retirement must withdraw the exact previous binding' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME='boot_agent_binding_revisions' THEN
        SELECT CASE WHEN jsonb_typeof(identity.original_record->'functional_roles')='array' THEN identity.original_record->'functional_roles'
                    WHEN jsonb_typeof(identity.original_record->'role')='string' THEN jsonb_build_array(identity.original_record->>'role') END,
               identity.identity_name INTO registered_roles,registered_name
          FROM cortex_core.project_identities AS identity
          JOIN cortex_auth.actors AS actor ON actor.actor_id=NEW.actor_id AND actor.display_name=identity.identity_name
          JOIN cortex_auth.actor_bindings AS binding ON binding.actor_id=actor.actor_id
          JOIN cortex_auth.principals AS principal ON principal.principal_id=binding.principal_id
          JOIN cortex_auth.memberships AS membership ON membership.actor_id=actor.actor_id AND membership.scope_id=NEW.project_scope_id
          JOIN cortex_auth.project_installations AS project ON project.scope_id=membership.scope_id AND project.installation_id=actor.installation_id
          WHERE identity.identity_id=NEW.identity_id AND identity.project_scope_id=NEW.project_scope_id AND identity.identity_kind='agent'
            AND actor.actor_kind='agent' AND actor.status='active' AND membership.status='active'
            AND principal.status='active' AND principal.installation_id=actor.installation_id;
        SELECT boot_manifest INTO canonical_manifest FROM cortex_context.persona_revisions
          WHERE scope_id=NEW.project_scope_id AND persona_id=NEW.persona_id AND revision=NEW.persona_revision AND audience='agent_boot';
        IF registered_roles IS NULL OR canonical_manifest IS NULL
           OR (SELECT array_agg(value ORDER BY value) FROM jsonb_array_elements_text(registered_roles) AS role(value))
                IS DISTINCT FROM (SELECT array_agg(value ORDER BY value) FROM unnest(NEW.functional_roles) AS role(value))
           OR (SELECT array_agg(value ORDER BY value) FROM jsonb_array_elements_text(canonical_manifest->'functional_roles') AS role(value))
                IS DISTINCT FROM (SELECT array_agg(value ORDER BY value) FROM unnest(NEW.functional_roles) AS role(value))
           OR NOT EXISTS (SELECT 1 FROM cortex_core.project_profiles AS profile
                          WHERE profile.project_scope_id=NEW.project_scope_id AND profile.agent_name=registered_name
                            AND profile.original_record->>'profile_kind'='identity'
                            AND profile.original_record->>'profile_text'=canonical_manifest->>'identity_text') THEN
            RAISE EXCEPTION 'boot identity, roles and persona must match registered canonical facts' USING ERRCODE='23514';
        END IF;
    ELSE
        IF TG_TABLE_NAME='boot_entry_binding_revisions' THEN
            IF NEW.subject_kind='agent' AND NOT EXISTS (
                SELECT 1 FROM cortex_auth.memberships AS member
                JOIN cortex_auth.actors AS actor ON actor.actor_id=member.actor_id
                JOIN cortex_auth.actor_bindings AS binding ON binding.actor_id=actor.actor_id
                JOIN cortex_auth.principals AS principal ON principal.principal_id=binding.principal_id
                JOIN cortex_auth.project_installations AS project ON project.scope_id=member.scope_id
                WHERE member.scope_id=NEW.project_scope_id AND member.actor_id=NEW.actor_id
                  AND member.status='active' AND actor.status='active' AND actor.actor_kind='agent'
                  AND actor.installation_id=project.installation_id
                  AND principal.status='active' AND principal.installation_id=actor.installation_id) THEN
                RAISE EXCEPTION 'entry subject requires a current registered agent' USING ERRCODE='23514';
            END IF;
            SELECT installation_id INTO installation FROM cortex_auth.project_installations WHERE scope_id=NEW.project_scope_id;
            IF NEW.entry_kind='skill' THEN
                SELECT boot_manifest,created_by_principal INTO canonical_manifest,canonical_creator FROM cortex_context.skill_revisions
                  WHERE scope_id=NEW.entry_scope_id AND skill_id=NEW.skill_id AND revision=NEW.bound_revision;
            ELSE
                SELECT boot_manifest,created_by_principal INTO canonical_manifest,canonical_creator FROM cortex_context.rule_revisions
                  WHERE scope_id=NEW.entry_scope_id AND rule_id=NEW.rule_id AND revision=NEW.bound_revision;
            END IF;
            IF canonical_manifest IS NULL OR (NEW.entry_scope_id=NEW.project_scope_id AND canonical_manifest->>'scope'<>'project')
               OR (NEW.entry_scope_id<>NEW.project_scope_id AND NOT cortex_context.boot_published_revision(NEW.entry_kind,NEW.entry_scope_id,
                         CASE WHEN NEW.entry_kind='skill' THEN NEW.skill_id ELSE NEW.rule_id END,NEW.bound_revision)) THEN
                RAISE EXCEPTION 'boot entry requires an exact project or published catalogue revision' USING ERRCODE='23514';
            END IF;
        ELSE
            IF NEW.entry_kind='skill' THEN
                SELECT boot_manifest,created_by_principal INTO canonical_manifest,canonical_creator FROM cortex_context.skill_revisions
                  WHERE scope_id=NEW.catalogue_scope_id AND skill_id=NEW.entry_id AND revision=NEW.entry_revision;
            ELSE
                SELECT boot_manifest,created_by_principal INTO canonical_manifest,canonical_creator FROM cortex_context.rule_revisions
                  WHERE scope_id=NEW.catalogue_scope_id AND rule_id=NEW.entry_id AND revision=NEW.entry_revision;
            END IF;
            IF canonical_manifest IS NULL OR canonical_manifest->>'scope'<>'global'
               OR NOT EXISTS (SELECT 1 FROM cortex_core.scopes WHERE scope_id=NEW.catalogue_scope_id AND scope_kind='shared' AND is_active)
               OR NOT EXISTS (SELECT 1 FROM cortex_auth.principals WHERE principal_id=canonical_creator AND installation_id=NEW.installation_id)
               OR NOT EXISTS (SELECT 1 FROM cortex_auth.scope_grants WHERE principal_id=caller AND scope_id=NEW.catalogue_scope_id
                              AND can_read AND can_publish AND revoked_at IS NULL) THEN
                RAISE EXCEPTION 'publication requires a curated exact shared revision in this installation' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

DO $streams$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['boot_agent_binding_revisions','boot_entry_binding_revisions','boot_catalogue_publication_revisions'] LOOP
        EXECUTE format('ALTER TABLE cortex_context.%I ENABLE ROW LEVEL SECURITY',name);
        EXECUTE format('ALTER TABLE cortex_context.%I FORCE ROW LEVEL SECURITY',name);
        EXECUTE format('CREATE POLICY %I ON cortex_context.%I FOR ALL TO cortex_v2_migrator USING (true) WITH CHECK (true)',name||'_migrator',name);
        IF name='boot_catalogue_publication_revisions' THEN
            EXECUTE format('CREATE POLICY %I ON cortex_context.%I FOR SELECT TO cortex_v2_app USING (cortex_context.boot_project_reader(installation_id))',name||'_read',name);
            EXECUTE format('CREATE POLICY %I ON cortex_context.%I FOR INSERT TO cortex_v2_app WITH CHECK (cortex_context.boot_current_owner(installation_id,published_by_principal))',name||'_insert',name);
        ELSE
            EXECUTE format('CREATE POLICY %I ON cortex_context.%I FOR SELECT TO cortex_v2_app USING (cortex_core.scope_read_policy(project_scope_id))',name||'_read',name);
            EXECUTE format('CREATE POLICY %I ON cortex_context.%I FOR INSERT TO cortex_v2_app WITH CHECK (cortex_core.scope_write_policy(project_scope_id) AND cortex_context.boot_project_manager(project_scope_id,enacted_by_principal))',name||'_insert',name);
        END IF;
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON cortex_context.%I FOR EACH ROW EXECUTE FUNCTION cortex_context.validate_boot_append()',name||'_append',name);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON cortex_context.%I FOR EACH ROW EXECUTE FUNCTION cortex_context.reject_append_only_mutation()',name||'_immutable',name);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON cortex_context.%I FOR EACH STATEMENT EXECUTE FUNCTION cortex_context.reject_append_only_mutation()',name||'_no_truncate',name);
        EXECUTE format('REVOKE ALL ON cortex_context.%I FROM PUBLIC,cortex_v2_app',name);
        EXECUTE format('GRANT SELECT,INSERT ON cortex_context.%I TO cortex_v2_app',name);
    END LOOP;
END
$streams$;

CREATE POLICY skill_revisions_boot_published_read ON cortex_context.skill_revisions
    FOR SELECT TO cortex_v2_app USING(cortex_context.boot_published_revision('skill',scope_id,skill_id,revision));
CREATE POLICY rule_revisions_boot_published_read ON cortex_context.rule_revisions
    FOR SELECT TO cortex_v2_app USING(cortex_context.boot_published_revision('rule',scope_id,rule_id,revision));

REVOKE ALL ON FUNCTION cortex_context.valid_boot_roles(text[]),cortex_context.boot_current_owner(uuid,uuid),
    cortex_context.boot_project_manager(uuid,uuid),cortex_context.boot_project_reader(uuid),
    cortex_context.boot_published_revision(text,uuid,uuid,integer),cortex_context.validate_boot_append() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION cortex_context.valid_boot_roles(text[]),cortex_context.boot_current_owner(uuid,uuid),
    cortex_context.boot_project_manager(uuid,uuid),cortex_context.boot_project_reader(uuid),
    cortex_context.boot_published_revision(text,uuid,uuid,integer) TO cortex_v2_app;
