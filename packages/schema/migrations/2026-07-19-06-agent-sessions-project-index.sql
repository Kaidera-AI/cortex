CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_sessions_project
    ON public.agent_sessions (project);
