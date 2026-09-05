-- Identity v2 profile-text polish.
--
-- A defensive follow-up for rows normalized by older seed code that rewrote
-- "Compound identity always ..." to "identity always ...". Keep generated
-- persona mirrors readable and consistent.

BEGIN;

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
            COALESCE(ap.profile_text, ''),
            '\\midentity always\\s+`[^`]+`\\.',
            'Working identity is `' || ps.display_identity || '`.',
            'gi'
        ) AS profile_text
    FROM agent_profiles ap
    JOIN profile_scope ps
      ON ps.id = ap.id
)
UPDATE agent_profiles ap
   SET profile_text = rewritten.profile_text,
       updated_at = NOW()
  FROM rewritten
 WHERE rewritten.id = ap.id
   AND ap.profile_text IS DISTINCT FROM rewritten.profile_text;

COMMIT;
