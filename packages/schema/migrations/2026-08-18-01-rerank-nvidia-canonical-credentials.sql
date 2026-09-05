-- Move reranking to NVIDIA, now that Cortex reads the ONE canonical credential store.
--
-- Until 2026-08-18 Cortex resolved provider keys from its own process env, separate from
-- the KOS settings store the console writes and tests. That second plane is why NVIDIA
-- was unusable: `nvidia_api_key` was configured in the system and cortex-api could not
-- see it, so an nvidia default meant no rerank at all and the default was moved to
-- OpenRouter in 2026-08-16-01. Cortex now resolves `<provider>_api_key` from
-- `app_settings`, so the configured NVIDIA credential is reachable and that workaround
-- is no longer needed.
--
-- Verified live before switching, with the key resolved from the settings store:
--   POST https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking
--   model nv-rerank-qa-mistral-4b:1  -> HTTP 200, correct ordering
--   (relevant passage logit -1.90 vs irrelevant -17.34)
--
-- EMBEDDING IS DELIBERATELY NOT MOVED. NVIDIA's nv-embedqa-e5-v5 returns 1024-dim
-- vectors and this schema stores vector(768) with HNSW indexes built on it; the API
-- rejects mismatched dimensions. Switching would invalidate every stored embedding and
-- require a full re-embed plus index rebuild, which is a planned migration, not a
-- default change. nvidia/llama-3.2-nv-embedqa-1b-v2 is HTTP 410 Gone.

ALTER TABLE cortex_platform_config
    ALTER COLUMN rerank_provider SET DEFAULT 'nvidia',
    ALTER COLUMN rerank_model SET DEFAULT 'nv-rerank-qa-mistral-4b:1';

-- Move deployments still on the OpenRouter workaround. Scoped to that exact pair so a
-- deliberate operator choice is never overwritten.
UPDATE cortex_platform_config
SET rerank_provider = 'nvidia',
    rerank_model = 'nv-rerank-qa-mistral-4b:1',
    updated_at = now()
WHERE rerank_provider = 'openrouter'
  AND rerank_model = 'nvidia/llama-nemotron-rerank-vl-1b-v2:free';
