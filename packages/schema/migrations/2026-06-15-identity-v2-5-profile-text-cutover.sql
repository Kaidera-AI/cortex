-- Identity v2 profile-text cutover.
--
-- The clean schema removed stored project hex identity, but generated persona
-- mirrors are sourced from agent_profiles.profile_text. Transform that source
-- text and metadata so regenerated identities carry agent@project and no
-- project_hex frontmatter.

BEGIN;

INSERT INTO cortex_legacy_identity_archive (
    archive_reason, source_schema, source_table, source_pk,
    project_key, project_id, legacy_payload
)
SELECT
    'identity-v2-profile-text-cutover',
    'public',
    'agent_profiles',
    ap.id::text,
    cp.project_key,
    cp.id,
    to_jsonb(ap)
FROM agent_profiles ap
JOIN cortex_projects cp
  ON cp.project_key = lower(btrim(ap.project))
WHERE (
        COALESCE(ap.profile_text, '') ~* 'project_hex'
     OR COALESCE(ap.profile_text, '') ~* '[a-z][a-z0-9_-]*:[a-f0-9?]{4}'
     OR COALESCE(ap.metadata::text, '') ~* 'project_hex'
)
AND NOT EXISTS (
    SELECT 1
      FROM cortex_legacy_identity_archive a
     WHERE a.archive_reason = 'identity-v2-profile-text-cutover'
       AND a.source_schema = 'public'
       AND a.source_table = 'agent_profiles'
       AND a.source_pk = ap.id::text
);

WITH profile_scope AS (
    SELECT
        ap.id,
        cortex_identity_base(ap.agent_name) AS agent_slug,
        cp.project_key,
        cortex_identity_display(cortex_identity_base(ap.agent_name), cp.project_key) AS display_identity
    FROM agent_profiles ap
    JOIN cortex_projects cp
      ON cp.project_key = lower(btrim(ap.project))
    WHERE ap.profile_kind = 'identity'
      AND ap.agent_name IS NOT NULL
      AND cortex_identity_base(ap.agent_name) IS NOT NULL
),
rewritten AS (
    SELECT
        ap.id,
        regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            COALESCE(ap.profile_text, ''),
                            E'(^|\\n)project_hex:[^\\n]*(\\n|$)',
                            E'\\1',
                            'g'
                        ),
                        '(^|[^[:alnum:]_@-])' || ps.agent_slug || ':[a-f0-9?]{4}([^[:alnum:]_@-]|$)',
                        E'\\1' || ps.display_identity || E'\\2',
                        'gi'
                    ),
                    'Compound identity always\\s+`[^`]+`\\.',
                    'Working identity is `' || ps.display_identity || '`.',
                    'gi'
                ),
                'Use the compound identity\\s+`[^`]+`',
                'Use the identity `' || ps.display_identity || '`',
                'gi'
            ),
            'compound identity',
            'identity',
            'gi'
        ) AS profile_text,
        CASE
            WHEN ap.metadata ? 'frontmatter' THEN
                jsonb_set(
                    ap.metadata,
                    '{frontmatter}',
                    COALESCE(ap.metadata->'frontmatter', '{}'::jsonb) - 'project_hex',
                    true
                )
            ELSE ap.metadata
        END AS metadata
    FROM agent_profiles ap
    JOIN profile_scope ps
      ON ps.id = ap.id
)
UPDATE agent_profiles ap
   SET profile_text = rewritten.profile_text,
       metadata = rewritten.metadata,
       updated_at = NOW()
  FROM rewritten
 WHERE rewritten.id = ap.id
   AND (
        ap.profile_text IS DISTINCT FROM rewritten.profile_text
     OR ap.metadata IS DISTINCT FROM rewritten.metadata
   );

COMMIT;
