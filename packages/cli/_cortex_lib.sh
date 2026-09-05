#!/usr/bin/env bash
# Agent Cortex shared library
# Replaces _lib.sh — adds PostgreSQL helpers and project scoping. Redis-era stream
# plumbing was removed 2026-09-01: Cortex is Postgres-backed and events are written
# server-side by the API; every former publish call was a silent no-op.
# Source this file: source "$(dirname "$0")/_cortex_lib.sh"

set -euo pipefail

# ---------------------------------------------------------------------------
# Directory resolution
# ---------------------------------------------------------------------------

# Resolve AGENTS_DIR to the active workspace's .agents/ directory. These
# scripts are often shared by symlink, so prefer the invocation workspace over
# the physical script install path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SCRIPT_AGENTS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cortex_agents_dir_from_invocation() {
    local dir="${CORTEX_PROJECT_ROOT:-${PWD:-}}"
    [ -n "${dir}" ] || return 1
    dir="$(cd "${dir}" 2>/dev/null && pwd || true)"
    [ -n "${dir}" ] || return 1

    while [ "${dir}" != "/" ]; do
        if [ -f "${dir}/.agents/config/workspace.json" ] \
           || [ -f "${dir}/.agents/config/runtime.yaml" ] \
           || [ -d "${dir}/.agents/scripts" ]; then
            printf '%s/.agents\n' "${dir}"
            return 0
        fi
        [ "${dir}" = "${HOME:-}" ] && break
        dir="$(dirname "${dir}")"
    done
    return 1
}

AGENTS_DIR="${CORTEX_AGENTS_DIR:-}"
if [ -z "${AGENTS_DIR}" ]; then
    AGENTS_DIR="$(cortex_agents_dir_from_invocation || true)"
fi
AGENTS_DIR="${AGENTS_DIR:-${SCRIPT_AGENTS_DIR}}"
MEMORY_DIR="${AGENTS_DIR}/memory"
SCRIPTS_DIR="${AGENTS_DIR}/scripts"
RUNTIME_CONFIG_FILE="${CORTEX_RUNTIME_CONFIG:-${AGENTS_DIR}/config/runtime.yaml}"
WORKSPACE_CONFIG_FILE="${CORTEX_WORKSPACE_CONFIG:-}"

# ---------------------------------------------------------------------------
# Runtime config loading
# Allows each repo to declare its own project scope without patching scripts.
# ---------------------------------------------------------------------------

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
                gsub(/^["'"'"']|["'"'"']$/, "", value)
                print value
                exit
            }
        }
    ' "${RUNTIME_CONFIG_FILE}"
}

cortex_runtime_top_level_value() {
    local key="$1"

    [ -f "${RUNTIME_CONFIG_FILE}" ] || return 0

    awk -v key="${key}" '
        function trim(value) {
            sub(/^[[:space:]]+/, "", value)
            sub(/[[:space:]]+$/, "", value)
            return value
        }
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        /^[^[:space:]].*:[[:space:]]*$/ {
            next
        }
        {
            pattern = "^" key ":[[:space:]]*"
            if ($0 ~ pattern) {
                value = $0
                sub(pattern, "", value)
                value = trim(value)
                gsub(/^["'"'"']|["'"'"']$/, "", value)
                print value
                exit
            }
        }
    ' "${RUNTIME_CONFIG_FILE}"
}

cortex_workspace_config_candidates() {
    local repo_root
    local workspace_root
    repo_root="$(cd "${AGENTS_DIR}/.." && pwd)"
    workspace_root="$(dirname "${repo_root}")"

    for candidate in \
        "${AGENTS_DIR}/config/workspace.json" \
        "${repo_root}/.agents/config/workspace.json" \
        "${workspace_root}/.agents/config/workspace.json"; do
        [ -f "${candidate}" ] && printf '%s\n' "${candidate}"
    done | awk '!seen[$0]++'

    return 0
}

