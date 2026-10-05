#!/usr/bin/env bash
# Cortex release-owned member API bridge. No bearer enters this shell.

CORTEX_SHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Legacy callers use this only in error text. Do not echo unvalidated URL env.
CORTEX_API="selected Cortex v2 origin"

_cortex_facade_unavailable() {
    printf '%s\n' 'ERROR: facade request unavailable in this release' >&2
    return 2
}

cortex_api_call() {
    if [ "$#" -lt 2 ] || [ "$#" -gt 4 ] || [ -n "${3:-}" ] \
       || [ -n "${CORTEX_API_PAYLOAD_FILE:-}" ] || [ "${1:-}" != GET ] \
       || [ "${2:-}" != /projects ] || [ -n "${CORTEX_CTO_OVERRIDE:-}" ] \
       || [ "${CORTEX_API_WITH_ADMIN:-0}" != 0 ]; then
        _cortex_facade_unavailable
        return 2
    fi
    if [ -z "${CORTEX_CONNECTION_PROFILE:-}" ]; then
        printf '%s\n' 'ERROR: select CORTEX_CONNECTION_PROFILE (non-secret v2 member profile)' >&2
        return 2
    fi
    if [ -n "${4:-}" ]; then
        "${CORTEX_SHIM_DIR}/cortex-agent" --config "${CORTEX_CONNECTION_PROFILE}" api GET /projects --agent-name "$4" </dev/null
    else
        "${CORTEX_SHIM_DIR}/cortex-agent" --config "${CORTEX_CONNECTION_PROFILE}" api GET /projects </dev/null
    fi
}

cortex_api_json() { cortex_api_call "$@"; }
cortex_api_call_json() { cortex_api_call "$@"; }
cortex_api() {
    # Dynamic legacy method/path/query/body wrappers admit the same exact row.
    if [ "$#" -ne 2 ]; then
        _cortex_facade_unavailable
        return 2
    fi
    cortex_api_call "$1" "$2" "" "${CORTEX_AGENT_ID:-}"
}

# File/form and operator adapters have no member mapping in this release.
# Do not open inputs, create outputs or load an administrative credential.
cortex_api_call_to_file() { _cortex_facade_unavailable; }
cortex_api_download_admin() { _cortex_facade_unavailable; }

# Credential-free string utilities used to construct dynamic caller data.
cortex_agent_base_name() {
    local base="${1%%@*}"
    printf '%s' "${base%%:*}"
}

_cortex_urlencode() {
    local s="$1" safe="$2" out="" c n i LC_ALL=C
    for (( i=0; i<${#s}; i++ )); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9_.~-]) out+="$c" ;;
            /) if [ "$safe" = / ]; then out+="$c"; else out+='%2F'; fi ;;
            *) printf -v n '%d' "'$c"; printf -v c '%%%02X' "$(( n & 0xFF ))"; out+="$c" ;;
        esac
    done
    printf '%s' "$out"
}
cortex_api_urlencode() { _cortex_urlencode "$1" /; }
cortex_api_urlencode_strict() { _cortex_urlencode "$1" ''; }

cortex_api_call_admin() {
    _cortex_facade_unavailable
}
