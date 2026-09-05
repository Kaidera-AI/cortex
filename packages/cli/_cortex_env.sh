#!/usr/bin/env bash
# Shared Cortex connection config for root-level scripts.
# Source this file: source "$(dirname "$0")/_cortex_env.sh"
#
# Project-local .agents/config/runtime.yaml is the primary source of truth.
# This helper reads that config first and only falls back to defaults.
#
# Local-only secrets (OPENROUTER_API_KEY, ANTHROPIC_API_KEY) live in
# ../../local-cortex/.env per the local-cortex demarcation directive
# (2026-05-03). This script auto-loads that file when it exists so
# downstream cortex-* CLI tools have the keys available without the
# user having to manually `source local-cortex/.env` first.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── local-cortex/.env (canonical home for local-only secrets) ────────────
# Probe both repo-root/local-cortex and parent-workspace/local-cortex. This
# supports root-level `.agents/scripts` as well as packaged component layouts.
for _local_cortex_env in \
    "$(cd "${PROJECT_ROOT}/.." && pwd)/local-cortex/.env" \
    "$(cd "${PROJECT_ROOT}/../.." && pwd)/local-cortex/.env"; do
    if [ -f "${_local_cortex_env}" ]; then
        while IFS= read -r _cortex_env_line || [ -n "${_cortex_env_line}" ]; do
            case "${_cortex_env_line}" in
                ""|\#*) continue ;;
                export\ *) _cortex_env_line="${_cortex_env_line#export }" ;;
            esac

            _cortex_env_key="${_cortex_env_line%%=*}"
            _cortex_env_value="${_cortex_env_line#*=}"
            [[ "${_cortex_env_key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

            case "${_cortex_env_value}" in
                \"*\") _cortex_env_value="${_cortex_env_value#\"}"; _cortex_env_value="${_cortex_env_value%\"}" ;;
                \'*\') _cortex_env_value="${_cortex_env_value#\'}"; _cortex_env_value="${_cortex_env_value%\'}" ;;
            esac
            export "${_cortex_env_key}=${_cortex_env_value}"
        done <"${_local_cortex_env}"
        break
    fi
done
unset _local_cortex_env
unset _cortex_env_line
unset _cortex_env_key
unset _cortex_env_value

# Locate runtime.yaml. Two supported layouts:
#   1. Root-level scripts:     <repo>/scripts/_cortex_env.sh
#   2. Project-local scripts:  <repo>/.agents/scripts/_cortex_env.sh
# In layout 1, PROJECT_ROOT is the repo root. In layout 2, PROJECT_ROOT is the
# .agents/ directory itself. Probe both so the same script works in either.
if [ -n "${CORTEX_RUNTIME_CONFIG:-}" ]; then
    RUNTIME_CONFIG_FILE="${CORTEX_RUNTIME_CONFIG}"
elif [ -f "${PROJECT_ROOT}/config/runtime.yaml" ]; then
    # Layout 2: PROJECT_ROOT is already .agents/
    RUNTIME_CONFIG_FILE="${PROJECT_ROOT}/config/runtime.yaml"
elif [ -f "${PROJECT_ROOT}/.agents/config/runtime.yaml" ]; then
    # Layout 1: PROJECT_ROOT is the repo root, .agents/ is one level down
    RUNTIME_CONFIG_FILE="${PROJECT_ROOT}/.agents/config/runtime.yaml"
else
    RUNTIME_CONFIG_FILE="${PROJECT_ROOT}/.agents/config/runtime.yaml"
fi

cortex_runtime_yaml_value() {
    local section="$1"
    local key="$2"

    [ -f "${RUNTIME_CONFIG_FILE}" ] || return 0

    awk -v section="${section}" -v key="${key}" '
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        /^[^[:space:]].*:[[:space:]]*$/ {
            current = $0
            sub(/:[[:space:]]*$/, "", current)
            current = trim(current)
            next
        }
        current == section {
            pattern = "^[[:space:]]*" key ":[[:space:]]*"
            if ($0 ~ pattern) {
                value = $0
                sub(pattern, "", value)
                value = trim(value)
                gsub(/^["\047]|["\047]$/, "", value)
                print value
                exit
            }
        }
    ' "${RUNTIME_CONFIG_FILE}"
}

CONFIG_PROJECT_NAME="$(cortex_runtime_yaml_value project name)"
CONFIG_PG_PORT="$(cortex_runtime_yaml_value postgres port)"
CONFIG_PG_USER="$(cortex_runtime_yaml_value postgres user)"
CONFIG_PG_PASS="$(cortex_runtime_yaml_value postgres password)"
CONFIG_PG_DB="$(cortex_runtime_yaml_value postgres database)"
CONFIG_PG_CONTAINER="$(cortex_runtime_yaml_value postgres container_name)"

# Project scope — derived from runtime.yaml so each repo is isolated.
# Legacy callers without a runtime config should set CORTEX_PROJECT explicitly.
export CORTEX_PROJECT="${CORTEX_PROJECT:-${CONFIG_PROJECT_NAME:-}}"

# Cortex API (authoritative interface for all agent operations)
export CORTEX_API_URL="${CORTEX_API_URL:-http://localhost:8501}"

# Helper: check API is reachable
cortex_api_check() {
    curl -sS --max-time 5 "${CORTEX_API_URL}/health" >/dev/null 2>&1
}

# OpenRouter Embedding API
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-sentence-transformers/all-mpnet-base-v2}"

# v2.3 — Analysis LLM config (uses free OpenRouter models by default)
# Leave CORTEX_ANALYSIS_MODEL empty to use the fallback chain.
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export CORTEX_ANALYSIS_MODEL="${CORTEX_ANALYSIS_MODEL:-}"
export CORTEX_ANALYSIS_PROVIDER="${CORTEX_ANALYSIS_PROVIDER:-}"
export CORTEX_ANALYSIS_FALLBACK_MODELS="${CORTEX_ANALYSIS_FALLBACK_MODELS:-nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free,minimax/minimax-m2.5:free,openai/gpt-oss-120b:free}"