cortex_workspace_project_from_path_json() {
    local path="$1"

    if [ -z "${WORKSPACE_CONFIG_FILE}" ] || [ ! -f "${WORKSPACE_CONFIG_FILE}" ]; then
        return 1
    fi

    python3 - "${WORKSPACE_CONFIG_FILE}" "${path}" <<'PYEOF'
import json
import os
import sys

config_path = sys.argv[1]
path = os.path.realpath(sys.argv[2])

with open(config_path, "r") as handle:
    config = json.load(handle)

best_project = None
best_len = -1

for project in config.get("projects", []):
    for root in project.get("roots", []):
        root_path = root.get("path", "")
        if not root_path:
            continue
        root_path = os.path.realpath(root_path)
        if path == root_path or path.startswith(root_path + os.sep):
            if len(root_path) > best_len:
                best_project = project.get("key")
                best_len = len(root_path)

if best_project:
    print(best_project)
PYEOF
}

if [ -z "${WORKSPACE_CONFIG_FILE}" ] || [ ! -f "${WORKSPACE_CONFIG_FILE}" ]; then
    WORKSPACE_CONFIG_FILE="$(cortex_workspace_config_candidates 2>/dev/null | head -1 || true)"
fi

# ---------------------------------------------------------------------------
# Project scoping
# Prevents key/table collisions when multiple projects share the same store.
# ---------------------------------------------------------------------------

CONFIG_PROJECT_NAME="$(cortex_runtime_yaml_value project name)"
CONFIG_RUNTIME="$(cortex_runtime_top_level_value runtime)"
CONFIG_SHARED_VENDOR_ROOT="$(cortex_runtime_top_level_value shared_vendor_root)"
CONFIG_PG_PORT="$(cortex_runtime_yaml_value postgres port)"
CONFIG_PG_CONTAINER="$(cortex_runtime_yaml_value postgres container_name)"
CONFIG_PG_DB="$(cortex_runtime_yaml_value postgres database)"
CONFIG_PG_USER="$(cortex_runtime_yaml_value postgres user)"
CONFIG_PG_PASS="$(cortex_runtime_yaml_value postgres password)"
AUTO_PROJECT_NAME="$(cortex_workspace_project_from_path_json "$(cd "${AGENTS_DIR}/.." && pwd)" 2>/dev/null || true)"
WORKSPACE_PROJECT_NAME="${AUTO_PROJECT_NAME:-}"

CORTEX_PROJECT="${CORTEX_PROJECT:-${WORKSPACE_PROJECT_NAME:-${CONFIG_PROJECT_NAME:-}}}"
CORTEX_WORKSPACE_PROJECT="${WORKSPACE_PROJECT_NAME:-}"

if [ -z "${CORTEX_PROJECT}" ]; then
    cat >&2 <<EOF
ERROR: CORTEX_PROJECT is not set.

Configure this deployment with the startup wizard, provide .agents/config/runtime.yaml,
or export CORTEX_PROJECT for this command. Kaidera OS will not guess a project key.
EOF
    exit 67
fi

# Functional discipline: a workspace must not silently operate against another
# Cortex project. Cross-project reads/writes require a one-off CTO override.
if [ -n "${CORTEX_WORKSPACE_PROJECT}" ] \
   && [ -n "${CORTEX_PROJECT}" ] \
   && [ "${CORTEX_PROJECT}" != "${CORTEX_WORKSPACE_PROJECT}" ]; then
    if [ -z "${CORTEX_CTO_OVERRIDE:-}" ]; then
        cat >&2 <<EOF
ERROR: Cortex project isolation guard blocked a cross-project context.
  Workspace project: ${CORTEX_WORKSPACE_PROJECT}
  Requested CORTEX_PROJECT: ${CORTEX_PROJECT}

Run from the correct workspace or use a one-off CTO override for a single command:
  CORTEX_CTO_OVERRIDE=<decision-id> CORTEX_PROJECT=${CORTEX_PROJECT} <command>

