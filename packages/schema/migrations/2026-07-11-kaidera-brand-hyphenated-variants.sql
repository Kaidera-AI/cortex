-- Canonicalize hyphenated/underscored retired-brand variants in active runtime data.
-- Historical messages, decisions, and artifacts retain their original provenance.

BEGIN;

CREATE FUNCTION pg_temp.kaidera_variant_text(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT regexp_replace(
             regexp_replace(
               regexp_replace(
                 regexp_replace(
                   regexp_replace(
                     regexp_replace(value,
                       'EnGen[-_]OS', 'Kaidera OS', 'g'),
                     'EnGen[-_]AI', 'Kaidera AI', 'g'),
                   'ENGEN[-_]OS', 'KAIDERA_OS', 'g'),
                 'ENGEN[-_]AI', 'KAIDERA_AI', 'g'),
               'engen[-_]os', 'kaidera-os', 'g'),
             'engen[-_]ai', 'kaidera-ai', 'g');
$$;

CREATE FUNCTION pg_temp.kaidera_variant_json(value jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT pg_temp.kaidera_variant_text(value::text)::jsonb;
$$;

UPDATE agent_profiles
   SET profile_text = pg_temp.kaidera_variant_text(profile_text),
       source_file = pg_temp.kaidera_variant_text(source_file),
       metadata = pg_temp.kaidera_variant_json(metadata),
       updated_at = NOW()
 WHERE project = 'kaidera-os'
   AND (
       profile_text ~* 'engen[-_](os|ai)'
       OR source_file ~* 'engen[-_](os|ai)'
       OR metadata::text ~* 'engen[-_](os|ai)'
   );

UPDATE rules
   SET rule_slug = pg_temp.kaidera_variant_text(rule_slug),
       title = pg_temp.kaidera_variant_text(title),
       body = pg_temp.kaidera_variant_text(body),
       source_file = pg_temp.kaidera_variant_text(source_file)
 WHERE project = 'kaidera-os'
   AND (
       rule_slug ~* 'engen[-_](os|ai)'
       OR title ~* 'engen[-_](os|ai)'
       OR body ~* 'engen[-_](os|ai)'
       OR source_file ~* 'engen[-_](os|ai)'
   );

UPDATE roles
   SET default_capabilities = pg_temp.kaidera_variant_json(default_capabilities),
       description = pg_temp.kaidera_variant_text(description),
       source_file = pg_temp.kaidera_variant_text(source_file),
       updated_at = NOW()
 WHERE project = 'kaidera-os'
   AND (
       default_capabilities::text ~* 'engen[-_](os|ai)'
       OR description ~* 'engen[-_](os|ai)'
       OR source_file ~* 'engen[-_](os|ai)'
   );

UPDATE agents
   SET capabilities = pg_temp.kaidera_variant_json(capabilities),
       runtime_state = pg_temp.kaidera_variant_json(runtime_state)
 WHERE project = 'kaidera-os'
   AND (
       capabilities::text ~* 'engen[-_](os|ai)'
       OR runtime_state::text ~* 'engen[-_](os|ai)'
   );

COMMIT;
