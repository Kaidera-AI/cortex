-- Supersedes the blocking multi-index 2026-08-16-03 migration on fresh installs.
DROP INDEX CONCURRENTLY IF EXISTS public.idx_lessons_embedding;