Do not export persistent cross-project overrides.
EOF
        exit 66
    else
        echo "WARNING: CTO override ${CORTEX_CTO_OVERRIDE} permits one-off cross-project context ${CORTEX_WORKSPACE_PROJECT} -> ${CORTEX_PROJECT}. Do not persist." >&2
    fi
fi

# ---------------------------------------------------------------------------
# PostgreSQL config
# ---------------------------------------------------------------------------

# Auto-detect psql if not on PATH (Homebrew libpq / postgresql@18, @16 kept as fallback)
if ! command -v psql >/dev/null 2>&1; then
    for _pg_prefix in /opt/homebrew/opt/libpq/bin /opt/homebrew/opt/postgresql@18/bin /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/libpq/bin; do
        if [ -x "${_pg_prefix}/psql" ]; then
            export PATH="${_pg_prefix}:${PATH}"
            break
        fi
    done
fi

PG_PORT="${PG_PORT:-${CONFIG_PG_PORT:-5499}}"
PG_USER="${PG_USER:-${CONFIG_PG_USER:-postgres}}"
PG_PASS="${PG_PASS:-${CONFIG_PG_PASS:-postgres}}"
PG_DB="${PG_DB:-${CONFIG_PG_DB:-platform_agent_memory}}"
PG_CONTAINER="${PG_CONTAINER:-${CONFIG_PG_CONTAINER:-cortex-pg}}"

# ---------------------------------------------------------------------------
# Container runtime detection
# Auto mode follows the supported production matrix: rootless Podman everywhere
# (macOS via podman machine). Legacy engines are migration inputs, not runtime
# fallbacks for the Cortex command surface.
# ---------------------------------------------------------------------------

detect_runtime() {
    case "$(uname -s)" in
        Linux|Darwin)
            if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
                echo "podman"
            else
                echo "none"
            fi
            ;;
        *) echo "none" ;;
    esac
}

# Cache the runtime so we only detect once per session
RUNTIME="${RUNTIME:-${CONFIG_RUNTIME:-}}"
case "${RUNTIME}" in
    ""|auto) RUNTIME="$(detect_runtime)" ;;
    podman|none) ;;
    *)
        printf 'ERROR: unsupported Cortex runtime %s; rootless Podman is the only current engine\n' "${RUNTIME}" >&2
        RUNTIME="none"
        ;;
esac

# Export core config so nested shells and Python helpers inherit the active
# project scope and data-layer settings.
export AGENTS_DIR MEMORY_DIR SCRIPTS_DIR \
    RUNTIME_CONFIG_FILE WORKSPACE_CONFIG_FILE \
    CORTEX_PROJECT CORTEX_WORKSPACE_PROJECT \
    PG_PORT PG_USER PG_PASS PG_DB PG_CONTAINER \
    RUNTIME

cortex_prepare_code_graph_env() {
    local repo_root="$1"
    export EMBEDDING_BACKEND="${CORTEX_CODE_GRAPH_EMBEDDING_BACKEND:-${EMBEDDING_BACKEND:-local}}"

    # better-code-review-graph 2.x requires a HEAD SHA for temporal migrations.
    # Fresh/uncommitted repos use the package's documented sentinel mode instead
    # of creating a synthetic commit.
    if [ -n "${repo_root}" ] \
       && [ -d "${repo_root}/.git" ] \
       && ! git -C "${repo_root}" rev-parse --verify HEAD >/dev/null 2>&1; then
        export CRG_TEST_ALLOW_NO_GIT="${CRG_TEST_ALLOW_NO_GIT:-1}"
    fi
}

# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

pg_host_available() {
    PGPASSWORD="${PG_PASS}" psql \
        -h localhost -p "${PG_PORT}" \
        -U "${PG_USER}" -d "${PG_DB}" \
        -c "SELECT 1" >/dev/null 2>&1
}

# pg_available — returns 0 if PostgreSQL is reachable
pg_available() {
    if pg_host_available; then
        return 0
    fi

    case "${RUNTIME}" in
        podman)
            "${RUNTIME}" exec "${PG_CONTAINER}" \
                psql -U "${PG_USER}" -d "${PG_DB}" -c "SELECT 1" >/dev/null 2>&1
            ;;
        *)
            return 1
            ;;
    esac
}

