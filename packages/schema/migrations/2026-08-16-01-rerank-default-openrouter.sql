-- Move the rerank default onto the gateway that actually has a key.
--
-- The shipped default pair was (nvidia, nv-rerank-qa-mistral-4b:1), but NVIDIA_API_KEY is
-- set by no install path — and rerank_results() returns None when the key is missing, so
-- reranking was silently OFF on every deployment that never edited this row. Embeddings
-- already default to OpenRouter, so rerank now rides the same gateway.
--
-- The `:free` suffix is part of the OpenRouter model id: the bare
-- nvidia/llama-nemotron-rerank-vl-1b-v2 has no served endpoint and returns 404
-- "No endpoints found" (verified against POST /api/v1/rerank, 2026-08-16).

ALTER TABLE cortex_platform_config
    ALTER COLUMN rerank_provider SET DEFAULT 'openrouter',
    ALTER COLUMN rerank_model SET DEFAULT 'nvidia/llama-nemotron-rerank-vl-1b-v2:free';

-- Repair deployments still carrying the unusable default. Scoped to that exact pair so a
-- deliberate operator choice (any other provider/model) is never overwritten.
UPDATE cortex_platform_config
SET rerank_provider = 'openrouter',
    rerank_model = 'nvidia/llama-nemotron-rerank-vl-1b-v2:free',
    updated_at = now()
WHERE rerank_provider = 'nvidia'
  AND rerank_model = 'nv-rerank-qa-mistral-4b:1';
