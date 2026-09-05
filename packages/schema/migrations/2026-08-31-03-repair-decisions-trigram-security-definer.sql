-- Repair fresh appliance databases whose baseline ledger included the original
-- decisions trigram bridge migration even though cortex-schema-full.sql did not
-- contain the function.  Replaying the fixed, idempotent definition under a new
-- migration id repairs both affected fresh installs and already-baselined volumes.

CREATE OR REPLACE FUNCTION public.cortex_search_decisions_trigram(
    p_project text,
    p_query text,
    p_room text,
    p_candidate_limit integer DEFAULT 100
)
RETURNS TABLE (
    id text,
    display_text text,
    meta text,
    category text,
    source text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog, public, pg_temp
AS $function$
    WITH trigram_candidates AS MATERIALIZED (
        SELECT
            d.id::text AS id,
            LEFT(d.summary, 150) AS display_text,
            d.category AS meta,
            d.agent_name AS category,
            LEFT(d.summary, 1000) AS rank_text
        FROM public.decisions AS d
        WHERE p_project = current_setting('cortex.project', true)
          AND d.summary ILIKE '%' || p_query || '%'
          AND d.project = p_project
          AND d.invalidated_at IS NULL
          AND (
              p_room IS NULL
              OR COALESCE(d.category, '') ILIKE '%' || p_room || '%'
              OR COALESCE(d.agent_name, '') ILIKE '%' || p_room || '%'
              OR LEFT(d.summary, 1000) ILIKE '%' || p_room || '%'
          )
        LIMIT LEAST(GREATEST(p_candidate_limit, 1), 100)
    )
    SELECT
        c.id,
        c.display_text,
        c.meta,
        c.category,
        'decisions'::text AS source
    FROM trigram_candidates AS c
    ORDER BY similarity(c.rank_text, p_query) DESC
    LIMIT 10
$function$;

ALTER FUNCTION public.cortex_search_decisions_trigram(text, text, text, integer)
    OWNER TO postgres;
REVOKE ALL ON FUNCTION public.cortex_search_decisions_trigram(text, text, text, integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cortex_search_decisions_trigram(text, text, text, integer)
    TO cortex_app;