# pg_query — run a SELECT and print results with no headers, pipe-delimited
# Usage: pg_query "SELECT id, name FROM agents WHERE project = 'tam'"
pg_query() {
    local sql="$1"
    if pg_host_available; then
        PGPASSWORD="${PG_PASS}" psql \
            -h localhost -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" \
            -t -A -F '|' \
            -c "${sql}"
        return
    fi

    case "${RUNTIME}" in
        podman)
            "${RUNTIME}" exec "${PG_CONTAINER}" \
                psql -U "${PG_USER}" -d "${PG_DB}" \
                -t -A -F '|' \
                -c "${sql}"
            ;;
        *)
            return 1
            ;;
    esac
}

# pg_exec — run an INSERT/UPDATE/DELETE (output suppressed)
# Usage: pg_exec "INSERT INTO events (type, agent) VALUES ('start', 'sophia')"
pg_exec() {
    local sql="$1"
    if pg_host_available; then
        PGPASSWORD="${PG_PASS}" psql \
            -h localhost -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" \
            -v ON_ERROR_STOP=1 \
            -c "${sql}" >/dev/null
        return
    fi

    case "${RUNTIME}" in
        podman)
            "${RUNTIME}" exec "${PG_CONTAINER}" \
                psql -U "${PG_USER}" -d "${PG_DB}" \
                -v ON_ERROR_STOP=1 \
                -c "${sql}" >/dev/null
            ;;
        *)
            return 1
            ;;
    esac
}

# pg_exec_file — execute a SQL file against the active Cortex database
pg_exec_file() {
    local sql_file="$1"
    if [ ! -f "${sql_file}" ]; then
        echo "ERROR: SQL file not found: ${sql_file}" >&2
        return 1
    fi

    if pg_host_available; then
        PGPASSWORD="${PG_PASS}" psql \
            -h localhost -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" \
            -v ON_ERROR_STOP=1 \
            -q -f "${sql_file}"
        return
    fi

    case "${RUNTIME}" in
        podman)
            "${RUNTIME}" exec -i "${PG_CONTAINER}" \
                psql -U "${PG_USER}" -d "${PG_DB}" \
                -v ON_ERROR_STOP=1 \
                -q -f - < "${sql_file}"
            ;;
        *)
            return 1
            ;;
    esac
}

# pg_query_file — execute a SQL file and return results (no headers, pipe-delimited)
pg_query_file() {
    local sql_file="$1"
    if [ ! -f "${sql_file}" ]; then
        echo "ERROR: SQL file not found: ${sql_file}" >&2
        return 1
    fi

    if pg_host_available; then
        PGPASSWORD="${PG_PASS}" psql \
            -h localhost -p "${PG_PORT}" \
            -U "${PG_USER}" -d "${PG_DB}" \
            -v ON_ERROR_STOP=1 \
            -q -t -A -F '|' \
            -f "${sql_file}"
        return
    fi

    case "${RUNTIME}" in
        podman)
            "${RUNTIME}" exec -i "${PG_CONTAINER}" \
                psql -U "${PG_USER}" -d "${PG_DB}" \
                -v ON_ERROR_STOP=1 \
                -q -t -A -F '|' \
                -f - < "${sql_file}"
            ;;
        *)
            return 1
            ;;
    esac
}

# sql_escape — escape single quotes for safe SQL string interpolation
# Usage: val=$(sql_escape "O'Brien")
sql_escape() {
    local raw="$1"
    # Replace each ' with ''
    printf '%s' "${raw//\'/\'\'}"
}

# cortex_repo_root — resolve the repo that owns this .agents directory
cortex_repo_root() {
    if command -v git >/dev/null 2>&1 && git -C "${AGENTS_DIR}/.." rev-parse --show-toplevel >/dev/null 2>&1; then
        git -C "${AGENTS_DIR}/.." rev-parse --show-toplevel
    else
        cd "${AGENTS_DIR}/.." && pwd
    fi
}

