CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lessons_project
    ON public.lessons (project);
