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
