-- memory-efficiency: REINDEX the big text-search GIN/trgm indexes to reclaim bloat from
-- heavy update churn (same root cause as the ivfflat bloat). CONCURRENTLY = no lock.
REINDEX INDEX CONCURRENTLY public.idx_messages_search_vector;
