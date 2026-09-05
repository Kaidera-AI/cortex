#!/usr/bin/env bash
# Cortex API helper for agent-facing CLI scripts.
# Source this file from cortex-* commands that should talk only to the Cortex API.
# No direct DB access. Admin credentials are loaded only for explicit
# cortex_api_call_admin callers and are never sent by ordinary API calls.

set -euo pipefail

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
RUNTIME_CONFIG_FILE="${CORTEX_RUNTIME_CONFIG:-${AGENTS_DIR}/config/runtime.yaml}"
WORKSPACE_CONFIG_FILE="${CORTEX_WORKSPACE_CONFIG:-${AGENTS_DIR}/config/workspace.json}"

cortex_runtime_yaml_value() {
    local section="$1"
    local key="$2"
    [ -f "${RUNTIME_CONFIG_FILE}" ] || return 0
    awk -v section="${section}" -v key="${key}" '
        function trim(v) { sub(/^[[:space:]]+/,"",v); sub(/[[:space:]]+$/,"",v); return v }
        /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
        /^[^[:space:]].*:[[:space:]]*$/ { current=$0; sub(/:[[:space:]]*$/,"",current); current=trim(current); next }
        current==section { pattern="^[[:space:]]*"key":[[:space:]]*"; if($0~pattern){ v=$0; sub(pattern,"",v); v=trim(v); gsub(/^["'"'"']|["'"'"']$/,"",v); print v; exit } }
    ' "${RUNTIME_CONFIG_FILE}"
}

