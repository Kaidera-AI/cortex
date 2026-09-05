-- Keep the E2 cold-tier lookup online while upgrading a populated archive.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_archive_messages_raw_session
    ON public.archive_messages (raw_session_id);
