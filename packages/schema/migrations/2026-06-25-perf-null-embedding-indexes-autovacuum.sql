-- 2026-06-25 perf: partial NULL-embedding indexes + autovacuum tuning
--
-- The Settings -> Cortex embedding-coverage card runs a per-table COUNT of rows
-- with embedding IS NULL. On the shared messages table (3.9GB / ~518k rows) this
-- was a Parallel Seq Scan (~6.8s, ~1.3GB physical read) because the predicate hit
-- no index, and it compounded to 9->20->35s under buffer-cache thrash. A partial
-- index over just the NULL-embedding rows turns that COUNT into a small index scan.
-- Also tighten autovacuum on the two hot/bloated tables (decisions had ~17k dead
-- tuples, last_autovacuum NULL) so dead tuples are reclaimed promptly.
--
-- NOTE: non-CONCURRENT CREATE INDEX takes a brief ACCESS EXCLUSIVE lock while it
-- builds — acceptable on this single-operator local deployment. Idempotent.

CREATE INDEX IF NOT EXISTS idx_messages_project_embedding_null
    ON public.messages (project) WHERE embedding IS NULL;

CREATE INDEX IF NOT EXISTS idx_knowledge_project_embedding_null
    ON public.knowledge (project) WHERE embedding IS NULL;

ALTER TABLE public.decisions
    SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01);
ALTER TABLE public.messages
    SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_analyze_scale_factor = 0.01);
