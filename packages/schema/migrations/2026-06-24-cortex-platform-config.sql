-- Cortex platform config: central ingestion/search model settings.
--
-- This is the API-owned source of truth for Cortex ingestion models. The console
-- may edit it through the admin API, but embedding/rerank runtime reads this row
-- directly so deployments do not drift through process-only env constants.

CREATE TABLE IF NOT EXISTS cortex_platform_config (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    embedding_provider TEXT NOT NULL DEFAULT 'openrouter',
    embedding_model TEXT NOT NULL DEFAULT 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
    embedding_dims INTEGER NOT NULL DEFAULT 768 CHECK (embedding_dims > 0),
    rerank_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    rerank_provider TEXT NOT NULL DEFAULT 'nvidia',
    rerank_model TEXT NOT NULL DEFAULT 'nv-rerank-qa-mistral-4b:1',
    analysis_provider TEXT NOT NULL DEFAULT 'openrouter',
    analysis_model TEXT NOT NULL DEFAULT 'google/gemma-4-31b-it:free',
    cortex_api_url TEXT NOT NULL DEFAULT 'http://localhost:8501',
    boot_context_version TEXT NOT NULL DEFAULT 'v2',
    max_boot_tokens INTEGER NOT NULL DEFAULT 250 CHECK (max_boot_tokens > 0),
    search_confidence_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.015,
    rrf_k INTEGER NOT NULL DEFAULT 60 CHECK (rrf_k > 0),
    embed_input_max_chars INTEGER NOT NULL DEFAULT 500 CHECK (embed_input_max_chars > 0),
    rerank_input_max_chars INTEGER NOT NULL DEFAULT 500 CHECK (rerank_input_max_chars > 0),
    embed_timeout_ms INTEGER NOT NULL DEFAULT 15000 CHECK (embed_timeout_ms > 0),
    rerank_timeout_ms INTEGER NOT NULL DEFAULT 15000 CHECK (rerank_timeout_ms > 0),
    analysis_timeout_ms INTEGER NOT NULL DEFAULT 90000 CHECK (analysis_timeout_ms > 0),
    embedding_provider_config_id UUID,
    rerank_provider_config_id UUID,
    analysis_provider_config_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cortex_platform_config
    ADD COLUMN IF NOT EXISTS embedding_provider TEXT NOT NULL DEFAULT 'openrouter',
    ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'nvidia/llama-nemotron-embed-vl-1b-v2:free',
    ADD COLUMN IF NOT EXISTS embedding_dims INTEGER NOT NULL DEFAULT 768,
    ADD COLUMN IF NOT EXISTS rerank_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS rerank_provider TEXT NOT NULL DEFAULT 'nvidia',
    ADD COLUMN IF NOT EXISTS rerank_model TEXT NOT NULL DEFAULT 'nv-rerank-qa-mistral-4b:1',
    ADD COLUMN IF NOT EXISTS analysis_provider TEXT NOT NULL DEFAULT 'openrouter',
    ADD COLUMN IF NOT EXISTS analysis_model TEXT NOT NULL DEFAULT 'google/gemma-4-31b-it:free',
    ADD COLUMN IF NOT EXISTS cortex_api_url TEXT NOT NULL DEFAULT 'http://localhost:8501',
    ADD COLUMN IF NOT EXISTS boot_context_version TEXT NOT NULL DEFAULT 'v2',
    ADD COLUMN IF NOT EXISTS max_boot_tokens INTEGER NOT NULL DEFAULT 250,
    ADD COLUMN IF NOT EXISTS search_confidence_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.015,
    ADD COLUMN IF NOT EXISTS rrf_k INTEGER NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS embed_input_max_chars INTEGER NOT NULL DEFAULT 500,
    ADD COLUMN IF NOT EXISTS rerank_input_max_chars INTEGER NOT NULL DEFAULT 500,
    ADD COLUMN IF NOT EXISTS embed_timeout_ms INTEGER NOT NULL DEFAULT 15000,
    ADD COLUMN IF NOT EXISTS rerank_timeout_ms INTEGER NOT NULL DEFAULT 15000,
    ADD COLUMN IF NOT EXISTS analysis_timeout_ms INTEGER NOT NULL DEFAULT 90000,
    ADD COLUMN IF NOT EXISTS embedding_provider_config_id UUID,
    ADD COLUMN IF NOT EXISTS rerank_provider_config_id UUID,
    ADD COLUMN IF NOT EXISTS analysis_provider_config_id UUID,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

INSERT INTO cortex_platform_config (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