cortex_workspace_project_from_path_json() {
    local path="$1"
    [ -f "${WORKSPACE_CONFIG_FILE}" ] || return 0
    python3 -S - "${WORKSPACE_CONFIG_FILE}" "${path}" <<'PYEOF'
import json
import os
import sys

config_path = sys.argv[1]
path = os.path.realpath(sys.argv[2])

with open(config_path, "r", encoding="utf-8") as handle:
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

export CORTEX_API="${CORTEX_API_URL:-http://localhost:8501}"
CONFIG_PROJECT_NAME="$(cortex_runtime_yaml_value project name)"
CORTEX_WORKSPACE_PROJECT="$(cortex_workspace_project_from_path_json "$(cd "${AGENTS_DIR}/.." && pwd)" 2>/dev/null || true)"
export CORTEX_PROJECT="${CORTEX_PROJECT:-${CORTEX_WORKSPACE_PROJECT:-${CONFIG_PROJECT_NAME}}}"
export CORTEX_WORKSPACE_PROJECT

if [ -n "${CORTEX_WORKSPACE_PROJECT}" ] \
   && [ -n "${CORTEX_PROJECT}" ] \
   && [ "${CORTEX_PROJECT}" != "${CORTEX_WORKSPACE_PROJECT}" ]; then
    if [ -z "${CORTEX_CTO_OVERRIDE:-}" ]; then
        cat >&2 <<EOF
ERROR: Cortex project isolation guard blocked a cross-project API context.
  Workspace project: ${CORTEX_WORKSPACE_PROJECT}
  Requested CORTEX_PROJECT: ${CORTEX_PROJECT}

Use a one-off CTO override for a single command only:
  CORTEX_CTO_OVERRIDE=<decision-id> CORTEX_PROJECT=${CORTEX_PROJECT} <command>
EOF
        exit 66
    else
        echo "WARNING: CTO override ${CORTEX_CTO_OVERRIDE} permits one-off cross-project API context ${CORTEX_WORKSPACE_PROJECT} -> ${CORTEX_PROJECT}. Do not persist." >&2
    fi
fi

cortex_workspace_project_id() {
    local project="$1"
    [ -n "${project}" ] || return 0
    [ -f "${WORKSPACE_CONFIG_FILE}" ] || return 0
    python3 -S - "${WORKSPACE_CONFIG_FILE}" "${project}" <<'PYEOF'
import json
import sys

config_path, project_key = sys.argv[1], sys.argv[2]
with open(config_path, "r", encoding="utf-8") as handle:
    data = json.load(handle)

for project in data.get("projects", []):
    if project.get("key") == project_key:
        print(project.get("project_id", ""))
        break
PYEOF
}

cortex_valid_uuid() {
    [[ "${1:-}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
}

cortex_api_project_id() {
    local project="$1"
    [ -n "${project}" ] || return 0
    local encoded raw
    encoded="$(cortex_api_urlencode_strict "${project}")"
    raw="$(curl -sS --max-time "${CORTEX_API_PROJECT_MAX_TIME:-3}" \
        -H "X-Project: ${project}" \
        "${CORTEX_API%/}/projects/${encoded}" 2>/dev/null || true)"
    [ -n "${raw}" ] || return 0
    printf '%s' "${raw}" | python3 -S -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
value = str(data.get("project_id") or "").strip()
if value:
    print(value)
'
}

cortex_resolve_project_id() {
    local api_id workspace_id runtime_id env_id candidate
    api_id="$(cortex_api_project_id "${CORTEX_PROJECT}" 2>/dev/null || true)"
    workspace_id="$(cortex_workspace_project_id "${CORTEX_PROJECT}" 2>/dev/null || true)"
    runtime_id="$(cortex_runtime_yaml_value project project_id 2>/dev/null || true)"
    env_id="${CORTEX_PROJECT_ID:-}"

    for candidate in "${api_id}" "${workspace_id}" "${runtime_id}" "${env_id}"; do
        candidate="$(printf '%s' "${candidate}" | tr -d '[:space:]')"
        if cortex_valid_uuid "${candidate}"; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

if [ -z "${CORTEX_PROJECT}" ]; then
    cat >&2 <<EOF
ERROR: CORTEX_PROJECT is not set. Configure ${RUNTIME_CONFIG_FILE}, ${WORKSPACE_CONFIG_FILE}, or export CORTEX_PROJECT.
EOF
    exit 67
fi

RESOLVED_CORTEX_PROJECT_ID="$(cortex_resolve_project_id || true)"
if cortex_valid_uuid "${RESOLVED_CORTEX_PROJECT_ID}"; then
    export CORTEX_PROJECT_ID="${RESOLVED_CORTEX_PROJECT_ID}"
fi
unset RESOLVED_CORTEX_PROJECT_ID

cortex_legacy_admin_token() {
    case "${1:-}" in
        cortex-local-admin)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

cortex_load_admin_token() {
    if [ -n "${CORTEX_ADMIN_TOKEN:-}" ] && ! cortex_legacy_admin_token "${CORTEX_ADMIN_TOKEN}"; then
        return 0
    fi

    local repo_root
    repo_root="$(cd "${AGENTS_DIR}/.." && pwd)"
    local candidate line key value
    for candidate in "${repo_root}/local-cortex/.env" "$(cd "${repo_root}/.." && pwd)/local-cortex/.env"; do
        [ -f "${candidate}" ] || continue
        while IFS= read -r line || [ -n "${line}" ]; do
            case "${line}" in
                ""|\#*) continue ;;
                export\ *) line="${line#export }" ;;
            esac
            key="${line%%=*}"
            value="${line#*=}"
            [ "${key}" = "CORTEX_ADMIN_TOKEN" ] || continue
            case "${value}" in
                \"*\") value="${value#\"}"; value="${value%\"}" ;;
                \'*\') value="${value#\'}"; value="${value%\'}" ;;
            esac
            [ -n "${value}" ] || continue
            export CORTEX_ADMIN_TOKEN="${value}"
            return 0
        done <"${candidate}"
    done
}

cortex_agent_display_name() {
    local base="${1%%@*}"
    base="${base%%:*}"
    printf '%s@%s' "${base}" "${CORTEX_PROJECT}"
}

cortex_agent_base_name() {
    local base="${1%%@*}"
    printf '%s' "${base%%:*}"
}

cortex_api_urlencode() {
    # Pure-bash equivalent of Python urllib.parse.quote(s) with the default
    # safe='/' set: keep A-Z a-z 0-9 and _.-~/ verbatim, percent-encode every
    # other byte (UTF-8 multibyte encoded byte-by-byte, exactly like quote()).
    # This avoids a python3 process spawn — these CLIs call it several times per
    # invocation and the whole agent fleet runs them constantly (E5 startup fix).
    local s="$1" out="" c n i old_lc="${LC_ALL:-}"
    LC_ALL=C  # iterate bytes, not characters, so UTF-8 encodes per-byte
    for (( i=0; i<${#s}; i++ )); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9_.~/-]) out+="$c" ;;
            *) printf -v n '%d' "'$c"; printf -v c '%%%02X' "$(( n & 0xFF ))"; out+="$c" ;;
        esac
    done
    LC_ALL="$old_lc"
    printf '%s' "$out"
}

cortex_api_urlencode_strict() {
    # Pure-bash equivalent of urllib.parse.quote(s, safe='') — like the helper
    # above but also percent-encodes '/'. Used for single path segments such as
    # a project key in /projects/<key>.
    local s="$1" out="" c n i old_lc="${LC_ALL:-}"
    LC_ALL=C
    for (( i=0; i<${#s}; i++ )); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9_.~-]) out+="$c" ;;
            *) printf -v n '%d' "'$c"; printf -v c '%%%02X' "$(( n & 0xFF ))"; out+="$c" ;;
        esac
    done
    LC_ALL="$old_lc"
    printf '%s' "$out"
}

# Base API call: cortex_api_call <METHOD> <path> [payload] [agent_name] [curl args...]
cortex_api_call() {
    local method="$1" path="$2"
    local payload="${3:-}"
    local agent_name="${4:-}"
    local -a extra_curl_args=("${@:5}")

    if [ -z "${CORTEX_PROJECT}" ]; then
        echo "ERROR: CORTEX_PROJECT is not set. Configure .agents/config/runtime.yaml or export CORTEX_PROJECT." >&2
        return 1
    fi

    # No -f: we capture the body + HTTP status ourselves so the server's JSON
    # error detail (e.g. a "did you mean?" agent-name suggestion, validation
    # messages) is surfaced instead of being swallowed by curl --fail. -sS keeps
    # it quiet on success but still shows transport errors.
    local max_time="${CORTEX_API_MAX_TIME:-30}"
    local -a curl_args=(-sS --max-time "${max_time}" -X "${method}"
        "${CORTEX_API}${path}" -H "X-Project: ${CORTEX_PROJECT}"
        -w $'\n%{http_code}')
    local payload_file="${CORTEX_API_PAYLOAD_FILE:-}"
    local payload_file_owned=0

    [ -n "${agent_name}" ] && curl_args+=(-H "X-Agent-Name: ${agent_name}")
    if [ -n "${CORTEX_CTO_OVERRIDE:-}" ]; then
        curl_args+=(-H "X-Cortex-CTO-Override: ${CORTEX_CTO_OVERRIDE}")
    fi
    # LCX-UR-014: opt-in admin token for admin-gated endpoints (e.g. /beat/events).
    # Only sent when a caller explicitly requests it via cortex_api_call_admin so
    # ordinary calls never leak operator credentials.
    if [ "${CORTEX_API_WITH_ADMIN:-0}" = "1" ] && [ -n "${CORTEX_ADMIN_TOKEN:-}" ]; then
        curl_args+=(-H "X-Cortex-Admin-Token: ${CORTEX_ADMIN_TOKEN}")
    fi
    if [ -n "${payload_file}" ]; then
        [ -f "${payload_file}" ] || {
            echo "ERROR: CORTEX_API_PAYLOAD_FILE does not exist: ${payload_file}" >&2
            return 1
        }
        curl_args+=(-H "Content-Type: application/json" --data-binary "@${payload_file}")
    elif [ -n "${payload}" ]; then
        curl_args+=(-H "Content-Type: application/json")
        if [ "${#payload}" -gt 100000 ]; then
            payload_file="$(mktemp "${TMPDIR:-/tmp}/cortex-api-payload.XXXXXX.json")"
            payload_file_owned=1
            printf '%s' "${payload}" >"${payload_file}"
            curl_args+=(--data-binary "@${payload_file}")
        else
            curl_args+=(-d "${payload}")
        fi
    fi
    if [ "${#extra_curl_args[@]}" -gt 0 ]; then
        curl_args+=("${extra_curl_args[@]}")
    fi

    local raw status=0
    raw="$(/usr/bin/curl "${curl_args[@]}")" || status=$?
    [ "${payload_file_owned}" -eq 0 ] || rm -f "${payload_file}"

    # Transport-level failure (connection refused, timeout): no usable HTTP body.
    # -sS already printed curl's own error to stderr; propagate the exit code.
    if [ "${status}" -ne 0 ]; then
        return "${status}"
    fi

    # -w appended "\n<http_code>" after the body; split them back apart.
    local http_code="${raw##*$'\n'}"
    local body="${raw%$'\n'*}"

    if [[ "${http_code}" =~ ^[0-9]+$ ]] && [ "${http_code}" -ge 400 ]; then
        local detail
        detail="$(printf '%s' "${body}" | python3 -S -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("detail") or d.get("error") or "")
except Exception:
    pass' 2>/dev/null || true)"
        if [ -n "${detail}" ]; then
            printf 'API error %s: %s\n' "${http_code}" "${detail}" >&2
        else
            printf 'API error %s\n' "${http_code}" >&2
            [ -n "${body}" ] && printf '%s\n' "${body}" >&2
        fi
        return 22
    fi

    printf '%s' "${body}"
    return 0
}

# Aliases used by API-only command-surface scripts.
cortex_api_json() {
    cortex_api_call "$@"
}

cortex_api_call_json() {
    cortex_api_call "$@"
}

# Admin-scoped API call for operator/admin-gated endpoints (e.g. /beat/events).
# Sends X-Cortex-Admin-Token from CORTEX_ADMIN_TOKEN. LCX-UR-014: readers of
# admin-gated endpoints must send the token or the API returns 403 silently.
cortex_api_call_admin() {
    cortex_load_admin_token
    if [ -z "${CORTEX_ADMIN_TOKEN:-}" ]; then
        echo "ERROR: CORTEX_ADMIN_TOKEN is not set; required for admin-gated endpoint ${2:-}" >&2
        return 1
    fi
    CORTEX_API_WITH_ADMIN=1 cortex_api_call "$@"
}

# Simple wrapper matching old cortex_api signature: cortex_api GET /path [k=v ...]
cortex_api() {
    local method="$1" path="$2"; shift 2
    local agent="${CORTEX_AGENT_ID:-}"
    case "$method" in
        GET)
            local qs=""; for p in "$@"; do qs="${qs:+${qs}&}${p}"; done
            cortex_api_call GET "${path}${qs:+?${qs}}" "" "${agent}"
            ;;
        POST|PUT)
            cortex_api_call "$method" "$path" "${1:-}" "${agent}"
            ;;
    esac
}