# cortex_workspace_root — prefer the parent workspace when the repo lives in a
# numbered lane like 02-cust-portal under a shared monorepo root.
cortex_workspace_root() {
    local repo_root
    local repo_name

    repo_root="$(cortex_repo_root)"
    repo_name="$(basename "${repo_root}")"

    case "${repo_name}" in
        [0-9][0-9]-*)
            dirname "${repo_root}"
            ;;
        *)
            printf '%s\n' "${repo_root}"
            ;;
    esac
}

cortex_vendor_root() {
    local candidate
    local default_root

    default_root="$(cortex_workspace_root)/.agents/data/vendor"

    for candidate in \
        "${CORTEX_VENDOR_ROOT:-}" \
        "${CONFIG_SHARED_VENDOR_ROOT:-}" \
        "${default_root}" \
        "$(cortex_repo_root)/.agents/data/vendor"; do
        [ -n "${candidate}" ] || continue
        if [ -d "${candidate}" ] || [ -f "${candidate}/magic-pdf.json" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    printf '%s\n' "${default_root}"
}

cortex_claude_project_slug() {
    printf '%s' "$1" | sed 's/[[:space:]]\+/-/g; s#/#-#g'
}

# cortex_find_claude_project_dirs — return Claude project dirs matching this
# workspace/repo without pulling in unrelated sibling projects.
cortex_find_claude_project_dirs() {
    local base="${CLAUDE_PROJECTS_DIR:-$HOME/.claude/projects}"
    local path
    local dir

    [ -d "${base}" ] || return 0

    for path in "$(cortex_workspace_root)" "$(cortex_repo_root)"; do
        dir="${base}/$(cortex_claude_project_slug "${path}")"
        if [ -d "${dir}" ]; then
            printf '%s\n' "${dir}"
        fi
    done | awk '!seen[$0]++'

    return 0
}

cortex_find_claude_memory_dirs() {
    local dir
    while IFS= read -r dir; do
        if [ -d "${dir}/memory" ]; then
            printf '%s\n' "${dir}/memory"
        fi
        if [ -d "${dir}/memory.bak" ]; then
            printf '%s\n' "${dir}/memory.bak"
        fi
    done < <(cortex_find_claude_project_dirs) | awk '!seen[$0]++'

    return 0
}

cortex_workspace_config_file() {
    if [ -n "${WORKSPACE_CONFIG_FILE}" ] && [ -f "${WORKSPACE_CONFIG_FILE}" ]; then
        printf '%s\n' "${WORKSPACE_CONFIG_FILE}"
        return 0
    fi

    return 1
}

cortex_workspace_project_from_path() {
    cortex_workspace_project_from_path_json "$1"
}

cortex_visible_agent_sql() {
    local alias="${1:-a}"
    printf "%s" "COALESCE(${alias}.capabilities->>'visibility', 'active') <> 'history-only' AND (COALESCE(${alias}.capabilities->>'keep_visible', 'false') = 'true' OR EXISTS (SELECT 1 FROM agent_profiles ap WHERE ap.project = ${alias}.project AND ap.agent_name = ${alias}.name))"
}

cortex_agent_roles_sql() {
    local agent_sql="${1:?agent_sql required}"
    local project_sql="${2:?project_sql required}"
    printf "%s" "SELECT DISTINCT derived.role
                   FROM (
                         SELECT NULLIF(a.role, '') AS role
                           FROM agents a
                          WHERE a.project = '${project_sql}'
                            AND lower(a.name) = lower('${agent_sql}')
                         UNION ALL
                         SELECT NULLIF(ap.role, '') AS role
                           FROM agent_profiles ap
                          WHERE ap.project = '${project_sql}'
                            AND lower(ap.agent_name) = lower('${agent_sql}')
                        ) AS derived
                  WHERE derived.role IS NOT NULL"
}

cortex_agent_exists() {
    local agent_name="${1:?agent_name required}"
    local project_name="${2:-${CORTEX_PROJECT}}"
    local agent_sql project_sql count
    agent_sql="$(sql_escape "${agent_name}")"
    project_sql="$(sql_escape "${project_name}")"
    count="$(pg_query "
        SELECT COUNT(*)
        FROM (
            SELECT ap.agent_name AS name
            FROM agent_profiles ap
            WHERE ap.project = '${project_sql}'
              AND lower(ap.agent_name) = lower('${agent_sql}')
            UNION
            SELECT a.name
            FROM agents a
            WHERE a.project = '${project_sql}'
              AND lower(a.name) = lower('${agent_sql}')
              AND $(cortex_visible_agent_sql "a")
        ) known_agent
    " 2>/dev/null | tr -d '[:space:]' || true)"
    [ "${count:-0}" -gt 0 ]
}

cortex_known_agents() {
    local project_name="${1:-${CORTEX_PROJECT}}"
    local project_sql
    project_sql="$(sql_escape "${project_name}")"
    pg_query "
        SELECT name
        FROM agents
        WHERE project = '${project_sql}'
          AND $(cortex_visible_agent_sql "agents")
        ORDER BY name
        LIMIT 20
    " 2>/dev/null | awk 'NF { out = out ? out ", " $0 : $0 } END { print out }' || true
}

cortex_normalize_agent_name() {
    local raw_agent="${1:-}"
    local project_name="${2:-${CORTEX_PROJECT}}"

    [ -n "${raw_agent}" ] || return 0

    python3 - "${WORKSPACE_CONFIG_FILE:-}" "${project_name}" "${raw_agent}" <<'PYEOF'
import json
import os
import re
import sys

config_path, project_name, raw_agent = sys.argv[1:4]
normalized = raw_agent.strip().lower()
if not normalized:
    raise SystemExit(0)

alias_map = {}
pattern_map = {}
if config_path and os.path.isfile(config_path):
    try:
        with open(config_path, "r") as handle:
            config = json.load(handle)
        alias_map = config.get("agent_aliases", {}) or {}
        pattern_map = config.get("agent_alias_patterns", {}) or {}
    except Exception:
        alias_map = {}
        pattern_map = {}

project_aliases = alias_map.get(project_name, {}) if isinstance(alias_map, dict) else {}
global_aliases = alias_map.get("*", {}) if isinstance(alias_map, dict) else {}
project_patterns = pattern_map.get(project_name, []) if isinstance(pattern_map, dict) else []
global_patterns = pattern_map.get("*", []) if isinstance(pattern_map, dict) else []

resolved = project_aliases.get(normalized) or global_aliases.get(normalized)
if not resolved:
    for bucket in (project_patterns, global_patterns):
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            pattern = str(entry.get("pattern") or "").strip()
            canonical = str(entry.get("canonical") or "").strip().lower()
            if not pattern or not canonical:
                continue
            try:
                if re.match(pattern, normalized):
                    resolved = canonical
                    break
            except re.error:
                continue
        if resolved:
            break

print((resolved or normalized).strip().lower())
PYEOF
}

# ---------------------------------------------------------------------------
# Utility helpers (preserved from _lib.sh)
# ---------------------------------------------------------------------------

# status — print a coloured status line
# Usage: status green "All systems nominal"
status() {
    local color="$1"
    local msg="$2"
    case "${color}" in
        green)  printf '\033[32m%s\033[0m\n' "${msg}" ;;
        red)    printf '\033[31m%s\033[0m\n' "${msg}" ;;
        yellow) printf '\033[33m%s\033[0m\n' "${msg}" ;;
        *)      printf '%s\n' "${msg}" ;;
    esac
}

# read_file — return file contents, or empty string if file is missing
# Usage: content=$(read_file "/path/to/file")
read_file() {
    local path="$1"
    if [ -f "${path}" ]; then
        cat "${path}"
    else
        echo ""
    fi
}
