-- Hot-path composite index for the per-project transcript read: messages(project, ts DESC).
--
-- `cortex_doctor_hot_path_index_check` warns when no messages index matches
-- `(project, ts[ DESC])`. marlow had `idx_messages_project` and `idx_messages_ts` as
-- SEPARATE single-column indexes, which cannot serve the shape the history hot path
-- actually issues -- filter by project, then ORDER BY ts DESC LIMIT n. Postgres can
-- scan one index and sort, or bitmap-and the two and sort, but neither returns rows in
-- ts order within a project, so the LIMIT cannot short-circuit and the sort grows with
-- the project's whole message history.
--
-- The composite is ordered (project, ts DESC) rather than (ts, project) deliberately:
-- the equality predicate must lead so the ordered range scan starts inside one project.
--
-- CONCURRENTLY matches the house style for index migrations (see
-- 2026-07-19-03-decisions-agent-index.sql) and keeps writes available on deployments
-- where messages is large and actively written.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_project_ts
    ON public.messages (project, ts DESC);
