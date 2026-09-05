-- 2026-06-25 L4 graph tables: app-owned + FORCE RLS
--
-- ensure_graph_schema runs idempotent DDL (ALTER/CREATE INDEX) on the L4 graph
-- tables AT REQUEST TIME as the scoped cortex_app role. DDL needs table
-- OWNERSHIP, but 00-cortex-bootstrap.sh grants cortex_app DML only and leaves
-- the tables owned by postgres -> /cortex-graph-extract 500s with
-- "must be owner of table cortex_entities" on every deployment (confirmed on
-- the DXB deployment: graph tab empty, 0 entities).
--
-- Fix: make cortex_app the owner of the two L4 graph tables so its runtime DDL
-- succeeds, and FORCE ROW LEVEL SECURITY so the owner is STILL subject to the
-- project-isolation policy (a table owner otherwise BYPASSES RLS, which would
-- weaken multi-project isolation). Idempotent + safe to re-run.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='cortex_entities') THEN
    EXECUTE 'ALTER TABLE public.cortex_entities OWNER TO cortex_app';
    EXECUTE 'ALTER TABLE public.cortex_entities FORCE ROW LEVEL SECURITY';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='public' AND table_name='cortex_relationships') THEN
    EXECUTE 'ALTER TABLE public.cortex_relationships OWNER TO cortex_app';
    EXECUTE 'ALTER TABLE public.cortex_relationships FORCE ROW LEVEL SECURITY';
  END IF;
END $$;
