CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lessons_agent_id
    ON public.lessons (agent_id);
