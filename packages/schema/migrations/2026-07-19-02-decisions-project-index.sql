CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decisions_project
    ON public.decisions (project);
