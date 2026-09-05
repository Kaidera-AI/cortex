CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_decisions_agent_id
    ON public.decisions (agent_id);
