-- Keep the messages full-text index reachable through the cortex_app RLS barrier.
--
-- PostgreSQL cannot push the non-leakproof @@ predicate below row security. On
-- the local Apple Container corpus that made the RLS role sequentially scan the
-- full messages table even though idx_messages_search_vector existed. This
-- deliberately narrow function runs the fixed query as its postgres owner, but
-- returns rows only when the requested project exactly matches the non-empty
-- session scope set by acquire_scoped(). It accepts no identifiers or dynamic
-- SQL and exposes only the fields already returned by the BM25 messages stage.

CREATE OR REPLACE FUNCTION public.cortex_search_messages_bm25(
    p_project text,
    p_query text,
    p_room text
)
RETURNS TABLE (
    id text,
    display_text text,
    source text,
    score real
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path TO pg_catalog, pg_temp
AS $function$
    WITH ranked_ids AS MATERIALIZED (
        SELECT
            m.id,
            pg_catalog.ts_rank_cd(
                m.search_vector,
                pg_catalog.plainto_tsquery(
                    'pg_catalog.english'::pg_catalog.regconfig,
                    p_query
                )
            ) AS score
        FROM public.messages AS m
        WHERE p_project = NULLIF(
                  pg_catalog.current_setting('cortex.project', true),
                  ''
              )
          AND m.project = p_project
          AND m.search_vector @@ pg_catalog.plainto_tsquery(
                  'pg_catalog.english'::pg_catalog.regconfig,
                  p_query
              )
          AND (
              p_room IS NULL
              OR pg_catalog.left(m.content, 1000) ILIKE '%' || p_room || '%'
          )
        ORDER BY score DESC
        LIMIT 10
    )
    SELECT
        ranked.id::text AS id,
        pg_catalog.left(m.content, 300) AS display_text,
        'messages'::text AS source,
        ranked.score
    FROM ranked_ids AS ranked
    JOIN public.messages AS m ON m.id = ranked.id
    ORDER BY ranked.score DESC
$function$;

ALTER FUNCTION public.cortex_search_messages_bm25(text, text, text)
    OWNER TO postgres;
REVOKE ALL ON FUNCTION public.cortex_search_messages_bm25(text, text, text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cortex_search_messages_bm25(text, text, text)
    TO cortex_app;
